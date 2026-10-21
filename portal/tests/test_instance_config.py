from __future__ import annotations

import json
import unittest

from app.instance_config import _quote, render_instance_config
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
    def test_render_contains_required_fields(self) -> None:
        text = render_instance_config(
            email="user@example.com",
            settings=settings(),
            password="s3cret",
            bearer_token="tok_en",
        )
        self.assertIn('path: "/data/mail.db"', text)
        self.assertIn('http_addr: ":8080"', text)
        self.assertIn('http_bearer_token: "tok_en"', text)
        self.assertIn('id: "user@example.com"', text)
        self.assertIn('password: "s3cret"', text)
        self.assertIn('tls_mode: "tls"', text)
        self.assertIn('idle_folders: ["INBOX"]', text)
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
