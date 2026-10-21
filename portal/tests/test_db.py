from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.db import Database


def instance_record(**overrides):
    base = {
        "id": "abc123",
        "email": "user@example.com",
        "idem_hash": "idem",
        "token_hash": "tokenhash",
        "token_salt": "salt",
        "container_name": "msm-abc123",
        "config_volume": "msm-config-abc123",
        "data_volume": "msm-data-abc123",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "tls_mode": "tls",
        "provider_key": "custom",
        "provider_name": "自定义 IMAP",
        "status": "provisioning",
        "last_error": None,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    base.update(overrides)
    return base


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "control.db")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()

    def test_invite_single_use(self) -> None:
        invite_id = self.db.create_invite("hash1", label="test", max_uses=1)
        invite = self.db.consume_invite("hash1")
        self.assertIsNotNone(invite)
        self.assertEqual(invite["id"], invite_id)
        self.assertEqual(invite["uses"], 1)
        self.assertIsNone(self.db.consume_invite("hash1"))

    def test_invite_disabled_and_expired(self) -> None:
        self.db.create_invite("hash2", max_uses=5)
        self.assertTrue(self.db.disable_invite(self.db.list_invites()[0]["id"]))
        self.assertIsNone(self.db.consume_invite("hash2"))

        self.db.create_invite("hash3", max_uses=5, expires_at=int(time.time()) - 1)
        self.assertIsNone(self.db.consume_invite("hash3"))

    def test_invite_active_check(self) -> None:
        invite_id = self.db.create_invite("hash4", max_uses=1)
        self.assertTrue(self.db.invite_is_active(invite_id))
        self.db.disable_invite(invite_id)
        self.assertFalse(self.db.invite_is_active(invite_id))

    def test_instance_crud(self) -> None:
        self.db.insert_instance(instance_record())
        self.assertEqual(self.db.get_instance_by_email("user@example.com")["id"], "abc123")
        self.assertEqual(self.db.get_instance_by_token_hash("tokenhash")["id"], "abc123")
        self.assertEqual(self.db.count_active_instances(), 1)
        self.assertEqual(self.db.get_instance("abc123")["host_port"], 0)
        self.db.set_instance_host_port("abc123", 43210)
        self.assertEqual(self.db.get_instance("abc123")["host_port"], 43210)

        self.db.set_instance_status("abc123", "running")
        self.assertEqual(self.db.get_instance("abc123")["status"], "running")

        self.db.update_instance_credentials(
            "abc123",
            idem_hash="new-idem",
            token_hash="new-token",
            token_salt="new-salt",
            imap_host="imap.new.example",
            imap_port=143,
            tls_mode="starttls",
            provider_key="custom",
            provider_name="自定义",
        )
        updated = self.db.get_instance("abc123")
        self.assertEqual(updated["idem_hash"], "new-idem")
        self.assertEqual(updated["token_hash"], "new-token")
        self.assertEqual(updated["token_salt"], "new-salt")
        self.assertEqual(updated["status"], "provisioning")

        self.db.mark_instance_removed("abc123")
        self.assertEqual(self.db.get_instance("abc123")["status"], "removed")
        self.assertEqual(self.db.count_active_instances(), 0)

    def test_audit_log(self) -> None:
        self.db.audit("test", ip="127.0.0.1", email="a@b.com", detail={"x": 1})
        with self.db._lock:  # noqa: SLF001 - inspect test-only row
            row = self.db._conn.execute("SELECT action, detail FROM audit_log").fetchone()  # noqa: SLF001
        self.assertEqual(row["action"], "test")
        self.assertIn('"x": 1', row["detail"])

    def test_unique_email_and_token(self) -> None:
        self.db.insert_instance(instance_record())
        with self.assertRaises(Exception):
            self.db.insert_instance(instance_record(id="other"))


if __name__ == "__main__":
    unittest.main()
