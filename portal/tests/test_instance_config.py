from __future__ import annotations

import json
import unittest

from app.instance_config import _quote, parse_instance_config, render_instance_config
from app.providers import ImapSettings


def settings(**overrides):
    base = dict(
        host="imap.example.com",
        port=993,
        tls_mode="tls",
        username="user@example.com",
        provider_key="custom",
        provider_name="自定义 IMAP",
        hint="",
    )
    base.update(overrides)
    return ImapSettings(**base)


class InstanceConfigTest(unittest.TestCase):
    def _tmpdir(self) -> str:
        import tempfile
        self.addCleanup(lambda: None)
        return tempfile.mkdtemp()

    def test_render_contains_required_fields(self) -> None:
        text = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password="s3cret",
            bearer_token="tok_en",
            sync_jitter_sec=1800,
        )
        self.assertIn('path: "/data/mail.db"', text)
        self.assertIn('http_addr: ":8080"', text)
        self.assertIn('http_bearer_token: "tok_en"', text)
        self.assertIn('id: "user@example.com"', text)
        self.assertIn('password: "s3cret"', text)
        self.assertIn('tls_mode: "tls"', text)
        self.assertIn('idle_folders: ["INBOX"]', text)
        self.assertIn("sync_jitter_sec: 1800", text)
        self.assertTrue(text.endswith("\n"))

    def test_quote_handles_quotes_backslashes_and_unicode(self) -> None:
        value = 'p"a\\ss 中文\ttab'
        quoted = _quote(value)
        self.assertEqual(json.loads(quoted), value)
        text = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password=value,
            bearer_token="tok",
        )
        self.assertIn("p\\\"a\\\\ss", text)

    def test_folder_restriction_defaults_to_inbox(self) -> None:
        text = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password="pw",
            bearer_token="tok",
        )
        self.assertIn('folders: ["INBOX"]', text)
        self.assertIn('idle_folders: ["INBOX"]', text)

    def test_custom_folders_and_empty_means_all(self) -> None:
        text = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password="pw",
            bearer_token="tok",
            folders=("INBOX", "Sent Items"),
        )
        self.assertIn('folders: ["INBOX", "Sent Items"]', text)
        self.assertIn('idle_folders: ["INBOX"]', text)

        all_folders = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password="pw",
            bearer_token="tok",
            folders=(),
        )
        self.assertNotIn("folders:", all_folders)
        self.assertNotIn("idle_folders:", all_folders)

    def test_all_folders_env_token(self) -> None:
        import os
        from unittest import mock
        from app.config import Config

        with mock.patch.dict(os.environ, {"MSM_DATA_DIR": self._tmpdir(), "MSM_SYNC_FOLDERS": "ALL"}, clear=True):
            self.assertEqual(Config.from_env().sync_folders, ())
        with mock.patch.dict(os.environ, {"MSM_DATA_DIR": self._tmpdir(), "MSM_SYNC_FOLDERS": "INBOX,Sent"}, clear=True):
            self.assertEqual(Config.from_env().sync_folders, ("INBOX", "Sent"))

    def test_trash_folder_is_optional(self) -> None:
        text = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password="pw",
            bearer_token="tok",
        )
        self.assertNotIn("trash_folder:", text)
        text = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password="pw",
            bearer_token="tok",
            trash_folder="llm_delete",
        )
        self.assertIn('trash_folder: "llm_delete"', text)

    def test_parse_reads_back_password_and_token(self) -> None:
        password = 'p"a\\ss 中文 # not-a-comment'
        text = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password=password,
            bearer_token="tok_en",
            sync_interval_min=15,
            tls_skip_verify=True,
        )
        parsed = parse_instance_config(text)
        self.assertEqual(parsed["password"], password)
        self.assertEqual(parsed["bearer_token"], "tok_en")
        self.assertEqual(parsed["sync_interval_min"], 15)
        self.assertTrue(parsed["tls_skip_verify"])

    def test_custom_port_and_starttls(self) -> None:
        text = render_instance_config(
            email="user@example.com",
            settings=settings(host="mail.internal", port=143, tls_mode="starttls"),
            password="pw",
            bearer_token="tok",
            http_port=18080,
        )
        self.assertIn('host: "mail.internal"', text)
        self.assertIn("port: 143", text)
        self.assertIn('tls_mode: "starttls"', text)
        self.assertIn('http_addr: ":18080"', text)


if __name__ == "__main__":
    unittest.main()
