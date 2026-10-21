from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from app.config import Config
from app.db import Database
from app.gateway import Gateway
from app.security import RateLimiter, TokenService
from app.web import WebApp


class BackendHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: list[dict] = []
    streaming = False

    def do_GET(self):  # noqa: N802
        BackendHandler.seen.append(
            {
                "method": "GET",
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "cookie": self.headers.get("Cookie"),
            }
        )
        if BackendHandler.streaming:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: one\n\n")
            self.wfile.flush()
            time.sleep(1.0)
            self.wfile.write(b"data: two\n\n")
            self.wfile.flush()
            self.close_connection = True
            return
        body = b'{"backend":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        BackendHandler.seen.append(
            {
                "method": "POST",
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": payload.decode("utf-8"),
            }
        )
        body = json.dumps({"echo": payload.decode("utf-8")}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FakeDocker:
    def container_ip(self, name, network):
        return "127.0.0.1"


class DummyProvisioner:
    def provision(self, *args, **kwargs):
        raise AssertionError("should not provision")


class GatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        BackendHandler.seen = []
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), BackendHandler)
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        backend_port = self.backend.server_address[1]

        self.tmp = tempfile.TemporaryDirectory()
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "MSM_DATA_DIR": self.tmp.name,
                "MSM_PUBLIC_URL": "http://localhost:8080",
                "MSM_SECURE_COOKIES": "0",
                "MSM_VERIFY_BACKEND": "0",
                "MSM_LISTEN_PORT": "0",
                "MSM_BACKEND_MODE": "ip",
                "MSM_BACKEND_PORT": str(backend_port),
                "MSM_MAX_INSTANCES": "5",
            },
            clear=True,
        )
        self.env_patch.start()
        self.config = Config.from_env()
        self.db = Database(Path(self.tmp.name) / "control.db")
        self.tokens = TokenService(self.config.master_key)
        self.token = "gateway-token-abcdefghijklmnopqrstuvwxyz"
        self.db.insert_instance(
            {
                "id": "b" * 16,
                "email": "user@example.com",
                "idem_hash": "idem",
                "token_hash": self.tokens.hash_mcp_token(self.token),
                "token_salt": "salt",
                "container_name": "msm-bbbbbbbbbbbbbbbb",
                "config_volume": "msm-config-b",
                "data_volume": "msm-data-b",
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "tls_mode": "tls",
                "provider_key": "custom",
                "provider_name": "自定义",
                "status": "running",
                "last_error": None,
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
        )
        limiter = RateLimiter()
        gateway = Gateway(self.config, self.db, FakeDocker(), self.tokens, limiter)
        self.web = WebApp(
            self.config,
            self.db,
            self.tokens,
            DummyProvisioner(),
            gateway,
            limiter,
        )
        self.server = self.web.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.backend.shutdown()
        self.backend.server_close()
        self.backend_thread.join(timeout=3)
        self.db.close()
        self.env_patch.stop()
        self.tmp.cleanup()

    def _request(self, method: str, path: str = "/mcp", *, token: str | None = None, body: bytes | None = None):
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        status = response.status
        connection.close()
        return status, data

    def test_missing_token_returns_401(self) -> None:
        status, data = self._request("GET")
        self.assertEqual(status, 401)
        self.assertIn(b"unauthorized", data)

    def test_valid_token_is_proxied_with_authorization(self) -> None:
        status, data = self._request("GET", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(data, b'{"backend":"ok"}')
        self.assertEqual(BackendHandler.seen[-1]["path"], "/mcp")
        self.assertEqual(
            BackendHandler.seen[-1]["auth"], f"Bearer {self.token}"
        )

    def test_post_body_is_forwarded(self) -> None:
        payload = b'{"jsonrpc":"2.0","id":1,"method":"initialize"}'
        status, data = self._request("POST", token=self.token, body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(BackendHandler.seen[-1]["body"], payload.decode("utf-8"))
        self.assertIn(b"initialize", data)

    def test_unknown_token_returns_401(self) -> None:
        status, _ = self._request("GET", token="nope")
        self.assertEqual(status, 401)

    def test_non_running_instance_returns_503(self) -> None:
        self.db.set_instance_status("b" * 16, "provisioning")
        status, _ = self._request("GET", token=self.token)
        self.assertEqual(status, 503)

    def test_trailing_slash_is_routed(self) -> None:
        status, _ = self._request("GET", "/mcp/", token=self.token)
        self.assertEqual(status, 200)

    def test_bad_authorization_scheme(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/mcp", headers={"Authorization": f"Basic {self.token}"})
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 401)
        connection.close()

    def test_gateway_does_not_require_session(self) -> None:
        status, _ = self._request("GET", token=self.token)
        self.assertEqual(status, 200)

    def test_portal_session_cookie_is_not_forwarded(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(
            "GET",
            "/mcp",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Cookie": "msm_session=secret-session-cookie",
            },
        )
        response = connection.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        connection.close()
        self.assertIsNone(BackendHandler.seen[-1]["cookie"])

    def test_port_mode_target_uses_loopback_host_port(self) -> None:
        config = replace(self.config, backend_mode="port")
        gateway = Gateway(config, self.db, FakeDocker(), self.tokens, RateLimiter())
        instance = dict(self.db.get_instance("b" * 16))
        instance["host_port"] = 43210
        self.assertEqual(gateway._backend_target(instance), ("127.0.0.1", 43210))  # noqa: SLF001
        instance["host_port"] = 0
        self.assertIsNone(gateway._backend_target(instance))  # noqa: SLF001

    def test_sse_is_streamed_without_waiting_for_eof(self) -> None:
        BackendHandler.streaming = True
        try:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            connection.request("GET", "/mcp", headers={"Authorization": f"Bearer {self.token}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
            started = time.monotonic()
            first = response.read1(1024)
            elapsed = time.monotonic() - started
            self.assertIn(b"data: one", first)
            self.assertNotIn(b"data: two", first)
            # The backend sleeps 1 s before the second event.  Receiving the
            # first event sooner than that proves the gateway did not buffer
            # the whole stream until EOF.
            self.assertLess(elapsed, 0.7)
            rest = response.read()
            self.assertIn(b"data: two", rest)
            connection.close()
        finally:
            BackendHandler.streaming = False


if __name__ == "__main__":
    unittest.main()
