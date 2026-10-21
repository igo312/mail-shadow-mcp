from __future__ import annotations

import time
import unittest

from app.security import (
    CookieError,
    RateLimiter,
    TokenService,
    b64url_decode,
    b64url_encode,
)


class TokenServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = TokenService(b"k" * 32)

    def test_idempotency_and_token_are_deterministic(self) -> None:
        first = self.service.derive_idempotency_key("user@example.com", "secret")
        second = self.service.derive_idempotency_key("user@example.com", "secret")
        self.assertEqual(first, second)
        self.assertEqual(
            self.service.derive_mcp_token("user@example.com", "secret", "salt"),
            self.service.derive_mcp_token("user@example.com", "secret", "salt"),
        )

    def test_different_password_or_salt_rotates_token(self) -> None:
        first = self.service.derive_idempotency_key("user@example.com", "old")
        second = self.service.derive_idempotency_key("user@example.com", "new")
        self.assertNotEqual(first, second)
        self.assertNotEqual(
            self.service.derive_mcp_token("user@example.com", "old", "salt"),
            self.service.derive_mcp_token("user@example.com", "new", "salt"),
        )
        self.assertNotEqual(
            self.service.derive_mcp_token("user@example.com", "old", "salt-a"),
            self.service.derive_mcp_token("user@example.com", "old", "salt-b"),
        )

    def test_token_is_not_derivable_from_stored_idempotency_hash(self) -> None:
        # The control DB stores idem_hash (and the non-secret salt).  Even with
        # the master key, those values must not be enough to reconstruct the
        # live MCP token without the password.
        email, password = "user@example.com", "secret"
        idem = self.service.derive_idempotency_key(email, password)
        token = self.service.derive_mcp_token(email, password, "salt")
        self.assertNotEqual(token, self.service.derive_mcp_token(email, idem, "salt"))
        self.assertNotEqual(
            token, self.service.derive_mcp_token("other@example.com", password, "salt")
        )

    def test_token_hash_is_not_reversible(self) -> None:
        token = self.service.derive_mcp_token("user@example.com", "secret", "salt")
        digest = self.service.hash_mcp_token(token)
        self.assertEqual(len(digest), 64)
        self.assertNotIn(token, digest)

    def test_session_round_trip_and_tamper(self) -> None:
        cookie = self.service.make_session("invite-1", 60)
        session = self.service.parse_session(cookie)
        self.assertEqual(session["invite"], "invite-1")
        payload, signature = cookie.split(".", 1)
        tampered = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature
        with self.assertRaises(CookieError):
            self.service.parse_session(tampered)

    def test_expired_session_is_rejected(self) -> None:
        cookie = self.service.make_session("invite-1", -1)
        with self.assertRaises(CookieError):
            self.service.parse_session(cookie)

    def test_csrf_is_bound_to_session(self) -> None:
        cookie = self.service.make_session("invite-1", 60)
        other = self.service.make_session("invite-1", 60)
        csrf = self.service.csrf_token(cookie)
        self.assertTrue(self.service.verify_csrf(cookie, csrf))
        self.assertFalse(self.service.verify_csrf(other, csrf))
        self.assertFalse(self.service.verify_csrf(cookie, ""))

    def test_flash_carries_metadata_and_rejects_tamper(self) -> None:
        flash = self.service.make_flash(
            "abc",
            "token",
            60,
            reused=True,
            rotated=False,
            warning="w",
        )
        data = self.service.parse_flash(flash)
        self.assertEqual(data["instance"], "abc")
        self.assertEqual(data["token"], "token")
        self.assertTrue(data["reused"])
        self.assertEqual(data["warning"], "w")
        payload, signature = flash.split(".", 1)
        tampered = ("A" if payload[0] != "A" else "B") + payload[1:] + "." + signature
        with self.assertRaises(CookieError):
            self.service.parse_flash(tampered)

    def test_invite_hash_is_domain_separated(self) -> None:
        service = TokenService(b"k" * 32)
        self.assertNotEqual(
            service.hash_invite("msm_code"),
            service.hash_mcp_token("msm_code"),
        )


class RateLimiterTest(unittest.TestCase):
    def test_limit_and_retry(self) -> None:
        limiter = RateLimiter()
        self.assertTrue(limiter.check("a", 2, 60).allowed)
        self.assertTrue(limiter.check("a", 2, 60).allowed)
        decision = limiter.check("a", 2, 60)
        self.assertFalse(decision.allowed)
        self.assertGreaterEqual(decision.retry_after, 1)
        self.assertTrue(limiter.check("b", 1, 60).allowed)

    def test_window_expires(self) -> None:
        limiter = RateLimiter()
        self.assertTrue(limiter.check("a", 1, 0.1).allowed)
        self.assertFalse(limiter.check("a", 1, 0.1).allowed)
        time.sleep(0.15)
        self.assertTrue(limiter.check("a", 1, 0.1).allowed)


class Base64Test(unittest.TestCase):
    def test_round_trip_without_padding(self) -> None:
        raw = b"\x00\xffhello"
        encoded = b64url_encode(raw)
        self.assertNotIn("=", encoded)
        self.assertEqual(b64url_decode(encoded), raw)


if __name__ == "__main__":
    unittest.main()
