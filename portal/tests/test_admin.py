"""Tests for the /admin console: gate, dashboard, actions, invites, audit."""

from __future__ import annotations

import dataclasses
import http.client
import json
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
from app.security import RateLimiter, TokenService
from app.stats import empty_entry
from app.web import WebApp

CSRF_RE = re.compile(r'name="csrf" value="([^"]+)"')
ADMIN_PASSWORD = "Zeme123@"
INSTANCE_ID = "a" * 16

BASE_ENV = {
    "MSM_PUBLIC_URL": "http://localhost:8080",
    "MSM_SECURE_COOKIES": "0",
    "MSM_VERIFY_BACKEND": "0",
    "MSM_LISTEN_PORT": "0",
    "MSM_BACKEND_PORT": "1",
    "MSM_MAX_INSTANCES": "5",
    "MSM_ADMIN_PASSWORD": ADMIN_PASSWORD,
    "MSM_ADMIN_RATE_LIMIT": "8",
    "MSM_ADMIN_RATE_WINDOW": "900",
}


class FakeGateway:
    def handle(self, handler) -> None:  # pragma: no cover - not used here
        handler.send_response(204)
        handler.end_headers()


class FakeProvisioner:
    def __init__(self, db: Database, tokens: TokenService, instance_id: str = INSTANCE_ID) -> None:
        self.db = db
        self.tokens = tokens
        self.instance_id = instance_id
        self.docker = None
        self.token = "test-token-abcdefghijklmnopqrstuvwxyz"
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


class FakeStats:
    def __init__(self) -> None:
        self.data: dict = {"generated_at": 0, "instances": {}, "host": {}, "error": ""}
        self.refreshes = 0

    def snapshot(self) -> dict:
        return json.loads(json.dumps(self.data))

    def refresh(self) -> None:
        self.refreshes += 1


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.running: set[str] = set()
        self.fail_message: str = ""

    def _record(self, action: str, name: str) -> None:
        if self.fail_message:
            raise RuntimeError(self.fail_message)
        self.calls.append((action, name))

    def start_container(self, name: str) -> None:
        self._record("start", name)
        self.running.add(name)

    def stop_container(self, name: str) -> None:
        self._record("stop", name)
        self.running.discard(name)

    def restart_container(self, name: str) -> None:
        self._record("restart", name)
        self.running.add(name)

    def remove_container(self, name: str) -> None:
        self._record("remove_container", name)
        self.running.discard(name)

    def remove_volume(self, name: str) -> None:
        self._record("remove_volume", name)

    def container_running(self, name: str) -> bool:
        return name in self.running

    def logs(self, name: str, *, tail: int = 60) -> str:
        return "sync ok\nsync failed: timeout"

    def list_managed_containers(self):
        return []

    def container_stats(self):
        return {}


class Client:
    def __init__(self, port: int) -> None:
        self.port = port
        self.cookies: dict[str, str] = {}

    def request(self, method, path, body=None, headers=None):
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

    def get(self, path):
        return self.request("GET", path)

    def post_form(self, path, fields, headers=None):
        body = urlencode(fields).encode("utf-8")
        merged = {"Content-Type": "application/x-www-form-urlencoded"}
        merged.update(headers or {})
        return self.request("POST", path, body=body, headers=merged)


class AdminTest(unittest.TestCase):
    env_overrides: dict[str, str] = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        env = {"MSM_DATA_DIR": self.tmp.name, **BASE_ENV, **self.env_overrides}
        self.env_patch = mock.patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.config = Config.from_env()
        self.db = Database(Path(self.tmp.name) / "control.db")
        self.tokens = TokenService(self.config.master_key)
        self.provisioner = FakeProvisioner(self.db, self.tokens)
        self.stats = FakeStats()
        self.docker = FakeDocker()
        self.web = WebApp(
            self.config,
            self.db,
            self.tokens,
            self.provisioner,
            FakeGateway(),
            RateLimiter(),
            stats=self.stats,
            docker=self.docker,
        )
        self.server = self.web.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.client = Client(self.port)
        self._seed_snapshot()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.db.close()
        self.env_patch.stop()
        self.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def _seed_snapshot(self) -> None:
        instance = self.db.get_instance(INSTANCE_ID)
        entry = empty_entry(instance)
        entry.update(
            {
                "ok": True,
                "messages": 477967,
                "folder_count": 3,
                "pending": 12,
                "total_bytes": 25 * 1024**3,
                "db_bytes": 20 * 1024**3,
                "wal_bytes": 1024,
                "attachment_disk_bytes": 5 * 1024**3,
                "newest_mail": "2026-10-27T02:00:00Z",
                "container_state": "running",
                "container_status": "Up 3 hours",
                "image": "mail-shadow-mcp:fix6",
                "folders": [
                    {"name": "INBOX", "messages": 400000, "newest_uid": 500, "synced_uid": 488,
                     "pending": 12, "cjk_done": True},
                    {"name": "已发送", "messages": 70000, "newest_uid": 90, "synced_uid": 90,
                     "pending": 0, "cjk_done": True},
                    {"name": "shipserv", "messages": 7967, "newest_uid": 70, "synced_uid": 70,
                     "pending": 0, "cjk_done": False},
                ],
                "configured": {"source": "instance", "all": False,
                               "names": ["INBOX", "已发送", "shipserv"]},
                "log_error": 'msg="sync failed" account=user@example.com err="AUTHENTICATIONFAILED"',
            }
        )
        self.stats.data = {
            "generated_at": int(time.time()),
            "instances": {INSTANCE_ID: entry},
            "host": {"load1": 0.17, "mem_total": 8 * 1024**3, "mem_available": 4 * 1024**3,
                     "disk_free": 300 * 1024**3, "cpu_count": 4},
            "error": "",
        }

    def _login(self) -> str:
        status, headers, _ = self.client.post_form("/admin/login", {"password": ADMIN_PASSWORD})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"], "/admin")
        self.assertIn("msm_admin", self.client.cookies)
        _, _, data = self.client.get("/admin")
        match = CSRF_RE.search(data.decode("utf-8"))
        self.assertIsNotNone(match)
        return match.group(1)

    # -- gate ------------------------------------------------------------
    def test_admin_requires_login(self) -> None:
        status, _, data = self.client.get("/admin")
        self.assertEqual(status, 200)
        self.assertIn("管理登录", data.decode("utf-8"))
        self.assertNotIn("msm_admin", self.client.cookies)

    def test_wrong_password_is_rejected_and_audited(self) -> None:
        status, _, data = self.client.post_form("/admin/login", {"password": "nope"})
        self.assertEqual(status, 401)
        self.assertIn("密码不正确", data.decode("utf-8"))
        self.assertNotIn("msm_admin", self.client.cookies)
        actions = [row["action"] for row in self.db.recent_audit(10)]
        self.assertIn("admin_login_failed", actions)

    def test_login_sets_cookie_and_audits(self) -> None:
        self._login()
        actions = [row["action"] for row in self.db.recent_audit(10)]
        self.assertIn("admin_login", actions)

    def test_admin_pages_require_cookie(self) -> None:
        for path in ("/admin/invites", "/admin/audit", f"/admin/instance/{INSTANCE_ID}"):
            status, _, data = self.client.get(path)
            self.assertEqual(status, 200, path)
            self.assertIn("管理登录", data.decode("utf-8"), path)

    def test_login_rate_limit(self) -> None:
        config = dataclasses.replace(self.config, admin_rate_limit=1)
        web = WebApp(
            config, self.db, self.tokens, self.provisioner, FakeGateway(), RateLimiter(),
            stats=self.stats, docker=self.docker,
        )
        server = web.create_server()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = Client(server.server_address[1])
            first, _, _ = client.post_form("/admin/login", {"password": "bad"})
            second, _, _ = client.post_form("/admin/login", {"password": "bad"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(first, 401)
        self.assertEqual(second, 429)

    def test_logout_clears_cookie(self) -> None:
        csrf = self._login()
        status, headers, _ = self.client.post_form("/admin/logout", {"csrf": csrf})
        self.assertEqual(status, 303)
        self.assertNotIn("msm_admin", self.client.cookies)

    def test_logout_requires_csrf(self) -> None:
        self._login()
        status, _, _ = self.client.post_form("/admin/logout", {})
        self.assertEqual(status, 400)
        self.assertIn("msm_admin", self.client.cookies)

    # -- dashboard -------------------------------------------------------
    def test_dashboard_shows_mailbox_folders_and_volume(self) -> None:
        self._login()
        status, _, data = self.client.get("/admin")
        self.assertEqual(status, 200)
        text = data.decode("utf-8")
        self.assertIn("user@example.com", text)
        self.assertIn("INBOX", text)
        self.assertIn("已发送", text)
        self.assertIn("shipserv", text)
        self.assertIn("47.8 万", text)
        self.assertIn("25.0 GB", text)
        self.assertIn("运行中", text)

    def test_dashboard_escapes_email(self) -> None:
        self.db.insert_instance(
            {
                "id": "e" * 16,
                "email": "<script>@example.com",
                "idem_hash": "idem2",
                "token_hash": self.tokens.hash_mcp_token("other-token"),
                "token_salt": "salt",
                "container_name": "msm-eeee",
                "config_volume": "msm-config-eeee",
                "data_volume": "msm-data-eeee",
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "tls_mode": "tls",
                "provider_key": "custom",
                "provider_name": "自定义 IMAP",
                "status": "failed",
                "last_error": "boom",
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
        )
        entry = empty_entry(self.db.get_instance("e" * 16))
        entry["ok"] = False
        entry["error"] = "<b>bad</b>"
        self.stats.data["instances"]["e" * 16] = entry
        self._login()
        _, _, data = self.client.get("/admin")
        text = data.decode("utf-8")
        self.assertNotIn("<script>@example.com", text)
        self.assertIn("&lt;script&gt;@example.com", text)
        self.assertNotIn("<b>bad</b>", text)

    def test_instance_page_lists_folders_and_logs(self) -> None:
        self._login()
        status, _, data = self.client.get(f"/admin/instance/{INSTANCE_ID}")
        self.assertEqual(status, 200)
        text = data.decode("utf-8")
        self.assertIn("同步文件夹明细", text)
        self.assertIn("INBOX", text)
        self.assertIn("补全中", text)  # shipserv has cjk_done=False
        self.assertIn("sync failed: timeout", text)
        self.assertIn("容器最近报错", text)
        self.assertIn("永久删除", text)

    def test_instance_page_without_snapshot_still_renders(self) -> None:
        self.stats.data = {"generated_at": 0, "instances": {}, "host": {}, "error": ""}
        self._login()
        status, _, data = self.client.get(f"/admin/instance/{INSTANCE_ID}")
        self.assertEqual(status, 200)
        self.assertIn("user@example.com", data.decode("utf-8"))

    def test_unknown_instance_is_404(self) -> None:
        self._login()
        status, _, _ = self.client.get("/admin/instance/" + "f" * 16)
        self.assertEqual(status, 404)
        status, _, _ = self.client.get("/admin/instance/not-a-hex-id")
        self.assertEqual(status, 404)

    # -- actions ---------------------------------------------------------
    def test_action_requires_csrf(self) -> None:
        self._login()
        status, _, _ = self.client.post_form(
            "/admin/action", {"instance": INSTANCE_ID, "action": "stop"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.db.get_instance(INSTANCE_ID)["status"], "running")

    def test_stop_and_start(self) -> None:
        csrf = self._login()
        self.docker.running.add(f"msm-{INSTANCE_ID}")
        status, headers, _ = self.client.post_form(
            "/admin/action",
            {"csrf": csrf, "instance": INSTANCE_ID, "action": "stop"},
        )
        self.assertEqual(status, 303)
        self.assertIn(f"/admin/instance/{INSTANCE_ID}", headers["location"])
        self.assertEqual(self.docker.calls, [("stop", f"msm-{INSTANCE_ID}")])
        self.assertEqual(self.db.get_instance(INSTANCE_ID)["status"], "stopped")
        self.assertGreaterEqual(self.stats.refreshes, 1)

        status, _, _ = self.client.post_form(
            "/admin/action",
            {"csrf": csrf, "instance": INSTANCE_ID, "action": "start"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.db.get_instance(INSTANCE_ID)["status"], "running")
        actions = [row["action"] for row in self.db.recent_audit(10)]
        self.assertIn("admin_action", actions)

    def test_restart(self) -> None:
        csrf = self._login()
        status, _, _ = self.client.post_form(
            "/admin/action",
            {"csrf": csrf, "instance": INSTANCE_ID, "action": "restart"},
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.docker.calls, [("restart", f"msm-{INSTANCE_ID}")])

    def test_remove_requires_matching_email(self) -> None:
        csrf = self._login()
        status, headers, _ = self.client.post_form(
            "/admin/action",
            {"csrf": csrf, "instance": INSTANCE_ID, "action": "remove", "confirm": "wrong@x"},
        )
        self.assertEqual(status, 303)
        self.assertIn("kind=error", headers["location"])
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.db.get_instance(INSTANCE_ID)["status"], "running")

        status, headers, _ = self.client.post_form(
            "/admin/action",
            {"csrf": csrf, "instance": INSTANCE_ID, "action": "remove",
             "confirm": "USER@example.com "},
        )
        self.assertEqual(status, 303)
        self.assertIn(("remove_volume", f"msm-config-{INSTANCE_ID}"), self.docker.calls)
        self.assertIn(("remove_volume", f"msm-data-{INSTANCE_ID}"), self.docker.calls)
        self.assertEqual(self.db.get_instance(INSTANCE_ID)["status"], "removed")
        actions = [row["action"] for row in self.db.recent_audit(10)]
        self.assertIn("instance_removed", actions)

    def test_docker_failure_reports_error(self) -> None:
        csrf = self._login()
        self.docker.fail_message = "docker daemon is down"
        status, _, _ = self.client.post_form(
            "/admin/action",
            {"csrf": csrf, "instance": INSTANCE_ID, "action": "stop"},
        )
        self.assertEqual(status, 303)
        actions = [row["action"] for row in self.db.recent_audit(10)]
        self.assertIn("admin_action_failed", actions)
        self.assertEqual(self.db.get_instance(INSTANCE_ID)["status"], "running")

    def test_unknown_action_is_rejected(self) -> None:
        csrf = self._login()
        status, _, _ = self.client.post_form(
            "/admin/action", {"csrf": csrf, "instance": INSTANCE_ID, "action": "explode"}
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.docker.calls, [])

    def test_refresh_triggers_collector(self) -> None:
        csrf = self._login()
        before = self.stats.refreshes
        status, headers, _ = self.client.post_form("/admin/refresh", {"csrf": csrf})
        self.assertEqual(status, 303)
        self.assertEqual(headers["location"].split("?")[0], "/admin")
        self.assertEqual(self.stats.refreshes, before + 1)

    # -- invites ---------------------------------------------------------
    def test_invite_create_shows_code_once(self) -> None:
        csrf = self._login()
        status, _, _ = self.client.post_form(
            "/admin/invites/create", {"csrf": csrf, "label": "张伟", "uses": "2", "days": "30"}
        )
        self.assertEqual(status, 303)
        self.assertIn("msm_admin_flash", self.client.cookies)

        status, _, data = self.client.get("/admin/invites")
        self.assertEqual(status, 200)
        text = data.decode("utf-8")
        self.assertIn("邀请码已创建", text)
        self.assertIn("msm_", text)
        self.assertIn("张伟", text)
        code = re.search(r'id="new-invite-code">(msm_[A-Za-z0-9_-]+)<', text)
        self.assertIsNotNone(code)
        self.assertNotIn("msm_admin_flash", self.client.cookies)

        # The code is not shown a second time.
        _, _, data = self.client.get("/admin/invites")
        self.assertNotIn(code.group(1), data.decode("utf-8"))
        invites = self.db.list_invites()
        self.assertEqual(len(invites), 1)
        self.assertEqual(invites[0]["label"], "张伟")
        self.assertEqual(invites[0]["max_uses"], 2)

    def test_invite_create_without_expiry(self) -> None:
        csrf = self._login()
        self.client.post_form("/admin/invites/create", {"csrf": csrf, "days": "0"})
        invite = self.db.list_invites()[0]
        self.assertIsNone(invite["expires_at"])

    def test_invite_disable(self) -> None:
        csrf = self._login()
        invite_id = self.db.create_invite(self.tokens.hash_invite("msm_x"), max_uses=1)
        status, _, _ = self.client.post_form(
            "/admin/invites/disable", {"csrf": csrf, "invite": invite_id}
        )
        self.assertEqual(status, 303)
        self.assertEqual(self.db.get_invite(invite_id)["disabled"], 1)

    def test_invites_require_csrf(self) -> None:
        self._login()
        status, _, _ = self.client.post_form("/admin/invites/create", {"label": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(self.db.list_invites(), [])

    # -- audit -----------------------------------------------------------
    def test_audit_page_lists_events(self) -> None:
        self._login()
        self.db.audit("provision_error", ip="1.2.3.4", email="a@b.c", detail={"code": "imap_auth"})
        status, _, data = self.client.get("/admin/audit")
        self.assertEqual(status, 200)
        text = data.decode("utf-8")
        self.assertIn("provision_error", text)
        self.assertIn("1.2.3.4", text)
        self.assertIn("admin_login", text)


class AdminDisabledTest(unittest.TestCase):
    """Without MSM_ADMIN_PASSWORD the console must not exist at all."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        env = {key: value for key, value in BASE_ENV.items() if key != "MSM_ADMIN_PASSWORD"}
        env["MSM_DATA_DIR"] = self.tmp.name
        self.env_patch = mock.patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.config = Config.from_env()
        self.db = Database(Path(self.tmp.name) / "control.db")
        self.tokens = TokenService(self.config.master_key)
        provisioner = FakeProvisioner(self.db, self.tokens)
        self.web = WebApp(
            self.config, self.db, self.tokens, provisioner, FakeGateway(), RateLimiter(),
            stats=FakeStats(), docker=FakeDocker(),
        )
        self.server = self.web.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Client(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.db.close()
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_admin_is_404(self) -> None:
        status, _, _ = self.client.get("/admin")
        self.assertEqual(status, 404)

    def test_login_post_is_404(self) -> None:
        status, _, _ = self.client.post_form("/admin/login", {"password": ADMIN_PASSWORD})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
