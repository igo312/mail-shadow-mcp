from __future__ import annotations

import unittest

from app.providers import (
    InputError,
    detect_provider,
    normalize_email,
    prepare_password,
    resolve_settings,
)


class ProviderTest(unittest.TestCase):
    def test_known_domains(self) -> None:
        cases = {
            "user@gmail.com": ("gmail", "imap.gmail.com", 993),
            "user@qq.com": ("qq", "imap.qq.com", 993),
            "user@163.com": ("netease", "imap.163.com", 993),
            "user@outlook.com": ("outlook", "outlook.office365.com", 993),
            "user@icloud.com": ("icloud", "imap.mail.me.com", 993),
        }
        for email, (key, host, port) in cases.items():
            settings = resolve_settings(email)
            self.assertEqual(settings.provider_key, key)
            self.assertEqual(settings.host, host)
            self.assertEqual(settings.port, port)
            self.assertEqual(settings.tls_mode, "tls")

    def test_unknown_domain_requires_explicit_host(self) -> None:
        with self.assertRaises(InputError) as ctx:
            resolve_settings("user@corp.example")
        self.assertEqual(ctx.exception.field, "host")

    def test_custom_host_and_tls(self) -> None:
        settings = resolve_settings(
            "user@corp.example",
            host="mail.corp.example",
            port="143",
            tls_mode="starttls",
        )
        self.assertEqual(settings.host, "mail.corp.example")
        self.assertEqual(settings.port, 143)
        self.assertEqual(settings.provider_key, "custom")

    def test_custom_host_default_port_by_tls(self) -> None:
        self.assertEqual(
            resolve_settings("user@corp.example", host="mail.corp.example").port,
            993,
        )
        self.assertEqual(
            resolve_settings(
                "user@corp.example", host="mail.corp.example", tls_mode="starttls"
            ).port,
            143,
        )

    def test_explicit_provider_host_keeps_provider_name(self) -> None:
        settings = resolve_settings("user@gmail.com", host="imap.gmail.com")
        self.assertEqual(settings.provider_key, "gmail")
        self.assertEqual(settings.provider_name, "Google Gmail")

    def test_aliyun_enterprise_can_be_selected_for_custom_domain(self) -> None:
        settings = resolve_settings("user@corp.example", provider_key="aliyun-enterprise")
        self.assertEqual(settings.provider_key, "aliyun-enterprise")
        self.assertEqual(settings.provider_name, "阿里企业邮箱")
        self.assertEqual(settings.host, "imap.qiye.aliyun.com")
        self.assertEqual(settings.port, 993)
        self.assertEqual(settings.tls_mode, "tls")
        self.assertIn("IMAP", settings.hint)

    def test_custom_provider_requires_host(self) -> None:
        with self.assertRaises(InputError) as ctx:
            resolve_settings("user@corp.example", provider_key="custom")
        self.assertEqual(ctx.exception.field, "host")

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaises(InputError) as ctx:
            resolve_settings("user@corp.example", provider_key="nope")
        self.assertEqual(ctx.exception.field, "provider")

    def test_aliyun_enterprise_host_override_becomes_custom(self) -> None:
        settings = resolve_settings(
            "user@corp.example",
            provider_key="aliyun-enterprise",
            host="mail.corp.example",
        )
        self.assertEqual(settings.provider_key, "custom")
        self.assertEqual(settings.host, "mail.corp.example")

    def test_email_normalisation(self) -> None:
        self.assertEqual(normalize_email("  User@Example.COM "), "user@example.com")
        with self.assertRaises(InputError):
            normalize_email("not-an-email")
        with self.assertRaises(InputError):
            normalize_email("")

    def test_invalid_host_and_port(self) -> None:
        with self.assertRaises(InputError):
            resolve_settings("a@b.com", host="https://imap.example.com")
        with self.assertRaises(InputError):
            resolve_settings("a@b.com", host="imap.example.com", port="70000")
        with self.assertRaises(InputError):
            resolve_settings("a@b.com", host="imap.example.com", tls_mode="ssl")

    def test_gmail_password_spaces_are_removed(self) -> None:
        self.assertEqual(prepare_password("a@gmail.com", "abcd efgh ijkl mnop"), "abcdefghijklmnop")
        self.assertEqual(prepare_password("a@qq.com", "abcd efgh"), "abcd efgh")

    def test_detect_provider_returns_none_for_unknown(self) -> None:
        self.assertIsNone(detect_provider("a@unknown.invalid"))

    def test_provider_hint_is_non_empty(self) -> None:
        settings = resolve_settings("a@qq.com")
        self.assertIn("授权码", settings.hint)


if __name__ == "__main__":
    unittest.main()
