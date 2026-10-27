"""Cryptographic helpers, cookie formats and in-process rate limiting.

Design notes
------------
* The portal never stores an email password or a bearer token in its database.
  The idempotency key is an HMAC of (email, password), and the MCP bearer token
  is derived deterministically from that key.  Re-submitting the same pair
  therefore recreates the same MCP configuration without a secret at rest.
* Invite codes are stored as HMACs, never in plaintext.
* Session and flash cookies are HMAC-signed.  The flash cookie carries the MCP
  bearer token for a short time so the token never appears in a URL or in the
  browser history.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any


class CookieError(ValueError):
    """Raised when a cookie is malformed, expired or has a bad signature."""


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


class TokenService:
    """Derives every secret the portal needs from one master key."""

    def __init__(self, master_key: bytes) -> None:
        self._key = master_key

    def _hmac(self, label: bytes, payload: bytes) -> bytes:
        return hmac.new(self._key, label + b"\x00" + payload, hashlib.sha256).digest()

    # -- invites ---------------------------------------------------------
    @staticmethod
    def new_invite_code() -> str:
        # 24 bytes of entropy, URL-safe, prefixed so it is recognisable.
        return "msm_" + secrets.token_urlsafe(24)

    def hash_invite(self, code: str) -> str:
        return self._hmac(b"invite", code.strip().encode("utf-8")).hex()

    # -- MCP identity ----------------------------------------------------
    def derive_idempotency_key(self, email: str, password: str) -> str:
        """Return a stable, non-reversible key for (email, password)."""
        payload = email.encode("utf-8") + b"\x00" + password.encode("utf-8")
        return self._hmac(b"idem", payload).hex()

    def derive_mcp_token(self, email: str, password: str, salt: str) -> str:
        """Derive the bearer token from credentials plus a per-instance salt.

        The salt is rotated when credentials change, so revoking an instance or
        changing the app password never resurrects an older token.  The token is
        intentionally *not* derived from the stored idempotency hash: the master
        key and the control database live in the same volume, so a backup leak
        must not be enough to reconstruct live MCP tokens.
        """
        payload = (
            salt.encode("utf-8")
            + b"\x00"
            + email.encode("utf-8")
            + b"\x00"
            + password.encode("utf-8")
        )
        return b64url_encode(self._hmac(b"mcp-token", payload))

    @staticmethod
    def hash_mcp_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def new_instance_id() -> str:
        return secrets.token_hex(8)

    # -- signed cookies --------------------------------------------------
    def _sign(self, payload: bytes) -> bytes:
        return hmac.new(self._key, b"cookie\x00" + payload, hashlib.sha256).digest()

    def _encode_cookie(self, data: dict[str, Any]) -> str:
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = self._sign(payload)
        return f"{b64url_encode(payload)}.{b64url_encode(signature)}"

    def _decode_cookie(self, value: str) -> dict[str, Any]:
        try:
            payload_part, signature_part = value.split(".", 1)
        except ValueError as exc:
            raise CookieError("malformed cookie") from exc
        try:
            payload = b64url_decode(payload_part)
            signature = b64url_decode(signature_part)
        except (ValueError, TypeError) as exc:
            raise CookieError("malformed cookie") from exc
        expected = self._sign(payload)
        if not hmac.compare_digest(signature, expected):
            raise CookieError("bad signature")
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CookieError("malformed payload") from exc
        if not isinstance(data, dict):
            raise CookieError("malformed payload")
        return data

    def make_session(self, invite_id: str, ttl_seconds: int) -> str:
        now = int(time.time())
        return self._encode_cookie(
            {
                "v": 1,
                "kind": "session",
                "invite": invite_id,
                "nonce": secrets.token_hex(8),
                "iat": now,
                "exp": now + ttl_seconds,
            }
        )

    def parse_session(self, cookie: str) -> dict[str, Any]:
        data = self._decode_cookie(cookie)
        if data.get("kind") != "session" or not isinstance(data.get("invite"), str):
            raise CookieError("not a session")
        if int(data.get("exp", 0)) <= int(time.time()):
            raise CookieError("expired")
        return data

    def csrf_token(self, session_cookie: str) -> str:
        # Bound to the exact session cookie, so it rotates with the session.
        return b64url_encode(self._hmac(b"csrf", session_cookie.encode("utf-8")))

    def verify_csrf(self, session_cookie: str, candidate: str) -> bool:
        if not candidate:
            return False
        return constant_time_equal(self.csrf_token(session_cookie), candidate)

    def make_flash(
        self,
        instance_id: str,
        token: str,
        ttl_seconds: int,
        *,
        reused: bool = False,
        rotated: bool = False,
        warning: str = "",
    ) -> str:
        now = int(time.time())
        return self._encode_cookie(
            {
                "v": 1,
                "kind": "flash",
                "instance": instance_id,
                "token": token,
                "reused": bool(reused),
                "rotated": bool(rotated),
                "warning": warning[:500],
                "iat": now,
                "exp": now + ttl_seconds,
            }
        )

    def parse_flash(self, cookie: str) -> dict[str, Any]:
        data = self._decode_cookie(cookie)
        if data.get("kind") != "flash" or not isinstance(data.get("instance"), str):
            raise CookieError("not a flash cookie")
        if int(data.get("exp", 0)) <= int(time.time()):
            raise CookieError("expired")
        return data

    # -- admin console ---------------------------------------------------
    def make_admin(self, ttl_seconds: int) -> str:
        """Signed cookie proving a successful admin password check.

        The cookie carries no secret: it is an HMAC over a nonce, a role and an
        expiry, so it cannot be forged without the master key and cannot be
        replayed after ``ttl_seconds``.
        """
        now = int(time.time())
        return self._encode_cookie(
            {
                "v": 1,
                "kind": "admin",
                "role": "admin",
                "nonce": secrets.token_hex(8),
                "iat": now,
                "exp": now + ttl_seconds,
            }
        )

    def parse_admin(self, cookie: str) -> dict[str, Any]:
        data = self._decode_cookie(cookie)
        if data.get("kind") != "admin" or data.get("role") != "admin":
            raise CookieError("not an admin session")
        if int(data.get("exp", 0)) <= int(time.time()):
            raise CookieError("expired")
        return data

    def make_admin_flash(self, values: dict[str, Any], ttl_seconds: int = 300) -> str:
        """One-shot payload (e.g. a freshly created invite code) for /admin."""
        now = int(time.time())
        return self._encode_cookie(
            {
                "v": 1,
                "kind": "admin_flash",
                "values": {
                    str(key): (value if isinstance(value, (int, bool)) else str(value))[:400]
                    for key, value in values.items()
                },
                "iat": now,
                "exp": now + ttl_seconds,
            }
        )

    def parse_admin_flash(self, cookie: str) -> dict[str, Any]:
        data = self._decode_cookie(cookie)
        if data.get("kind") != "admin_flash":
            raise CookieError("not an admin flash")
        if int(data.get("exp", 0)) <= int(time.time()):
            raise CookieError("expired")
        values = data.get("values")
        if not isinstance(values, dict):
            raise CookieError("malformed admin flash")
        return values


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after: int


class RateLimiter:
    """Tiny in-memory sliding-window limiter.

    The portal is designed to run as a single replica.  If it is ever scaled
    out, move this state into the shared database or a proxy.
    """

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_cleanup = 0.0

    def check(self, key: str, limit: int, window_seconds: int) -> RateDecision:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry = max(1, int(bucket[0] + window_seconds - now) + 1)
                return RateDecision(False, retry)
            bucket.append(now)
            if now - self._last_cleanup > 300:
                self._last_cleanup = now
                self._cleanup(cutoff)
            return RateDecision(True, 0)

    def _cleanup(self, cutoff: float) -> None:
        empty: list[str] = []
        for key, bucket in self._events.items():
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                empty.append(key)
        for key in empty:
            del self._events[key]
