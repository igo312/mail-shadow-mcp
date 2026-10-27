// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// db_test.go:
// Unit tests for schema creation, migrations, and basic DB operations.
package db

import (
	"database/sql"
	"path/filepath"
	"testing"
)

// openTestDB opens an in-memory SQLite database and runs Migrate().
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if err := Migrate(db); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

func TestOpen_InMemory(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("expected no error, got: %v", err)
	}
	defer db.Close()
	if err := db.Ping(); err != nil {
		t.Fatalf("Ping: %v", err)
	}
}

func TestMigrate_Idempotent(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	// First run
	if err := Migrate(db); err != nil {
		t.Fatalf("first Migrate: %v", err)
	}
	// Second run must not fail
	if err := Migrate(db); err != nil {
		t.Fatalf("second Migrate (idempotency): %v", err)
	}
}

func TestMigrate_TablesExist(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	if err := Migrate(db); err != nil {
		t.Fatalf("Migrate: %v", err)
	}

	// Check regular tables via sqlite_master
	tables := []string{"mail_entries", "mail_attachments", "sync_state"}
	for _, table := range tables {
		var name string
		row := db.QueryRow(
			`SELECT name FROM sqlite_master WHERE type='table' AND name=?`, table,
		)
		if err := row.Scan(&name); err != nil {
			t.Errorf("table %q not found: %v", table, err)
		}
	}

	// FTS5 virtual tables show up as type='table' too
	var name string
	row := db.QueryRow(
		`SELECT name FROM sqlite_master WHERE type='table' AND name='mail_content_fts'`,
	)
	if err := row.Scan(&name); err != nil {
		t.Errorf("virtual table mail_content_fts not found: %v", err)
	}
}

func TestMigrate_IndexesExist(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	if err := Migrate(db); err != nil {
		t.Fatalf("Migrate: %v", err)
	}

	indexes := []string{
		"idx_mail_date",
		"idx_mail_account_folder",
		"idx_mail_sender",
		"idx_mail_account_uid_folder",
	}
	for _, idx := range indexes {
		var idxName string
		row := db.QueryRow(
			`SELECT name FROM sqlite_master WHERE type='index' AND name=?`, idx,
		)
		if err := row.Scan(&idxName); err != nil {
			t.Errorf("index %q not found: %v", idx, err)
		}
	}
}

func TestMigrate_WALAndForeignKeys(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	if err := Migrate(db); err != nil {
		t.Fatalf("Migrate: %v", err)
	}

	// Verify foreign keys are ON
	var fk int
	if err := db.QueryRow("PRAGMA foreign_keys").Scan(&fk); err != nil {
		t.Fatalf("PRAGMA foreign_keys: %v", err)
	}
	if fk != 1 {
		t.Errorf("foreign_keys: want 1, got %d", fk)
	}
}

func TestMigrate_ForeignKeyEnforced(t *testing.T) {
	db, err := Open(":memory:")
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer db.Close()

	if err := Migrate(db); err != nil {
		t.Fatalf("Migrate: %v", err)
	}

	// Inserting an attachment with a non-existent entry_id must fail
	_, err = db.Exec(`INSERT INTO mail_attachments (entry_id, filename) VALUES ('nonexistent', 'file.pdf')`)
	if err == nil {
		t.Error("expected foreign key violation, got nil")
	}
}

func TestOpenFileSerializesWriters(t *testing.T) {
	database, err := Open(filepath.Join(t.TempDir(), "mail.db"))
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer database.Close()
	if err := Migrate(database); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	// One connection for everything.  WAL lets a reader run while a writer is
	// active, but the sync engine, the IDLE-triggered sync, the CJK backfill
	// and retrySkipped all write, and with two connections they raced for the
	// single SQLite write lock: one held it while the other spent its whole
	// busy_timeout and failed with SQLITE_BUSY.  Serialising writers is worth
	// losing read/write overlap; every write is now a short, bounded batch.
	if got := database.Stats().MaxOpenConnections; got != 1 {
		t.Fatalf("MaxOpenConnections = %d, want 1 so writers cannot race", got)
	}

	if _, err := database.Exec(`INSERT INTO mail_entries
		(id, account_id, imap_uid, imap_folder, subject, sender)
		VALUES ('e1','a',1,'INBOX','one','x@y.z')`); err != nil {
		t.Fatal(err)
	}

	var n int
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_entries`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("got %d rows, want 1", n)
	}
}

func TestOpenFilePathWithSpace(t *testing.T) {
	path := filepath.Join(t.TempDir(), "mail shadow.db")
	database, err := Open(path)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	defer database.Close()
	if err := Migrate(database); err != nil {
		t.Fatalf("Migrate: %v", err)
	}
	if _, err := database.Exec(`INSERT INTO mail_entries
		(id, account_id, imap_uid, imap_folder, subject, sender)
		VALUES ('e1','a',1,'INBOX','one','x@y.z')`); err != nil {
		t.Fatal(err)
	}
}

func TestCJKIndexPending(t *testing.T) {
	database := openTestDB(t)

	// No folders synced yet: nothing can be missing from the index.
	if CJKIndexPending(database) {
		t.Fatal("empty sync_state should not be pending")
	}

	if _, err := database.Exec(`INSERT INTO sync_state
		(account_id, imap_folder, last_uid, cjk_backfill_done)
		VALUES ('acct', 'INBOX', 7, 0)`); err != nil {
		t.Fatal(err)
	}
	if !CJKIndexPending(database) {
		t.Fatal("a folder with cjk_backfill_done=0 must be pending")
	}

	if _, err := database.Exec(
		`UPDATE sync_state SET cjk_backfill_done = 1`,
	); err != nil {
		t.Fatal(err)
	}
	if CJKIndexPending(database) {
		t.Fatal("a fully backfilled folder must not be pending")
	}
}
