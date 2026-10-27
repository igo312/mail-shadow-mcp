// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// db.go:
// Opens and configures the SQLite shadow database and runs idempotent schema
// migrations. The schema consists of six tables:
//
//	mail_entries      — envelope metadata (subject, sender, date, …)
//	mail_attachments  — attachment metadata, 1:n to mail_entries
//	mail_content      — plain-text body, 1:1 to mail_entries, fast PK lookup
//	mail_content_fts  — FTS5 virtual table for full-text search
//	sync_state        — highest UID synced per account+folder
//	sync_skipped      — messages whose IMAP data cannot be decoded (quarantine)
package db

import (
	"database/sql"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	_ "modernc.org/sqlite"
)

// Open opens (or creates) the SQLite database at the given path.
//
// File-backed databases use several connections so a long sync write no longer
// blocks MCP reads behind the single connection the old implementation used.
// WAL allows concurrent readers with one writer. Per-connection PRAGMAs are set
// through the DSN because database/sql may open each connection lazily.
//
// In-memory databases stay on a single connection: a second connection to
// ":memory:" would be a *different* database.
func Open(path string) (*sql.DB, error) {
	if path != ":memory:" && !strings.HasPrefix(path, "file:") {
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			return nil, fmt.Errorf("db: cannot create directory for %q: %w", path, err)
		}
	}

	dsn := path
	if path != ":memory:" && !strings.HasPrefix(path, "file:") {
		abs, err := filepath.Abs(path)
		if err != nil {
			return nil, fmt.Errorf("db: resolve path %q: %w", path, err)
		}
		abs = filepath.ToSlash(abs)
		if runtime.GOOS == "windows" && len(abs) >= 2 && abs[1] == ':' {
			abs = "/" + abs
		}
		params := url.Values{}
		// NORMAL: fsync only at checkpoints, not every commit — safe on app crash,
		// only risks data loss on OS crash/power failure (acceptable for a cache DB).
		for _, pragma := range []string{
			"busy_timeout(10000)",
			"foreign_keys(1)",
			"synchronous(NORMAL)",
			"cache_size(-4096)",
			"temp_store(FILE)",
			"mmap_size(0)",
		} {
			params.Add("_pragma", pragma)
		}
		dsn = (&url.URL{Scheme: "file", Path: abs, RawQuery: params.Encode()}).String()
	}

	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("db: cannot open %q: %w", path, err)
	}

	if path == ":memory:" || strings.HasPrefix(path, "file:") {
		// A shared-cache/file URI may be intentional, but the previous code
		// assumed one connection; keep that guarantee for in-memory/test DBs.
		db.SetMaxOpenConns(1)
	} else {
		// One connection for everything.  WAL lets readers run concurrently with
		// a writer, but this process has several writers (the periodic pass, the
		// IDLE-triggered sync, the CJK backfill and retrySkipped).  With two
		// connections those writers race each other: one holds the write lock
		// while the other spends its whole busy_timeout waiting and then fails
		// with SQLITE_BUSY, which showed up as a retry storm on every start.
		// Serialising on one connection costs some read concurrency and removes
		// the writer/writer deadlock (same guarantee the in-memory path already
		// relied on).
		db.SetMaxOpenConns(1)
		db.SetMaxIdleConns(1)
	}

	if err := db.Ping(); err != nil {
		return nil, fmt.Errorf("db: cannot connect to %q: %w", path, err)
	}

	// journal_mode is persistent in the database file, so setting it once is
	// enough. The other PRAGMAs are per-connection and already in the DSN.
	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		return nil, fmt.Errorf("db: cannot enable WAL: %w", err)
	}
	if path == ":memory:" || strings.HasPrefix(path, "file:") {
		// The DSN path above is not used for these forms; apply the rest
		// explicitly for the single connection.
		for _, pragma := range []string{
			"PRAGMA foreign_keys=ON",
			"PRAGMA synchronous=NORMAL",
			"PRAGMA cache_size=-4096",
			"PRAGMA temp_store=MEMORY",
			"PRAGMA mmap_size=0",
		} {
			if _, err := db.Exec(pragma); err != nil {
				return nil, fmt.Errorf("db: cannot set pragma %q: %w", pragma, err)
			}
		}
	}

	return db, nil
}

// Migrate creates all required tables and indexes if they do not already exist.
// It is safe to call multiple times (idempotent).
func Migrate(db *sql.DB) error {
	statements := []string{
		// Core email metadata
		`CREATE TABLE IF NOT EXISTS mail_entries (
			id            TEXT PRIMARY KEY,
			account_id    TEXT      NOT NULL,
			imap_uid      INTEGER   NOT NULL,
			imap_folder   TEXT      NOT NULL,
			subject       TEXT      NOT NULL DEFAULT '',
			sender        TEXT      NOT NULL DEFAULT '',
			recipients_to TEXT      NOT NULL DEFAULT '',
			recipients_cc TEXT      NOT NULL DEFAULT '',
			date_utc      TIMESTAMP
		)`,

		// Attachment metadata (1:n to mail_entries)
		`CREATE TABLE IF NOT EXISTS mail_attachments (
			id           INTEGER PRIMARY KEY AUTOINCREMENT,
			entry_id     TEXT    NOT NULL REFERENCES mail_entries(id) ON DELETE CASCADE,
			filename     TEXT    NOT NULL DEFAULT '',
			content_type TEXT    NOT NULL DEFAULT '',
			size_bytes   INTEGER NOT NULL DEFAULT 0
		)`,

		// Sync state: tracks highest UID synced per account+folder
		`CREATE TABLE IF NOT EXISTS sync_state (
			account_id         TEXT NOT NULL,
			imap_folder        TEXT NOT NULL,
			last_uid           INTEGER NOT NULL DEFAULT 0,
			cjk_backfill_done  INTEGER NOT NULL DEFAULT 0,
			PRIMARY KEY (account_id, imap_folder)
		)`,

		// Quarantine for messages the IMAP decoder cannot handle. The sync
		// advances past them so one broken message cannot stall the mailbox,
		// and retries them on every run through the raw-literal fallback.
		`CREATE TABLE IF NOT EXISTS sync_skipped (
			account_id  TEXT    NOT NULL,
			imap_folder TEXT    NOT NULL,
			imap_uid    INTEGER NOT NULL,
			reason      TEXT    NOT NULL DEFAULT '',
			attempts    INTEGER NOT NULL DEFAULT 1,
			first_seen  TIMESTAMP,
			last_seen   TIMESTAMP,
			PRIMARY KEY (account_id, imap_folder, imap_uid)
		)`,

		// FTS5 virtual table for full-text search over subject + body
		`CREATE VIRTUAL TABLE IF NOT EXISTS mail_content_fts
			USING fts5(entry_id UNINDEXED, subject, body_text)`,

		// Auxiliary index for CJK substring search.  CJK text is written with
		// spaces between characters, so unicode61 tokenizes each character and
		// a 2-character Chinese term can match adjacent tokens.
		`CREATE VIRTUAL TABLE IF NOT EXISTS mail_cjk_fts
			USING fts5(entry_id UNINDEXED, text)`,

		// Separate table for fast body_text retrieval (PRIMARY KEY = O(log n) lookup).
		// FTS5 virtual tables only support full-text index scans, not column index lookups.
		`CREATE TABLE IF NOT EXISTS mail_content (
			entry_id     TEXT PRIMARY KEY,
			body_text    TEXT NOT NULL DEFAULT '',
			cjk_indexed  INTEGER NOT NULL DEFAULT 0
		)`,

		// One-off database maintenance bookkeeping.  Each named job runs once
		// per database; unlike sync_state it survives folder selection changes
		// and does not depend on a backfill starting at UID 0.
		`CREATE TABLE IF NOT EXISTS db_maintenance (
			name       TEXT PRIMARY KEY,
			applied_at TIMESTAMP NOT NULL
		)`,

		// Indexes
		`CREATE INDEX IF NOT EXISTS idx_mail_date
			ON mail_entries(date_utc DESC)`,

		`CREATE INDEX IF NOT EXISTS idx_mail_account_folder
			ON mail_entries(account_id, imap_folder)`,

		// Backfill walks one folder in imap_uid order with a keyset cursor
		// (imap_uid > ?).  Without this exact column order SQLite has to sort
		// the whole folder for every batch, which made the CJK backfill
		// quadratic on large mailboxes.
		`CREATE INDEX IF NOT EXISTS idx_mail_folder_uid
			ON mail_entries(account_id, imap_folder, imap_uid)`,

		`CREATE INDEX IF NOT EXISTS idx_mail_sender
			ON mail_entries(sender)`,

		`CREATE UNIQUE INDEX IF NOT EXISTS idx_mail_account_uid_folder
			ON mail_entries(account_id, imap_uid, imap_folder)`,
	}

	for _, stmt := range statements {
		if _, err := db.Exec(stmt); err != nil {
			return fmt.Errorf("db: migration failed: %w\nstatement: %s", err, stmt)
		}
	}

	// Column additions — idempotent: ignore "duplicate column name" errors from SQLite.
	// Each applied migration is logged so operators can see what ran on startup.
	columnMigrations := []string{
		`ALTER TABLE sync_state ADD COLUMN uid_validity INTEGER NOT NULL DEFAULT 0`,
		// 0 = the auxiliary CJK index may still be missing rows for this folder,
		// so CJK searches must use the bounded substring fallback until the
		// backfill marks it done.  Existing rows default to 0, which is the safe
		// (complete but slower) choice after an upgrade.
		`ALTER TABLE sync_state ADD COLUMN cjk_backfill_done INTEGER NOT NULL DEFAULT 0`,
		// Resume point of the CJK backfill (highest imap_uid already processed).
		// Without it every pass restarts at the first UID and re-scans all
		// indexed rows, which is what made the backfill quadratic.
		`ALTER TABLE sync_state ADD COLUMN cjk_cursor_uid INTEGER NOT NULL DEFAULT 0`,
		// Nullable: NULL means "flags not yet fetched" (pre-migration rows or backfill pending).
		`ALTER TABLE mail_entries ADD COLUMN is_read    INTEGER`,
		`ALTER TABLE mail_entries ADD COLUMN is_replied INTEGER`,
		// Nullable: NULL means "envelope not yet backfilled" (pre-R6 rows).
		`ALTER TABLE mail_entries ADD COLUMN message_id  TEXT`,
		`ALTER TABLE mail_entries ADD COLUMN in_reply_to TEXT`,
		`ALTER TABLE mail_content ADD COLUMN cjk_indexed INTEGER NOT NULL DEFAULT 0`,
	}
	for _, stmt := range columnMigrations {
		if _, err := db.Exec(stmt); err != nil {
			if strings.Contains(err.Error(), "duplicate column name") {
				continue
			}
			return fmt.Errorf("db: column migration failed: %w\nstatement: %s", err, stmt)
		}
		slog.Info("db: schema migration applied", "statement", stmt)
	}

	// Indexes that depend on columns added above — must run after columnMigrations.
	for _, stmt := range []string{
		`CREATE INDEX IF NOT EXISTS idx_mail_message_id  ON mail_entries(message_id)`,
		`CREATE INDEX IF NOT EXISTS idx_mail_in_reply_to ON mail_entries(in_reply_to)`,
	} {
		if _, err := db.Exec(stmt); err != nil {
			return fmt.Errorf("db: index migration failed: %w", err)
		}
	}

	// Data migration: normalise legacy date_utc values written by old builds.
	// Before this fix, time.Time was passed directly to the modernc/sqlite driver,
	// which stored it in Go's default format "2006-01-02 15:04:05 +0000 UTC".
	// That format breaks >= / <= comparisons against RFC3339 strings because
	// space (0x20) < 'T' (0x54) lexicographically.
	// We convert all such rows to "YYYY-MM-DDTHH:MM:SSZ" in-place.
	// The LIKE pattern matches the old format; already-correct RFC3339 rows
	// contain a 'T' at position 11 and are not matched.
	if res, err := db.Exec(`UPDATE mail_entries
		SET date_utc = replace(substr(date_utc, 1, 19), ' ', 'T') || 'Z'
		WHERE date_utc LIKE '____-__-__ __%'`); err != nil {
		return fmt.Errorf("db: date_utc normalisation failed: %w", err)
	} else if n, _ := res.RowsAffected(); n > 0 {
		slog.Info("db: normalised legacy date_utc values", "rows", n)
	}

	return nil
}
