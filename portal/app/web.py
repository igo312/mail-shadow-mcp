"""HTTP surface: invite gate, provisioning form, result page and MCP gateway."""

from __future__ import annotations

import json
import logging
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import templates
from .config import Config
from .db import Database
from .gateway import Gateway
from .provisioner import (
    ProvisionError,
    Provisioner,
    folders_from_instance,
)
from .providers import normalize_email
from .security import CookieError, RateLimiter, TokenService, constant_time_equal

log = logging.getLogger(__name__)

MAX_FORM_BYTES = 64 * 1024
SESSION_COOKIE = "msm_session"
FLASH_COOKIE = "msm_flash"

_CSP = (
    "default-src 'none'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


class PortalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], app: "WebApp"):
        self.app = app
        super().__init__(address, handler)


class WebApp:
    def __init__(
        self,
        config: Config,
        db: Database,
        tokens: TokenService,
        provisioner: Provisioner,
        gateway: Gateway,
        limiter: RateLimiter,
    ) -> None:
        self.config = config
        self.db = db
        self.tokens = tokens
        self.provisioner = provisioner
        self.gateway = gateway
        self.limiter = limiter
        self.static_dir = Path(__file__).with_name("static")
        templates.set_public_url(config.public_url)
        self._server: PortalHTTPServer | None = None

    # -- server ----------------------------------------------------------
    def create_server(self) -> PortalHTTPServer:
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "mail-shadow-portal/1.0"
            sys_version = ""

            def do_GET(self) -> None:  # noqa: N802
                app.handle(self)

            def do_POST(self) -> None:  # noqa: N802
                app.handle(self)

            def do_HEAD(self) -> None:  # noqa: N802
                app.handle(self)

            def do_PUT(self) -> None:  # noqa: N802
                app.handle(self)

            def do_DELETE(self) -> None:  # noqa: N802
                app.handle(self)

            def do_OPTIONS(self) -> None:  # noqa: N802
                app.handle(self)

            def log_message(self, fmt: str, *args: Any) -> None:
                log.debug("%s - %s", self.address_string(), fmt % args)

        server = PortalHTTPServer(
            (self.config.listen_host, self.config.listen_port),
            Handler,
            self,
        )
        self._server = server
        return server

    def serve_forever(self) -> None:
        assert self._server is not None
        self._server.serve_forever()

    def shutdown(self) -> None:
        if self._server is not None:
            self._server.shutdown()

    # -- dispatch --------------------------------------------------------
    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        handler._sent_headers = False  # type: ignore[attr-defined]
        path = urlsplit(handler.path).path
        try:
            if path in {"/mcp", "/mcp/"}:
                self.gateway.handle(handler)
                return
            if path.startswith("/static/"):
                self._serve_static(handler, path)
                return
            if path == "/healthz":
                self._health(handler)
                return
            if path == "/favicon.ico":
                self._empty(handler, 204)
                return
            if path == "/":
                self._home(handler)
                return
            if path == "/invite" and handler.command == "POST":
                self._invite(handler)
                return
            if path == "/provision":
                if handler.command == "GET":
                    self._provision_form(handler)
                elif handler.command == "POST":
                    self._provision_submit(handler)
                else:
                    self._empty(handler, 405, allow="GET, POST")
                return
            if path == "/folders" and handler.command == "POST":
                self._folders(handler)
                return
            if path.startswith("/instance/") and handler.command == "GET":
                self._instance(handler, path[len("/instance/") :])
                return
            if path == "/logout" and handler.command == "POST":
                self._logout(handler)
                return
            self._error(handler, 404, "页面不存在", "返回首页看看。", show_logout=False)
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True
        except Exception:
            log.exception("unhandled error while serving %s %s", handler.command, path)
            if not getattr(handler, "_sent_headers", False):
                try:
                    self._error(
                        handler,
                        500,
                        "服务器内部错误",
                        "请稍后重试；如果问题持续，请联系服务管理员。",
                    )
                except Exception:
                    handler.close_connection = True
            else:
                handler.close_connection = True

    # -- pages -----------------------------------------------------------
    def _health(self, handler: BaseHTTPRequestHandler) -> None:
        try:
            self.db.invite_count()
        except Exception:
            log.exception("health check: database unavailable")
            self._json(handler, 503, {"ok": False})
            return
        self._json(handler, 200, {"ok": True})

    def _home(self, handler: BaseHTTPRequestHandler) -> None:
        session, _ = self._session(handler)
        if session:
            self._redirect(handler, "/provision")
            return
        self._html(
            handler,
            200,
            templates.invite_page(no_invites=self.db.invite_count() == 0),
        )

    def _invite(self, handler: BaseHTTPRequestHandler) -> None:
        ip = _client_ip(handler, self.config)
        decision = self.limiter.check(
            f"invite:{ip}",
            self.config.invite_rate_limit,
            self.config.invite_rate_window,
        )
        if not decision.allowed:
            self._html(
                handler,
                429,
                templates.invite_page(error="尝试次数过多，请稍后再试。"),
            )
            return
        if not self._origin_ok(handler):
            self._html(handler, 403, templates.invite_page(error="请求来源无效，请用浏览器直接打开本页后重试。"))
            return
        try:
            form = _read_form(handler)
        except ValueError:
            self._html(handler, 400, templates.invite_page(error="请求格式无效。"))
            return
        code = (form.get("invite") or "").strip()
        if not code or len(code) > 200:
            self._html(handler, 400, templates.invite_page(error="邀请码无效或已用完。"))
            return
        invite = self.db.consume_invite(self.tokens.hash_invite(code))
        if not invite:
            self.db.audit("invite_rejected", ip=ip)
            self._html(handler, 403, templates.invite_page(error="邀请码无效或已用完。"))
            return
        session_cookie = self.tokens.make_session(invite["id"], self.config.session_ttl_seconds)
        self.db.audit("invite_used", ip=ip, detail={"invite_id": invite["id"]})
        self._redirect(
            handler,
            "/provision",
            cookies=[(SESSION_COOKIE, session_cookie, self.config.session_ttl_seconds, "Lax")],
        )

    def _provision_form(self, handler: BaseHTTPRequestHandler) -> None:
        session, cookie = self._session(handler)
        if not session:
            self._redirect(handler, "/")
            return
        invite = self.db.get_invite(session["invite"])
        remaining = ""
        if invite:
            left = max(0, int(invite["max_uses"]) - int(invite["uses"]))
            remaining = f"此邀请码还可使用 {left} 次" if left else "此邀请码已使用（当前会话仍可创建）"
        csrf = self.tokens.csrf_token(cookie)
        self._html(
            handler,
            200,
            templates.provision_page(csrf=csrf, invite_remaining=remaining),
        )

    def _provision_submit(self, handler: BaseHTTPRequestHandler) -> None:
        session, cookie = self._session(handler)
        if not session:
            self._redirect(handler, "/")
            return
        ip = _client_ip(handler, self.config)
        decision = self.limiter.check(
            f"provision:{ip}",
            self.config.provision_rate_limit,
            self.config.provision_rate_window,
        )
        if not decision.allowed:
            self._render_provision_error(
                handler,
                session,
                cookie,
                "请求过于频繁，请稍后再试。",
                429,
                hint=f"请在 {decision.retry_after} 秒后重试。",
            )
            return
        if not self._origin_ok(handler):
            self._render_provision_error(handler, session, cookie, "请求来源无效，请用浏览器直接打开本页后重试。", 403)
            return
        try:
            form_all = _read_form_values(handler)
            form = {key: values[-1] for key, values in form_all.items()}
        except ValueError:
            self._render_provision_error(handler, session, cookie, "表单数据无效，请刷新后重试。", 400)
            return
        if not self.tokens.verify_csrf(cookie, form.get("csrf") or ""):
            self._render_provision_error(handler, session, cookie, "页面已过期，请重新提交。", 400)
            return

        email = (form.get("email") or "").strip()
        password = form.get("password") or ""
        host = (form.get("host") or "").strip()
        port = (form.get("port") or "").strip()
        tls_mode = (form.get("tls_mode") or "tls").strip()
        provider_key = (form.get("provider") or "auto").strip()

        # Three states: the picker was never used (None -> deployment default),
        # "sync everything" ([]), or an explicit selection.
        folders_mode = (form.get("folders_mode") or "").strip()
        picked = [name for name in form_all.get("folders", []) if name.strip()]
        if folders_mode == "all":
            selected_folders: list[str] | None = []
        elif form.get("folders_used") == "1":
            if not picked:
                self._render_provision_error(
                    handler,
                    session,
                    cookie,
                    "请至少选择一个要同步的文件夹，或选择「同步全部文件夹」。",
                    400,
                    field="folders",
                    email=email,
                    host=host,
                    port=port,
                    tls_mode=tls_mode,
                    provider_key=provider_key,
                    selected_folders=picked,
                )
                return
            selected_folders = picked
        else:
            selected_folders = None

        try:
            result = self.provisioner.provision(
                email,
                password,
                host=host,
                port=port,
                tls_mode=tls_mode,
                provider_key=provider_key,
                invite_id=session["invite"],
                ip=ip,
                folders=selected_folders,
            )
        except ProvisionError as exc:
            status = {
                "invalid_input": 400,
                "imap_auth": 400,
                "insecure_imap": 400,
                "capacity": 429,
                "in_progress": 409,
                "docker": 502,
                "healthcheck": 502,
                "internal": 500,
            }.get(exc.code, 400)
            self.db.audit(
                "provision_error",
                ip=ip,
                email=email or None,
                detail={"code": exc.code, "message": exc.message},
            )
            self._render_provision_error(
                handler,
                session,
                cookie,
                exc.message,
                status,
                field=exc.field,
                hint=exc.hint,
                email=email,
                host=host,
                port=port,
                tls_mode=tls_mode,
                provider_key=provider_key,
                selected_folders=selected_folders,
                folders_all=folders_mode == "all",
            )
            return

        flash = self.tokens.make_flash(
            result.instance["id"],
            result.token,
            self.config.flash_ttl_seconds,
            reused=result.reused,
            rotated=result.rotated,
            warning=result.warning,
        )
        self._redirect(
            handler,
            f"/instance/{result.instance['id']}",
            cookies=[(FLASH_COOKIE, flash, self.config.flash_ttl_seconds, "Lax")],
        )

    def _folders(self, handler: BaseHTTPRequestHandler) -> None:
        """Return the mailbox folder list for the picker (JSON)."""
        session, cookie = self._session(handler)
        if not session:
            self._json(handler, 401, {"ok": False, "error": "会话已过期，请刷新页面后重试。"})
            return
        ip = _client_ip(handler, self.config)
        decision = self.limiter.check(
            f"folders:{ip}",
            self.config.folders_rate_limit,
            self.config.folders_rate_window,
        )
        if not decision.allowed:
            self._json(
                handler,
                429,
                {
                    "ok": False,
                    "error": f"请求过于频繁，请在 {decision.retry_after} 秒后重试。",
                },
            )
            return
        if not self._origin_ok(handler):
            self._json(handler, 403, {"ok": False, "error": "请求来源无效，请刷新页面后重试。"})
            return
        try:
            form = _read_form(handler)
        except ValueError:
            self._json(handler, 400, {"ok": False, "error": "表单数据无效，请刷新后重试。"})
            return
        if not self.tokens.verify_csrf(cookie, form.get("csrf") or ""):
            self._json(handler, 400, {"ok": False, "error": "页面已过期，请刷新页面后重试。"})
            return

        started = time.monotonic()
        try:
            folders = self.provisioner.list_mailbox_folders(
                (form.get("email") or "").strip(),
                form.get("password") or "",
                host=(form.get("host") or "").strip(),
                port=(form.get("port") or "").strip(),
                tls_mode=(form.get("tls_mode") or "tls").strip(),
                provider_key=(form.get("provider") or "auto").strip(),
            )
        except ProvisionError as exc:
            self.db.audit(
                "folders_error",
                ip=ip,
                email=(form.get("email") or "").strip() or None,
                detail={"code": exc.code, "message": exc.message},
            )
            self._json(
                handler,
                400,
                {"ok": False, "error": exc.message, "field": exc.field, "hint": exc.hint},
            )
            return

        self._json(
            handler,
            200,
            {
                "ok": True,
                "folders": folders,
                "count": len(folders),
                "current": self._current_folder_selection(
                    form.get("email") or "", session
                ),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            },
        )

    def _current_folder_selection(
        self, email: str, session: dict[str, Any]
    ) -> list[str] | None:
        """Existing selection for this mailbox, but only for its owner session."""
        if not email:
            return None
        try:
            instance = self.db.get_instance_by_email(normalize_email(email))
        except Exception:
            return None
        if not instance:
            return None
        if str(instance.get("owner_invite") or "") != str(session.get("invite") or ""):
            return None
        return folders_from_instance(instance)

    def _render_provision_error(
        self,
        handler: BaseHTTPRequestHandler,
        session: dict[str, Any],
        cookie: str,
        message: str,
        status: int,
        *,
        field: str = "",
        hint: str = "",
        email: str = "",
        host: str = "",
        port: str = "",
        tls_mode: str = "tls",
        provider_key: str = "auto",
        selected_folders: list[str] | None = None,
        folders_all: bool = False,
    ) -> None:
        self._html(
            handler,
            status,
            templates.provision_page(
                csrf=self.tokens.csrf_token(cookie),
                email=email,
                host=host,
                port=port,
                tls_mode=tls_mode or "tls",
                provider_key=provider_key or "auto",
                error=message,
                field=field,
                hint=hint,
                selected_folders=selected_folders,
                folders_all=folders_all,
            ),
        )

    def _instance(self, handler: BaseHTTPRequestHandler, instance_id: str) -> None:
        session, cookie = self._session(handler)
        if not session:
            self._redirect(handler, "/")
            return
        if not instance_id or len(instance_id) > 64 or not all(
            ch in "0123456789abcdef" for ch in instance_id
        ):
            self._error(handler, 404, "实例不存在", "链接可能已经失效。")
            return
        instance = self.db.get_instance(instance_id)
        if not instance:
            self._error(handler, 404, "实例不存在", "链接可能已经失效。")
            return
        owner = str(instance.get("owner_invite") or "")
        if owner and owner != session["invite"]:
            # Do not reveal another invite's mailbox details.  Instance IDs are
            # random, but a leaked link must not become a cross-tenant read.
            self._error(handler, 404, "实例不存在", "链接可能已经失效。")
            return

        flash_cookie = _cookies(handler).get(FLASH_COOKIE, "")
        if flash_cookie:
            try:
                flash = self.tokens.parse_flash(flash_cookie)
            except CookieError:
                flash = None
            if (
                flash
                and flash.get("instance") == instance_id
                and instance.get("status") == "running"
                and isinstance(flash.get("token"), str)
                and self._token_matches(instance, flash["token"])
            ):
                self._html(
                    handler,
                    200,
                    templates.result_page(
                        instance=instance,
                        token=flash["token"],
                        csrf=self.tokens.csrf_token(cookie),
                        reused=bool(flash.get("reused")),
                        rotated=bool(flash.get("rotated")),
                        warning=str(flash.get("warning") or ""),
                    ),
                )
                return

        self._html(
            handler,
            200,
            templates.instance_status_page(
                instance=instance,
                csrf=self.tokens.csrf_token(cookie),
            ),
        )

    def _logout(self, handler: BaseHTTPRequestHandler) -> None:
        session, cookie = self._session(handler)
        if not session:
            self._redirect(handler, "/")
            return
        try:
            form = _read_form(handler)
        except ValueError:
            self._error(handler, 400, "无法退出", "请求格式无效，请刷新后重试。")
            return
        if not self.tokens.verify_csrf(cookie, form.get("csrf") or ""):
            self._error(handler, 403, "无法退出", "页面已过期，请刷新后重试。")
            return
        self._redirect(
            handler,
            "/",
            clear_cookies=[SESSION_COOKIE, FLASH_COOKIE],
        )

    # -- session / cookies ----------------------------------------------
    def _session(self, handler: BaseHTTPRequestHandler) -> tuple[dict[str, Any] | None, str]:
        cookie = _cookies(handler).get(SESSION_COOKIE, "")
        if not cookie:
            return None, ""
        try:
            session = self.tokens.parse_session(cookie)
        except CookieError:
            return None, ""
        if not self.db.invite_is_active(session["invite"]):
            return None, ""
        return session, cookie

    def _origin_ok(self, handler: BaseHTTPRequestHandler) -> bool:
        # Sec-Fetch-Site is authoritative when the client sends it: a
        # cross-site form post must never be accepted.
        fetch_site = (handler.headers.get("Sec-Fetch-Site") or "").lower()
        if fetch_site == "cross-site":
            return False
        origin = handler.headers.get("Origin")
        # Embedded webviews, sandboxed previews and some privacy browsers
        # send an opaque origin ("null") for a same-origin form post.  Treat
        # it like a missing Origin instead of rejecting a valid request; the
        # session cookie is SameSite=Lax and framing is denied, so allowing
        # it does not enable cross-site state changes.
        if origin and origin.strip().lower() != "null":
            return origin.rstrip("/") == self.config.public_origin
        return True

    def _token_matches(self, instance: dict[str, Any], token: str) -> bool:
        return constant_time_equal(self.tokens.hash_mcp_token(token), instance["token_hash"])

    def _set_cookie(
        self,
        handler: BaseHTTPRequestHandler,
        name: str,
        value: str,
        max_age: int,
        *,
        same_site: str = "Lax",
    ) -> None:
        parts = [
            f"{name}={value}",
            "Path=/",
            f"Max-Age={int(max_age)}",
            "HttpOnly",
            f"SameSite={same_site}",
        ]
        if self.config.secure_cookies:
            parts.append("Secure")
        handler.send_header("Set-Cookie", "; ".join(parts))

    def _clear_cookie(self, handler: BaseHTTPRequestHandler, name: str) -> None:
        parts = [f"{name}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
        if self.config.secure_cookies:
            parts.append("Secure")
        handler.send_header("Set-Cookie", "; ".join(parts))

    # -- static ----------------------------------------------------------
    def _serve_static(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        name = path[len("/static/") :]
        allowed = {
            "app.css": "text/css; charset=utf-8",
            "app.js": "application/javascript; charset=utf-8",
        }
        content_type = allowed.get(name)
        if not content_type:
            self._empty(handler, 404)
            return
        file_path = self.static_dir / name
        try:
            data = file_path.read_bytes()
        except OSError:
            self._empty(handler, 404)
            return
        self._start(handler, 200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "public, max-age=3600")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(data)

    # -- response helpers ------------------------------------------------
    def _start(self, handler: BaseHTTPRequestHandler, status: int) -> None:
        handler._sent_headers = True  # type: ignore[attr-defined]
        handler.send_response(status)
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
        if self.config.secure_cookies:
            handler.send_header(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )

    def _html(self, handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self._start(handler, status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Security-Policy", _CSP)
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(data)

    def _json(self, handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._start(handler, status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        if handler.command != "HEAD":
            handler.wfile.write(data)

    def _empty(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        *,
        allow: str = "",
    ) -> None:
        self._start(handler, status)
        if allow:
            handler.send_header("Allow", allow)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()

    def _redirect(
        self,
        handler: BaseHTTPRequestHandler,
        location: str,
        status: int = 303,
        *,
        cookies: list[tuple[str, str, int, str]] | None = None,
        clear_cookies: list[str] | None = None,
    ) -> None:
        self._start(handler, status)
        for name, value, max_age, same_site in cookies or []:
            self._set_cookie(handler, name, value, max_age, same_site=same_site)
        for name in clear_cookies or []:
            self._clear_cookie(handler, name)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()

    def _error(
        self,
        handler: BaseHTTPRequestHandler,
        status: int,
        title: str,
        message: str,
        *,
        hint: str = "",
        show_logout: bool = False,
    ) -> None:
        session, cookie = self._session(handler)
        csrf = self.tokens.csrf_token(cookie) if session else ""
        self._html(
            handler,
            status,
            templates.error_page(
                title=title,
                message=message,
                hint=hint,
                csrf=csrf,
                show_logout=show_logout and bool(session),
            ),
        )


# ---------------------------------------------------------------------------
# request helpers
# ---------------------------------------------------------------------------
def _cookies(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    jar = SimpleCookie()
    try:
        jar.load(handler.headers.get("Cookie", ""))
    except Exception:
        return {}
    return {name: morsel.value for name, morsel in jar.items()}


def _client_ip(handler: BaseHTTPRequestHandler, config: Config) -> str:
    # Behind Caddy/Traefik the first X-Forwarded-For entry is the browser.  If
    # the portal is exposed directly, trusting the header would let a client
    # pick its own rate-limit bucket, so MSM_TRUST_PROXY=0 disables it.
    if config.trust_proxy:
        forwarded = handler.headers.get("X-Forwarded-For", "")
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
            if candidate and len(candidate) <= 64:
                return candidate
    return str(handler.client_address[0]) if handler.client_address else "unknown"


def _read_form_values(handler: BaseHTTPRequestHandler) -> dict[str, list[str]]:
    """Parse the URL-encoded body once, keeping every repeated field."""
    length_text = handler.headers.get("Content-Length")
    if not length_text:
        raise ValueError("missing content length")
    try:
        length = int(length_text)
    except ValueError as exc:
        raise ValueError("invalid content length") from exc
    if length < 0 or length > MAX_FORM_BYTES:
        raise ValueError("form too large")
    body = handler.rfile.read(length)
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError:
        decoded = body.decode("utf-8", "replace")
    return parse_qs(decoded, keep_blank_values=True, max_num_fields=400)


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    return {key: values[-1] for key, values in _read_form_values(handler).items()}
