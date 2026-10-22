from __future__ import annotations

import http.client
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from app.config import Config
from app.db import Database
from app.provisioner import ProvisionError, ProvisionResult
from app.security import RateLimiter, TokenService
from app.web import WebApp

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')


class FakeGateway:
    def handles(self, path: str) -> bool:
        return path == "/mcp"

    def handle(self, handler) -> None:
        body = b'{"ok":true}'
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


class FakeProvisioner:
    def __init__(self, db: Database, tokens: TokenService, instance_id: str = "a" * 16) -> None:
        self.db = db
        self.tokens = tokens
        self.instance_id = instance_id
        self.token = "test-token-abcdefghijklmnopqrstuvwxyz"
        self.calls: list[dict] = []
        self.db.insert_instance(
            {
                "id": instance_id,
                "email": "user@example.com",
                "idem_hash": "idem",
                "token_hash": tokens.hash_mcp_token(self.token),
                "token_salt": "salt",
                "container_name": f"msm-{instance_id}",
                "config_volume": f"msm-config-{instance_id}",
                "data_volume": f"msm-data-{instance_id}",
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "tls_mode": "tls",
                "provider_key": "custom",
                "provider_name": "自定义 IMAP",
                "status": "running",
                "last_error": None,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
        )

    def provision(self, email, password, **kwargs):
        self.calls.append({"email": email, "password": password, **kwargs})
        instance = self.db.get_instance(self.instance_id)
        return ProvisionResult(
            instance=instance,
            token=self.token,
            reused=len(self.calls) > 1,
            rotated=False,
        )


class RaisingProvisioner:
    def __init__(self, error: ProvisionError) -> None:
        self.error = error
        self.calls = 0

    def provision(self, *args, **kwargs):
        self.calls += 1
        raise self.error


class Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        all_headers = dict(headers or {})
        if self.cookies:
            all_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=body, headers=all_headers)
        response = connection.getresponse()
        data = response.read()
        for header, value in response.getheaders():
            if header.lower() == "set-cookie":
                pair = value.split(";", 1)[0]
                if "=" in pair:
                    name, cookie_value = pair.split("=", 1)
                    if "Max-Age=0" in value:
                        self.cookies.pop(name, None)
                    else:
                        self.cookies[name] = cookie_value
        status = response.status
        response_headers = {k.lower(): v for k, v in response.getheaders()}
        connection.close()
        return status, response_headers, data

    def get(self, path: str):
        return self.request("GET", path)

    def post_form(self, path: str, fields: dict[str, str], headers: dict | None = None):
        body = urlencode(fields).encode("utf-8")
        merged = {"Content-Type": "application/x-www-form-urlencoded"}
        merged.update(headers or {})
        return self.request("POST", path, body=body, headers=merged)


class WebAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "MSM_DATA_DIR": self.tmp.name,
                "MSM_PUBLIC_URL": "http://localhost:8080",
                "MSM_SECURE_COOKIES": "0",
                "MSM_VERIFY_BACKEND": "0",
                "MSM_LISTEN_PORT": "0",
                "MSM_BACKEND_PORT": "1",
                "MSM_MAX_INSTANCES": "5",
            },
            clear=True,
        )
        self.env_patch.start()
        self.config = Config.from_env()
        self.db = Database(Path(self.tmp.name) / "control.db")
        self.tokens = TokenService(self.config.master_key)
        self.provisioner = FakeProvisioner(self.db, self.tokens)
        self.web = WebApp(
            self.config,
            self.db,
            self.tokens,
            self.provisioner,
            FakeGateway(),
            RateLimiter(),
        )
        self.server = self.web.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.client = Client(self.port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.db.close()
        self.env_patch.stop()
        self.tmp.cleanup()

    def _make_invite(self, code: str = "msm_test_code", uses: int = 1) -> str:
        return self.db.create_invite(self.tokens.hash_invite(code), max_uses=uses)

    def _enter(self, code: str = "msm_test_code") -> None:
        status, headers, _ = self.client.post_form("/invite", {"invite": code})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/provision")
        self.assertIn("msm_session", self.client.cookies)

    def _csrf(self) -> str:
        status, _, data = self.client.get("/provision")
        self.assertEqual(status, 200)
        match = CSRF_RE.search(data.decode("utf-8"))
        self.assertIsNotNone(match)
        return match.group(1)

    def test_full_flow_and_reuse(self) -> None:
        self._make_invite()
        status, _, data = self.client.get("/")
        self.assertEqual(status, 200)
        self.assertIn("输入邀请码", data.decode("utf-8"))

        status, headers, _ = self.client.post_form("/invite", {"invite": "wrong"})
        self.assertEqual(status, 403)

        self._enter()
        csrf = self._csrf()
        status, headers, _ = self.client.post_form(
            "/provision",
            {
                "csrf": csrf,
                "email": "user@example.com",
                "password": "app-password",
                "host": "",
                "port": "",
                "tls_mode": "tls",
                "provider": "aliyun-enterprise",
            },
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], f"/instance/{self.provisioner.instance_id}")
        self.assertIn("msm_flash", self.client.cookies)
        self.assertEqual(self.provisioner.calls[0]["provider_key"], "aliyun-enterprise")

        status, _, data = self.client.get(headers["location"])
        self.assertEqual(status, 200)
        text = data.decode("utf-8")
        self.assertIn(self.provisioner.token, text)
        self.assertIn("http://localhost:8080/mcp", text)
        self.assertIn("Claude / Cursor", text)
        self.assertIn("已就绪", text)

        # Without the short-lived flash cookie the token is not rendered.
        del self.client.cookies["msm_flash"]
        status, _, data = self.client.get(headers["location"])
        self.assertEqual(status, 200)
        self.assertNotIn(self.provisioner.token, data.decode("utf-8"))
        self.assertIn("重新获取 MCP 配置", data.decode("utf-8"))

        # Re-submitting the same credentials hits the idempotent path.
        csrf = self._csrf()
        status, headers, _ = self.client.post_form(
            "/provision",
            {"csrf": csrf, "email": "user@example.com", "password": "app-password"},
        )
        self.assertEqual(status, 303)
        status, _, data = self.client.get(headers["location"])
        self.assertIn("已找到现有实例", data.decode("utf-8"))
        self.assertEqual(len(self.provisioner.calls), 2)

    def test_provision_requires_csrf(self) -> None:
        self._make_invite()
        self._enter()
        status, _, _ = self.client.post_form(
            "/provision",
            {"email": "user@example.com", "password": "pw"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.provisioner.calls, [])

    def test_origin_is_checked(self) -> None:
        self._make_invite()
        self._enter()
        csrf = self._csrf()
        status, _, _ = self.client.post_form(
            "/provision",
            {"csrf": csrf, "email": "user@example.com", "password": "pw"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.provisioner.calls, [])

    def test_origin_null_is_accepted(self) -> None:
        # Embedded webviews / sandboxed previews send an opaque origin.
        self._make_invite()
        status, headers, _ = self.client.post_form(
            "/invite",
            {"invite": "msm_test_code"},
            headers={"Origin": "null"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/provision")

    def test_origin_null_cross_site_is_rejected(self) -> None:
        invite_id = self._make_invite()
        status, _, _ = self.client.post_form(
            "/invite",
            {"invite": "msm_test_code"},
            headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(self.db.get_invite(invite_id)["uses"], 0)

    def test_session_required(self) -> None:
        status, headers, _ = self.client.get("/provision")
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/")

    def test_invite_single_use(self) -> None:
        self._make_invite(uses=1)
        self._enter()
        other = Client(self.port)
        status, _, _ = other.post_form("/invite", {"invite": "msm_test_code"})
        self.assertEqual(status, 403)

    def test_provision_error_render_keeps_email(self) -> None:
        self._make_invite()
        self._enter()
        csrf = self._csrf()
        self.web.provisioner = RaisingProvisioner(
            ProvisionError("授权码无效", field="password", code="imap_auth", hint="请使用授权码")
        )
        status, _, data = self.client.post_form(
            "/provision",
            {"csrf": csrf, "email": "user@example.com", "password": "secret-pw"},
        )
        self.assertEqual(status, 400)
        text = data.decode("utf-8")
        self.assertIn("授权码无效", text)
        self.assertIn("user@example.com", text)
        self.assertNotIn("secret-pw", text)

    def test_instance_page_is_owner_only(self) -> None:
        self._make_invite()
        self._enter()
        session = self.tokens.parse_session(self.client.cookies["msm_session"])
        instance_id = self.provisioner.instance_id

        self.db.set_instance_owner(instance_id, "other-invite")
        status, _, _ = self.client.get(f"/instance/{instance_id}")
        self.assertEqual(status, 404)

        self.db.set_instance_owner(instance_id, session["invite"])
        status, _, data = self.client.get(f"/instance/{instance_id}")
        self.assertEqual(status, 200)
        self.assertIn("实例", data.decode("utf-8"))

    def test_gateway_path_bypasses_session(self) -> None:
        status, _, data = self.client.get("/mcp")
        self.assertEqual(status, 200)
        self.assertEqual(data, b'{"ok":true}')

    def test_healthz(self) -> None:
        status, headers, data = self.client.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertIn(b'"ok":true', data)

    def test_static_and_security_headers(self) -> None:
        status, headers, data = self.client.get("/static/app.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["content-type"])
        self.assertGreater(len(data), 100)

        status, headers, _ = self.client.get("/")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertEqual(headers["cache-control"], "no-store")

    def test_logout_clears_cookie(self) -> None:
        self._make_invite()
        self._enter()
        csrf = self._csrf()
        status, headers, _ = self.client.post_form("/logout", {"csrf": csrf})
        self.assertEqual(status, 303)
        self.assertNotIn("msm_session", self.client.cookies)
        status, headers, _ = self.client.get("/provision")
        self.assertEqual(status, 303)


if __name__ == "__main__":
    unittest.main()
