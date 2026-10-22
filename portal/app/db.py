"""SQLite persistence for invites, instances and a small audit trail."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    id            TEXT PRIMARY KEY,
    code_hash     TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL DEFAULT '',
    max_uses      INTEGER NOT NULL DEFAULT 1,
    uses          INTEGER NOT NULL DEFAULT 0,
    expires_at    INTEGER,
    disabled      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    last_used_at  INTEGER
);

CREATE TABLE IF NOT EXISTS instances (
    id             TEXT PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    idem_hash      TEXT NOT NULL,
    token_hash     TEXT NOT NULL UNIQUE,
    token_salt     TEXT NOT NULL DEFAULT '',
    container_id   TEXT,
    container_name TEXT NOT NULL,
    config_volume  TEXT NOT NULL,
    data_volume    TEXT NOT NULL,
    host_port      INTEGER NOT NULL DEFAULT 0,
    imap_host      TEXT NOT NULL,
    imap_port      INTEGER NOT NULL,
    tls_mode       TEXT NOT NULL,
    provider_key   TEXT NOT NULL DEFAULT '',
    provider_name  TEXT NOT NULL DEFAULT '',
    owner_invite   TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL,
    last_error     TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    last_seen_at   INTEGER
);

CREATE INDEX IF NOT EXISTS idx_instances_email ON instances(email);
CREATE INDEX IF NOT EXISTS idx_instances_token ON instances(token_hash);
CREATE INDEX IF NOT EXISTS idx_instances_status ON instances(status);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      INTEGER NOT NULL,
    ip      TEXT NOT NULL DEFAULT '',
    action  TEXT NOT NULL,
    email   TEXT,
    detail  TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
"""


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,
            timeout=10,
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.executescript(_SCHEMA)
            columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(instances)").fetchall()
            }
            if "token_salt" not in columns:
                self._conn.execute(
                    "ALTER TABLE instances ADD COLUMN token_salt TEXT NOT NULL DEFAULT ''"
                )
            if "host_port" not in columns:
                self._conn.execute(
                    "ALTER TABLE instances ADD COLUMN host_port INTEGER NOT NULL DEFAULT 0"
                )
            if "owner_invite" not in columns:
                self._conn.execute(
                    "ALTER TABLE instances ADD COLUMN owner_invite TEXT NOT NULL DEFAULT ''"
                )
            current = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if current < SCHEMA_VERSION:
                self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- invites ---------------------------------------------------------
    def create_invite(
        self,
        code_hash: str,
        *,
        label: str = "",
        max_uses: int = 1,
        expires_at: int | None = None,
    ) -> str:
        invite_id = secrets.token_hex(8)
        with self._lock:
            self._conn.execute(
                """INSERT INTO invites
                   (id, code_hash, label, max_uses, uses, expires_at, disabled, created_at)
                   VALUES (?, ?, ?, ?, 0, ?, 0, ?)""",
                (invite_id, code_hash, label, max(1, max_uses), expires_at, now()),
            )
        return invite_id

    def consume_invite(self, code_hash: str) -> dict[str, Any] | None:
        """Atomically spend one use of an invite.  Returns the invite row."""
        stamp = now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    """UPDATE invites
                       SET uses = uses + 1, last_used_at = ?
                       WHERE code_hash = ?
                         AND disabled = 0
                         AND uses < max_uses
                         AND (expires_at IS NULL OR expires_at > ?)""",
                    (stamp, code_hash, stamp),
                )
                if cursor.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return None
                row = self._conn.execute(
                    "SELECT * FROM invites WHERE code_hash = ?", (code_hash,)
                ).fetchone()
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return dict(row) if row else None

    def get_invite(self, invite_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM invites WHERE id = ?", (invite_id,)
            ).fetchone()
        return dict(row) if row else None

    def invite_is_active(self, invite_id: str) -> bool:
        stamp = now()
        with self._lock:
            row = self._conn.execute(
                """SELECT 1 FROM invites
                   WHERE id = ? AND disabled = 0
                     AND (expires_at IS NULL OR expires_at > ?)""",
                (invite_id, stamp),
            ).fetchone()
        return row is not None

    def list_invites(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT i.id, i.label, i.max_uses, i.uses, i.expires_at, i.disabled,
                          i.created_at, i.last_used_at,
                          (SELECT COUNT(*) FROM instances
                            WHERE owner_invite = i.id AND status != 'removed') AS mailbox_count
                   FROM invites i ORDER BY i.created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def disable_invite(self, invite_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE invites SET disabled = 1 WHERE id = ?", (invite_id,)
            )
        return cursor.rowcount == 1

    def invite_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM invites").fetchone()[0])

    # -- instances -------------------------------------------------------
    def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM instances WHERE id = ?", (instance_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_instance_by_email(self, email: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM instances WHERE email = ?", (email,)
            ).fetchone()
        return dict(row) if row else None

    def get_instance_by_token_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM instances WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        return dict(row) if row else None

    def count_active_instances(self) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM instances WHERE status != 'removed'"
                ).fetchone()[0]
            )

    _INSTANCE_KEYS = (
        "id",
        "email",
        "idem_hash",
        "token_hash",
        "token_salt",
        "container_name",
        "config_volume",
        "data_volume",
        "host_port",
        "imap_host",
        "imap_port",
        "tls_mode",
        "provider_key",
        "provider_name",
        "owner_invite",
        "status",
        "last_error",
        "created_at",
        "updated_at",
    )

    def _insert_instance_row(self, record: dict[str, Any]) -> None:
        keys = self._INSTANCE_KEYS
        values = [record.get(key) for key in keys]
        values[keys.index("host_port")] = record.get("host_port") or 0
        values[keys.index("owner_invite")] = record.get("owner_invite") or ""
        self._conn.execute(
            f"INSERT INTO instances ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
            values,
        )

    def insert_instance(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._insert_instance_row(record)

    def count_instances_for_invite(self, invite_id: str) -> int:
        with self._lock:
            return int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM instances WHERE owner_invite = ? AND status != 'removed'",
                    (invite_id,),
                ).fetchone()[0]
            )

    def reserve_instance(self, record: dict[str, Any], max_instances: int) -> str:
        """Insert a new instance only if capacity is still available.

        The count check and insert happen in one BEGIN IMMEDIATE transaction so
        two concurrent sign-ups cannot both observe ``count == max-1`` and
        overshoot the deployment limit.  Container creation happens later,
        outside this lock; the reserved row already counts against capacity.

        Returns ``"ok"`` or ``"capacity"``.  There is deliberately no per-invite
        mailbox cap: one invite may create multiple instances; ``max_uses``
        limits invite redemptions/sessions, not mailboxes.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM instances WHERE status != 'removed'"
                    ).fetchone()[0]
                )
                if max_instances > 0 and count >= max_instances:
                    self._conn.execute("ROLLBACK")
                    return "capacity"
                self._insert_instance_row(record)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return "ok"

    def update_instance_credentials(
        self,
        instance_id: str,
        *,
        idem_hash: str,
        token_hash: str,
        token_salt: str,
        imap_host: str,
        imap_port: int,
        tls_mode: str,
        provider_key: str,
        provider_name: str,
        status: str = "provisioning",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE instances SET
                     idem_hash = ?, token_hash = ?, token_salt = ?, imap_host = ?, imap_port = ?,
                     tls_mode = ?, provider_key = ?, provider_name = ?,
                     status = ?, last_error = NULL, updated_at = ?
                   WHERE id = ?""",
                (
                    idem_hash,
                    token_hash,
                    token_salt,
                    imap_host,
                    imap_port,
                    tls_mode,
                    provider_key,
                    provider_name,
                    status,
                    now(),
                    instance_id,
                ),
            )

    def set_instance_status(
        self,
        instance_id: str,
        status: str,
        *,
        error: str | None = None,
        container_id: str | None = None,
    ) -> None:
        with self._lock:
            if container_id is not None:
                self._conn.execute(
                    """UPDATE instances SET status = ?, last_error = ?,
                           container_id = ?, updated_at = ? WHERE id = ?""",
                    (status, error, container_id, now(), instance_id),
                )
            else:
                self._conn.execute(
                    """UPDATE instances SET status = ?, last_error = ?,
                           updated_at = ? WHERE id = ?""",
                    (status, error, now(), instance_id),
                )

    def set_instance_owner(self, instance_id: str, invite_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE instances SET owner_invite = ?, updated_at = ? WHERE id = ?",
                (invite_id, now(), instance_id),
            )

    def set_instance_token_hash(self, instance_id: str, token_hash: str) -> None:
        """Repair the routing hash to match the credential in the config volume."""
        with self._lock:
            self._conn.execute(
                "UPDATE instances SET token_hash = ?, updated_at = ? WHERE id = ?",
                (token_hash, now(), instance_id),
            )

    def set_instance_host_port(self, instance_id: str, host_port: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE instances SET host_port = ?, updated_at = ? WHERE id = ?",
                (int(host_port), now(), instance_id),
            )

    def touch_instance(self, instance_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE instances SET last_seen_at = ? WHERE id = ?",
                (now(), instance_id),
            )

    def list_instances(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, email, container_name, config_volume, data_volume,
                          host_port, imap_host, status, last_error, created_at, updated_at,
                          last_seen_at
                   FROM instances ORDER BY created_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_instance_removed(self, instance_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE instances SET status = 'removed', updated_at = ? WHERE id = ?",
                (now(), instance_id),
            )

    # -- audit -----------------------------------------------------------
    def audit(
        self,
        action: str,
        *,
        ip: str = "",
        email: str | None = None,
        detail: Any = None,
    ) -> None:
        payload = None if detail is None else json.dumps(detail, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (at, ip, action, email, detail) VALUES (?, ?, ?, ?, ?)",
                (now(), ip, action, email, payload),
            )
