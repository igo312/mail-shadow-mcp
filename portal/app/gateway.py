"""Bearer-token routed reverse proxy for all MCP instances.

Every user gets the *same* public endpoint (``https://host/mcp``) and a unique
bearer token.  The gateway hashes the token, looks up the instance, and proxies
the request to that instance's container over the private Docker network.  No
per-user host port is ever published.
"""

from __future__ import annotations

import http.client
import logging
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .config import Config
from .db import Database
from .docker_cli import DockerClient, DockerError
from .security import RateLimiter, TokenService

log = logging.getLogger(__name__)

MAX_REQUEST_BODY = 4 * 1024 * 1024
_HOP_BY_HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
}
_RESPONSE_SKIP = _HOP_BY_HOP | {"content-length", "date", "server", "set-cookie"}


class Gateway:
    def __init__(
        self,
        config: Config,
        db: Database,
        docker: DockerClient,
        tokens: TokenService,
        limiter: RateLimiter,
    ) -> None:
        self.config = config
        self.db = db
        self.docker = docker
        self.tokens = tokens
        self.limiter = limiter
        self._touch_lock = threading.Lock()
        self._last_touch: dict[str, float] = {}
        self._ip_cache: dict[str, tuple[str, float]] = {}
        self._ip_cache_lock = threading.Lock()

    def handles(self, path: str) -> bool:
        return urlsplit(path).path in {"/mcp", "/mcp/"}

    def handle(self, handler: Any) -> None:
        ip = _client_ip(handler, self.config.trust_proxy)
        token = _bearer_token(handler.headers.get("Authorization", ""))
        if not token:
            self._unauthorized(handler)
            return

        token_hash = self.tokens.hash_mcp_token(token)
        instance = self.db.get_instance_by_token_hash(token_hash)
        if not instance:
            decision = self.limiter.check(
                f"gateway-fail:{ip}",
                self.config.gateway_rate_limit,
                self.config.gateway_rate_window,
            )
            if not decision.allowed:
                self._error(handler, 429, "too_many_requests", retry_after=decision.retry_after)
                return
            log.info("gateway rejected unknown token from %s", ip)
            self._unauthorized(handler)
            return

        if instance["status"] != "running":
            self._error(handler, 503, "instance_not_running")
            return

        try:
            target = self._backend_target(instance)
        except DockerError as exc:
            log.warning("cannot resolve backend for %s: %s", instance["id"], exc)
            self._error(handler, 502, "backend_unavailable")
            return
        if not target:
            self._error(handler, 502, "backend_unavailable")
            return
        target_host, target_port = target

        try:
            body = _read_request_body(handler)
        except ValueError as exc:
            self._error(handler, 413, str(exc))
            return

        headers: dict[str, str] = {}
        for key, value in handler.headers.items():
            lowered = key.lower()
            if lowered in _HOP_BY_HOP or lowered in {
                "host",
                "content-length",
                "cookie",
                "expect",
            }:
                continue
            headers[key] = value
        headers["X-Forwarded-For"] = ip
        headers["X-Forwarded-Proto"] = urlsplit(self.config.public_url).scheme
        headers["Authorization"] = f"Bearer {token}"

        started = time.monotonic()
        connection: http.client.HTTPConnection | None = None
        headers_sent = False
        try:
            connection = http.client.HTTPConnection(target_host, target_port, timeout=30)
            connection.request(handler.command, "/mcp", body=body, headers=headers)
            response = connection.getresponse()
            # Server-sent events (and long-lived StreamableHTTP streams) must
            # not inherit the 30-second header timeout once the stream starts.
            if connection.sock is not None:
                connection.sock.settimeout(None)

            handler.send_response(response.status, response.reason)
            headers_sent = True
            for key, value in response.getheaders():
                if key.lower() in _RESPONSE_SKIP:
                    continue
                handler.send_header(key, value)
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.close_connection = True

            if handler.command != "HEAD":
                while True:
                    # read1 (not read) is essential for SSE: read(n) would wait
                    # for n bytes or EOF and buffer the whole event stream.
                    chunk = response.read1(8192)
                    if not chunk:
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
            log.info(
                "gateway %s %s -> instance=%s status=%s duration_ms=%d",
                handler.command,
                "/mcp",
                instance["id"],
                response.status,
                int((time.monotonic() - started) * 1000),
            )
            self._touch(instance["id"])
        except (BrokenPipeError, ConnectionResetError):
            handler.close_connection = True
        except (OSError, http.client.HTTPException) as exc:
            log.warning("gateway upstream error for %s: %s", instance["id"], exc)
            if not headers_sent and not handler.wfile.closed:
                try:
                    self._error(handler, 502, "backend_unavailable")
                except (BrokenPipeError, OSError):
                    pass
        finally:
            if connection is not None:
                connection.close()

    # -- helpers ---------------------------------------------------------
    def _backend_target(self, instance: dict[str, Any]) -> tuple[str, int] | None:
        if self.config.backend_mode == "network":
            return str(instance["container_name"]), self.config.backend_port
        if self.config.backend_mode == "port":
            host_port = int(instance.get("host_port") or 0)
            if not host_port:
                return None
            return "127.0.0.1", host_port

        name = str(instance["container_name"])
        now = time.monotonic()
        with self._ip_cache_lock:
            cached = self._ip_cache.get(name)
            if cached and cached[1] > now:
                return cached[0], self.config.backend_port
        ip = self.docker.container_ip(name, self.config.docker_network) or ""
        if ip:
            with self._ip_cache_lock:
                self._ip_cache[name] = (ip, now + 30)
            return ip, self.config.backend_port
        return None

    def _touch(self, instance_id: str) -> None:
        now = time.monotonic()
        with self._touch_lock:
            last = self._last_touch.get(instance_id, 0.0)
            if now - last < 60:
                return
            self._last_touch[instance_id] = now
        try:
            self.db.touch_instance(instance_id)
        except Exception:  # never fail a proxied request because of telemetry
            log.debug("could not touch instance %s", instance_id, exc_info=True)

    def _unauthorized(self, handler: Any) -> None:
        handler.send_response(401)
        handler.send_header("WWW-Authenticate", 'Bearer realm="mail-shadow-mcp"')
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        body = b'{"error":"unauthorized"}'
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.close_connection = True
        handler.wfile.write(body)

    def _error(self, handler: Any, status: int, code: str, *, retry_after: int = 0) -> None:
        body = ('{"error":"%s"}' % code).encode("ascii")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        if retry_after:
            handler.send_header("Retry-After", str(retry_after))
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.close_connection = True
        handler.wfile.write(body)


def _bearer_token(header: str) -> str:
    if not header:
        return ""
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    token = parts[1].strip()
    if not token or len(token) > 512 or any(ch.isspace() for ch in token):
        return ""
    return token


def _client_ip(handler: Any, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = handler.headers.get("X-Forwarded-For", "")
        if forwarded:
            candidate = forwarded.split(",", 1)[0].strip()
            if candidate and len(candidate) <= 64:
                return candidate
    return str(handler.client_address[0]) if handler.client_address else "unknown"


def _read_request_body(handler: Any) -> bytes:
    transfer_encoding = handler.headers.get("Transfer-Encoding", "").lower()
    if transfer_encoding and handler.headers.get("Content-Length"):
        raise ValueError("ambiguous_request_body")
    if "chunked" in transfer_encoding:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = handler.rfile.readline(1024)
            if not line:
                raise ValueError("incomplete chunked body")
            size_text = line.split(b";", 1)[0].strip()
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise ValueError("invalid chunk size") from exc
            if size == 0:
                while True:
                    trailer = handler.rfile.readline(1024)
                    if trailer in {b"\r\n", b"\n", b""}:
                        break
                break
            total += size
            if total > MAX_REQUEST_BODY:
                raise ValueError("request_body_too_large")
            data = handler.rfile.read(size)
            if len(data) != size:
                raise ValueError("incomplete chunked body")
            handler.rfile.read(2)  # trailing CRLF
            chunks.append(data)
        return b"".join(chunks)

    length_text = handler.headers.get("Content-Length")
    if not length_text:
        return b""
    try:
        length = int(length_text)
    except ValueError as exc:
        raise ValueError("invalid_content_length") from exc
    if length < 0 or length > MAX_REQUEST_BODY:
        raise ValueError("request_body_too_large")
    return handler.rfile.read(length)
