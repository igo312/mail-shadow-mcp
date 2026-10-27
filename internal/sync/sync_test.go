// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// sync_test.go:
// Unit tests for body decoding, MIME parsing, and mail entry extraction.
package imapsync

import (
	"database/sql"
	"fmt"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/dryas/mail-shadow-mcp/internal/config"
	"github.com/dryas/mail-shadow-mcp/internal/db"
	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"
)

// openTestDB opens an in-memory DB and runs migrations for use in sync tests.
func openTestDB(t *testing.T) *sql.DB {
	t.Helper()
	database, err := db.Open(":memory:")
	if err != nil {
		t.Fatalf("db.Open: %v", err)
	}
	if err := db.Migrate(database); err != nil {
		t.Fatalf("db.Migrate: %v", err)
	}
	t.Cleanup(func() { database.Close() })
	return database
}

func TestGetLastUID_NoEntry(t *testing.T) {
	database := openTestDB(t)
	uid, err := getLastUID(database, "test@example.com", "INBOX")
	if err != nil {
		t.Fatalf("getLastUID: %v", err)
	}
	if uid != 0 {
		t.Errorf("expected 0 for missing entry, got %d", uid)
	}
}

func TestSetAndGetLastUID(t *testing.T) {
	database := openTestDB(t)

	if err := setLastUID(database, "test@example.com", "INBOX", 42); err != nil {
		t.Fatalf("setLastUID: %v", err)
	}

	uid, err := getLastUID(database, "test@example.com", "INBOX")
	if err != nil {
		t.Fatalf("getLastUID: %v", err)
	}
	if uid != 42 {
		t.Errorf("expected 42, got %d", uid)
	}
}

func TestSetLastUID_Idempotent(t *testing.T) {
	database := openTestDB(t)

	for _, value := range []uint32{10, 20, 30} {
		if err := setLastUID(database, "test@example.com", "INBOX", value); err != nil {
			t.Fatalf("setLastUID(%d): %v", value, err)
		}
	}

	uid, err := getLastUID(database, "test@example.com", "INBOX")
	if err != nil {
		t.Fatalf("getLastUID: %v", err)
	}
	if uid != 30 {
		t.Errorf("expected 30 after three updates, got %d", uid)
	}
}

func TestInsertEntry_NoDuplicate(t *testing.T) {
	database := openTestDB(t)

	entry := mailEntry{
		ID:         "account:INBOX:1",
		AccountID:  "test@example.com",
		IMAPUID:    1,
		IMAPFolder: "INBOX",
		Subject:    "Hello World",
		Sender:     "sender@example.com",
		DateUTC:    time.Now().UTC().Format(time.RFC3339),
	}

	tx, err := database.Begin()
	if err != nil {
		t.Fatalf("Begin: %v", err)
	}

	insert := func() error {
		_, err := tx.Exec(`INSERT OR IGNORE INTO mail_entries
			(id, account_id, imap_uid, imap_folder, subject, sender, recipients_to, recipients_cc, date_utc)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			entry.ID, entry.AccountID, entry.IMAPUID, entry.IMAPFolder,
			entry.Subject, entry.Sender, "", "", entry.DateUTC)
		return err
	}

	// Insert twice — only one row must appear (INSERT OR IGNORE).
	if err := insert(); err != nil {
		t.Fatalf("first insert: %v", err)
	}
	if err := insert(); err != nil {
		t.Fatalf("second insert: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("Commit: %v", err)
	}

	var count int
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_entries WHERE id = ?`, entry.ID).Scan(&count); err != nil {
		t.Fatalf("count query: %v", err)
	}
	if count != 1 {
		t.Errorf("expected 1 row after duplicate insert, got %d", count)
	}
}

func TestBuildEntry_Fields(t *testing.T) {
	now := time.Date(2026, 4, 21, 12, 0, 0, 0, time.UTC)
	msg := &imapclient.FetchMessageBuffer{
		UID: imap.UID(99),
		Envelope: &imap.Envelope{
			Date:    now,
			Subject: "Test Subject",
			From:    []imap.Address{{Name: "Sender", Mailbox: "sender", Host: "example.com"}},
			To:      []imap.Address{{Mailbox: "to", Host: "example.com"}},
			Cc:      []imap.Address{{Mailbox: "cc", Host: "example.com"}},
		},
	}

	entry := buildEntry("acc@test.com", "INBOX", 99, msg)

	if entry.ID != "acc@test.com:INBOX:99" {
		t.Errorf("ID: want acc@test.com:INBOX:99, got %q", entry.ID)
	}
	if entry.Subject != "Test Subject" {
		t.Errorf("Subject: want 'Test Subject', got %q", entry.Subject)
	}
	if entry.Sender != "Sender <sender@example.com>" {
		t.Errorf("Sender: got %q", entry.Sender)
	}
	if entry.RecipientsTo != "to@example.com" {
		t.Errorf("RecipientsTo: got %q", entry.RecipientsTo)
	}
	if entry.RecipientsCC != "cc@example.com" {
		t.Errorf("RecipientsCC: got %q", entry.RecipientsCC)
	}
	dt, ok := entry.DateUTC.(string)
	if !ok {
		t.Fatalf("DateUTC is not string (got %T)", entry.DateUTC)
	}
	if dt != now.UTC().Format(time.RFC3339) {
		t.Errorf("DateUTC: want %v, got %v", now.UTC().Format(time.RFC3339), dt)
	}
}

func TestFormatAddresses(t *testing.T) {
	addrs := []imap.Address{
		{Name: "Alice", Mailbox: "alice", Host: "example.com"},
		{Mailbox: "bob", Host: "example.com"},
	}
	result := formatAddresses(addrs)
	expected := "Alice <alice@example.com>, bob@example.com"
	if result != expected {
		t.Errorf("want %q, got %q", expected, result)
	}
}

// ---------------------------------------------------------------------------
// Step 5 — body text & attachments
// ---------------------------------------------------------------------------

func TestExtractTextFromSections_Decodes(t *testing.T) {
	section := &imap.FetchItemBodySection{Specifier: imap.PartSpecifierText, Peek: true}
	bs := &imap.BodyStructureSinglePart{Type: "text", Subtype: "plain", Encoding: "7BIT"}
	msg := &imapclient.FetchMessageBuffer{
		BodySection: []imapclient.FetchBodySectionBuffer{
			{Section: section, Bytes: []byte("  Hello world  ")},
		},
	}
	if got := extractTextFromSections(bs, msg); got != "Hello world" {
		t.Errorf("want %q, got %q", "Hello world", got)
	}
}

func TestExtractTextFromSections_Missing(t *testing.T) {
	if got := extractTextFromSections(nil, &imapclient.FetchMessageBuffer{}); got != "" {
		t.Errorf("expected empty string, got %q", got)
	}
}

// A multipart mail with a text part and a PDF attachment must only ask for the
// text part: the attachment payload is what made one folder take hours.
func TestTextSections_SkipsAttachments(t *testing.T) {
	pdf := &imap.BodyStructureSinglePart{
		Type: "application", Subtype: "pdf", Size: 14 << 20,
		Extended: &imap.BodyStructureSinglePartExt{
			Disposition: &imap.BodyStructureDisposition{
				Value:  "attachment",
				Params: map[string]string{"filename": "report.pdf"},
			},
		},
	}
	bs := &imap.BodyStructureMultiPart{
		Children: []imap.BodyStructure{
			&imap.BodyStructureMultiPart{
				Children: []imap.BodyStructure{
					&imap.BodyStructureSinglePart{Type: "text", Subtype: "plain", Encoding: "base64"},
					&imap.BodyStructureSinglePart{Type: "text", Subtype: "html", Encoding: "base64"},
				},
				Subtype: "alternative",
			},
			pdf,
		},
		Subtype: "mixed",
	}

	sections := textSections(bs)
	if len(sections) != 1 {
		t.Fatalf("expected exactly the text/plain part, got %d section(s)", len(sections))
	}
	if got := sections[0].Part; len(got) != 2 || got[0] != 1 || got[1] != 1 {
		t.Errorf("part path: want [1 1], got %v", got)
	}
	if !sections[0].Peek {
		t.Error("body section must be fetched with PEEK so the mailbox is not modified")
	}
}

// A single-part PDF mail has no readable text: fetching BODY[TEXT] there would
// download the whole attachment for nothing.
func TestTextSections_SinglePartAttachmentIsSkipped(t *testing.T) {
	bs := &imap.BodyStructureSinglePart{Type: "application", Subtype: "pdf", Size: 14 << 20}
	if sections := textSections(bs); len(sections) != 0 {
		t.Errorf("expected no sections for an attachment-only mail, got %d", len(sections))
	}
}

func TestTextSections_SinglePartText(t *testing.T) {
	bs := &imap.BodyStructureSinglePart{Type: "text", Subtype: "plain", Encoding: "quoted-printable"}
	sections := textSections(bs)
	if len(sections) != 1 {
		t.Fatalf("expected 1 section, got %d", len(sections))
	}
	if sections[0].Specifier != imap.PartSpecifierText {
		t.Errorf("single-part body must be fetched as BODY[TEXT], got %q", sections[0].Specifier)
	}
}

func TestInsertFTS_AndSearch(t *testing.T) {
	database := openTestDB(t)

	_, err := database.Exec(
		`INSERT INTO mail_entries (id, account_id, imap_uid, imap_folder, subject, sender, recipients_to, recipients_cc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		"acc:INBOX:1", "acc", 1, "INBOX", "Test Subject", "", "", "",
	)
	if err != nil {
		t.Fatalf("insert mail_entries: %v", err)
	}

	tx, err := database.Begin()
	if err != nil {
		t.Fatalf("Begin: %v", err)
	}
	if _, err := tx.Exec(`DELETE FROM mail_content_fts WHERE entry_id = ?`, "acc:INBOX:1"); err != nil {
		t.Fatalf("delete fts: %v", err)
	}
	if _, err := tx.Exec(`INSERT INTO mail_content_fts (entry_id, subject, body_text) VALUES (?, ?, ?)`,
		"acc:INBOX:1", "Test Subject", "unique_token_xyz"); err != nil {
		t.Fatalf("insert fts: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatalf("Commit: %v", err)
	}

	var count int
	err = database.QueryRow(`SELECT count(*) FROM mail_content_fts WHERE mail_content_fts MATCH 'unique_token_xyz'`).Scan(&count)
	if err != nil {
		t.Fatalf("FTS MATCH query: %v", err)
	}
	if count != 1 {
		t.Errorf("expected 1 FTS match, got %d", count)
	}
}

func TestInsertFTS_Idempotent(t *testing.T) {
	database := openTestDB(t)

	for i := 0; i < 2; i++ {
		tx, _ := database.Begin()
		tx.Exec(`DELETE FROM mail_content_fts WHERE entry_id = ?`, "acc:INBOX:2")
		tx.Exec(`INSERT INTO mail_content_fts (entry_id, subject, body_text) VALUES (?, ?, ?)`, "acc:INBOX:2", "Subject", "body")
		tx.Commit()
	}

	var count int
	database.QueryRow(`SELECT count(*) FROM mail_content_fts`).Scan(&count)
	if count != 1 {
		t.Errorf("expected 1 row after duplicate insert, got %d", count)
	}
}

func TestExtractAttachments_WithAttachment(t *testing.T) {
	ext := &imap.BodyStructureSinglePartExt{
		Disposition: &imap.BodyStructureDisposition{
			Value:  "attachment",
			Params: map[string]string{"filename": "invoice.pdf"},
		},
	}
	bs := &imap.BodyStructureSinglePart{
		Type:     "application",
		Subtype:  "pdf",
		Size:     1024,
		Extended: ext,
	}
	atts := extractAttachments(bs)
	if len(atts) != 1 {
		t.Fatalf("expected 1 attachment, got %d", len(atts))
	}
	if atts[0].Filename != "invoice.pdf" {
		t.Errorf("filename: want %q, got %q", "invoice.pdf", atts[0].Filename)
	}
	if atts[0].SizeBytes != 1024 {
		t.Errorf("size: want 1024, got %d", atts[0].SizeBytes)
	}
}

func TestExtractAttachments_NoAttachment(t *testing.T) {
	bs := &imap.BodyStructureSinglePart{
		Type:    "text",
		Subtype: "plain",
	}
	if atts := extractAttachments(bs); len(atts) != 0 {
		t.Errorf("expected no attachments, got %d", len(atts))
	}
}

func TestInsertAttachment_And_Query(t *testing.T) {
	database := openTestDB(t)

	// parent row
	_, err := database.Exec(
		`INSERT INTO mail_entries (id, account_id, imap_uid, imap_folder, subject, sender, recipients_to, recipients_cc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		"acc:INBOX:3", "acc", 3, "INBOX", "With Attachment", "", "", "",
	)
	if err != nil {
		t.Fatalf("insert mail_entries: %v", err)
	}

	tx, _ := database.Begin()
	_, err2 := tx.Exec(`INSERT INTO mail_attachments (entry_id, filename, content_type, size_bytes) VALUES (?,?,?,?)`,
		"acc:INBOX:3", "doc.pdf", "application/pdf", 512)
	if err2 != nil {
		t.Fatalf("insertAttachment: %v", err2)
	}
	tx.Commit()

	var fn string
	var sz int64
	err = database.QueryRow(
		`SELECT filename, size_bytes FROM mail_attachments WHERE entry_id = ?`, "acc:INBOX:3",
	).Scan(&fn, &sz)
	if err != nil {
		t.Fatalf("query attachment: %v", err)
	}
	if fn != "doc.pdf" || sz != 512 {
		t.Errorf("got filename=%q size=%d", fn, sz)
	}
}

// When the server changes UIDVALIDITY the old rows must be purged from all
// three tables.  mail_content and mail_content_fts have no foreign key to
// mail_entries, so deleting only mail_entries used to leave stale bodies and
// duplicate FTS documents behind.
func TestCheckAndResetUIDValidity_PurgesIndexRows(t *testing.T) {
	database := openTestDB(t)
	const account, folder = "test@example.com", "INBOX"
	if _, err := database.Exec(`INSERT INTO mail_entries
		(id, account_id, imap_uid, imap_folder, subject, sender)
		VALUES ('e1', ?, 1, ?, 'old', 'a@b.c')`, account, folder); err != nil {
		t.Fatal(err)
	}
	if _, err := database.Exec(`INSERT INTO mail_content (entry_id, body_text) VALUES ('e1', 'old body')`); err != nil {
		t.Fatal(err)
	}
	if _, err := database.Exec(`INSERT INTO mail_content_fts (entry_id, subject, body_text) VALUES ('e1', 'old', 'old body')`); err != nil {
		t.Fatal(err)
	}
	if _, err := database.Exec(`INSERT INTO mail_cjk_fts (entry_id, text) VALUES ('e1', '旧 邮 件')`); err != nil {
		t.Fatal(err)
	}
	if _, err := database.Exec(`INSERT INTO sync_state
		(account_id, imap_folder, last_uid, uid_validity) VALUES (?, ?, 1, 100)`, account, folder); err != nil {
		t.Fatal(err)
	}

	last, err := checkAndResetUIDValidity(database, slog.Default(), account, folder, 1, &imap.SelectData{UIDValidity: 200})
	if err != nil {
		t.Fatalf("checkAndResetUIDValidity: %v", err)
	}
	if last != 0 {
		t.Fatalf("expected last UID 0 after reset, got %d", last)
	}
	for _, table := range []string{"mail_entries", "mail_content", "mail_content_fts", "mail_cjk_fts"} {
		var n int
		if err := database.QueryRow("SELECT COUNT(*) FROM " + table).Scan(&n); err != nil {
			t.Fatalf("count %s: %v", table, err)
		}
		if n != 0 {
			t.Errorf("%s still has %d row(s) after UIDVALIDITY purge", table, n)
		}
	}
	var validity uint32
	var lastUID uint32
	if err := database.QueryRow(`SELECT uid_validity, last_uid FROM sync_state WHERE account_id=? AND imap_folder=?`,
		account, folder).Scan(&validity, &lastUID); err != nil {
		t.Fatal(err)
	}
	if validity != 200 || lastUID != 0 {
		t.Errorf("sync_state: got validity=%d last_uid=%d, want 200/0", validity, lastUID)
	}
}

func TestTextSections_HTMLOnlyFallsBackToHTML(t *testing.T) {
	bs := &imap.BodyStructureMultiPart{
		Children: []imap.BodyStructure{
			&imap.BodyStructureSinglePart{Type: "text", Subtype: "html", Encoding: "quoted-printable"},
		},
		Subtype: "mixed",
	}
	sections := textSections(bs)
	if len(sections) != 1 {
		t.Fatalf("expected the HTML part, got %d section(s)", len(sections))
	}
	if got := sections[0].Part; len(got) != 1 || got[0] != 1 {
		t.Errorf("part path: want [1], got %v", got)
	}
}

func TestExtractTextFromSections_StripsHTML(t *testing.T) {
	section := &imap.FetchItemBodySection{Part: []int{1}, Peek: true}
	bs := &imap.BodyStructureMultiPart{
		Children: []imap.BodyStructure{
			&imap.BodyStructureSinglePart{Type: "text", Subtype: "html", Encoding: "7BIT"},
		},
		Subtype: "mixed",
	}
	msg := &imapclient.FetchMessageBuffer{
		BodySection: []imapclient.FetchBodySectionBuffer{{
			Section: section,
			Bytes:   []byte(`<html><style>p{color:red}</style><body><p>Hello&nbsp;<b>world</b></p><script>x()</script></body></html>`),
		}},
	}
	got := extractTextFromSections(bs, msg)
	if got != "Hello world" {
		t.Errorf("want %q, got %q", "Hello world", got)
	}
}

func TestHTMLToText(t *testing.T) {
	got := htmlToText(`<p>Hello&nbsp;<b>world</b> &amp; friends</p><!-- c -->`)
	if got != "Hello world & friends" {
		t.Errorf("htmlToText: got %q", got)
	}
}

func TestMergeFTS_BoundedCommand(t *testing.T) {
	database := openTestDB(t)
	for i := 0; i < 50; i++ {
		if _, err := database.Exec(`INSERT INTO mail_content_fts (entry_id, subject, body_text) VALUES (?,?,?)`,
			fmt.Sprintf("e%d", i), "subject", "body"); err != nil {
			t.Fatal(err)
		}
	}
	mergeFTS(database, slog.Default(), 10)
	var n int
	if err := database.QueryRow(`SELECT count(*) FROM mail_content_fts WHERE mail_content_fts MATCH 'body'`).Scan(&n); err != nil {
		t.Fatalf("FTS query after merge: %v", err)
	}
	if n != 50 {
		t.Errorf("expected 50 FTS rows, got %d", n)
	}
}

func TestBackfillCJK_IndexesExistingRows(t *testing.T) {
	database := openTestDB(t)
	const account, folder = "acc1", "INBOX"
	for i, item := range []struct{ subject, body string }{
		{"关于财务的通知", "正文内容"},
		{"Weekly report", "plain body"},
	} {
		id := fmt.Sprintf("%s:%s:%d", account, folder, i+1)
		if _, err := database.Exec(`INSERT INTO mail_entries
			(id, account_id, imap_uid, imap_folder, subject, sender)
			VALUES (?, ?, ?, ?, ?, 'a@b.c')`, id, account, i+1, folder, item.subject); err != nil {
			t.Fatal(err)
		}
		if _, err := database.Exec(`INSERT INTO mail_content (entry_id, body_text, cjk_indexed)
			VALUES (?, ?, 0)`, id, item.body); err != nil {
			t.Fatal(err)
		}
	}

	client := &Client{cfg: config.AccountConfig{ID: account}}
	if err := client.backfillCJK(database, slog.Default(), folder); err != nil {
		t.Fatalf("backfillCJK: %v", err)
	}

	var cjkRows int
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_cjk_fts`).Scan(&cjkRows); err != nil {
		t.Fatal(err)
	}
	if cjkRows != 1 {
		t.Errorf("expected 1 CJK-indexed row, got %d", cjkRows)
	}
	var pending int
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_content WHERE cjk_indexed = 0`).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if pending != 0 {
		t.Errorf("expected all rows marked indexed, %d pending", pending)
	}
}

func TestIndexNewMessage_PopulatesCJKIndex(t *testing.T) {
	database := openTestDB(t)
	tx, err := database.Begin()
	if err != nil {
		t.Fatal(err)
	}
	stmts, err := prepareBatchStmts(tx)
	if err != nil {
		t.Fatal(err)
	}
	entry := mailEntry{ID: "e1", Subject: "关于财务的通知"}
	if err := indexNewMessage(tx, stmts, entry, &imapclient.FetchMessageBuffer{}, "正文内容", 1); err != nil {
		t.Fatalf("indexNewMessage: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	var cjkRows int
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_cjk_fts WHERE mail_cjk_fts MATCH ?`, `"通 知"`).Scan(&cjkRows); err != nil {
		t.Fatal(err)
	}
	if cjkRows != 1 {
		t.Errorf("expected the CJK index to contain the new message, got %d", cjkRows)
	}
	var indexed int
	if err := database.QueryRow(`SELECT cjk_indexed FROM mail_content WHERE entry_id = 'e1'`).Scan(&indexed); err != nil {
		t.Fatal(err)
	}
	if indexed != 1 {
		t.Errorf("cjk_indexed = %d, want 1", indexed)
	}
}

func TestIndexNewMessage_CapsStoredBody(t *testing.T) {
	database := openTestDB(t)
	tx, err := database.Begin()
	if err != nil {
		t.Fatal(err)
	}
	stmts, err := prepareBatchStmts(tx)
	if err != nil {
		t.Fatal(err)
	}
	body := strings.Repeat("x", maxStoredBodyBytes*2)
	entry := mailEntry{ID: "e1", Subject: "big"}
	if err := indexNewMessage(tx, stmts, entry, &imapclient.FetchMessageBuffer{}, body, 1); err != nil {
		t.Fatalf("indexNewMessage: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	var stored int
	if err := database.QueryRow(`SELECT length(body_text) FROM mail_content WHERE entry_id = 'e1'`).Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if stored != maxStoredBodyBytes {
		t.Errorf("stored body length = %d, want %d", stored, maxStoredBodyBytes)
	}
}

// insertBackfillRow adds one message and an unindexed mail_content row.
func insertBackfillRow(t *testing.T, database *sql.DB, account, folder string, uid int, subject, body string) {
	t.Helper()
	id := fmt.Sprintf("%s:%s:%d", account, folder, uid)
	if _, err := database.Exec(`INSERT INTO mail_entries
		(id, account_id, imap_uid, imap_folder, subject, sender)
		VALUES (?, ?, ?, ?, ?, 'a@b.c')`, id, account, uid, folder, subject); err != nil {
		t.Fatalf("insert entry uid=%d: %v", uid, err)
	}
	if _, err := database.Exec(`INSERT INTO mail_content (entry_id, body_text, cjk_indexed)
		VALUES (?, ?, 0)`, id, body); err != nil {
		t.Fatalf("insert content uid=%d: %v", uid, err)
	}
	if _, err := database.Exec(`INSERT INTO sync_state (account_id, imap_folder)
		VALUES (?, ?) ON CONFLICT(account_id, imap_folder) DO NOTHING`, account, folder); err != nil {
		t.Fatalf("insert sync_state: %v", err)
	}
}

func TestBackfillCJK_BatchFailureRollsBackAndRetries(t *testing.T) {
	database := openTestDB(t)
	const account, folder = "acc1", "INBOX"
	insertBackfillRow(t, database, account, folder, 1, "关于财务的通知", "正文内容")
	insertBackfillRow(t, database, account, folder, 2, "Weekly report", "plain body")

	// Make marking the first row fail.  The whole batch must roll back: a
	// partially committed batch would advance the cursor past the failure and
	// the folder would later be marked complete with that mail unindexed.
	if _, err := database.Exec(`CREATE TRIGGER fail_mark
		BEFORE UPDATE ON mail_content
		WHEN NEW.entry_id = 'acc1:INBOX:1'
		BEGIN SELECT RAISE(ABORT, 'simulated write error'); END`); err != nil {
		t.Fatalf("create trigger: %v", err)
	}

	client := &Client{cfg: config.AccountConfig{ID: account}}
	if err := client.backfillCJK(database, slog.Default(), folder); err == nil {
		t.Fatal("expected the failing batch to return an error")
	}

	var done int
	var cursor uint32
	if err := database.QueryRow(
		`SELECT cjk_backfill_done, cjk_cursor_uid FROM sync_state
		  WHERE account_id = ? AND imap_folder = ?`, account, folder,
	).Scan(&done, &cursor); err != nil {
		t.Fatal(err)
	}
	if done != 0 || cursor != 0 {
		t.Fatalf("after failure done=%d cursor=%d, want 0 and 0", done, cursor)
	}
	var pending, cjkRows int
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_content WHERE cjk_indexed = 0`).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_cjk_fts`).Scan(&cjkRows); err != nil {
		t.Fatal(err)
	}
	if pending != 2 || cjkRows != 0 {
		t.Fatalf("after failure pending=%d cjk_rows=%d, want 2 and 0", pending, cjkRows)
	}

	if _, err := database.Exec(`DROP TRIGGER fail_mark`); err != nil {
		t.Fatal(err)
	}
	if err := client.backfillCJK(database, slog.Default(), folder); err != nil {
		t.Fatalf("retry: %v", err)
	}
	if err := database.QueryRow(`SELECT cjk_backfill_done FROM sync_state
		WHERE account_id = ? AND imap_folder = ?`, account, folder).Scan(&done); err != nil {
		t.Fatal(err)
	}
	if done != 1 {
		t.Fatalf("done=%d after successful retry, want 1", done)
	}
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_content WHERE cjk_indexed = 0`).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if pending != 0 {
		t.Fatalf("pending=%d after successful retry, want 0", pending)
	}
}

func TestBackfillCJK_DeduplicatesEvenWhenResuming(t *testing.T) {
	database := openTestDB(t)
	const account, folder = "acc1", "INBOX"
	insertBackfillRow(t, database, account, folder, 1, "关于财务的通知", "正文内容")
	insertBackfillRow(t, database, account, folder, 2, "另一封通知", "正文内容")

	// State after a restart that already committed the first batch: the
	// cursor is non-zero, so the old "started from zero" dedupe was skipped.
	if _, err := database.Exec(`UPDATE mail_content SET cjk_indexed = 1
		WHERE entry_id = 'acc1:INBOX:1'`); err != nil {
		t.Fatal(err)
	}
	if _, err := database.Exec(`UPDATE sync_state SET cjk_cursor_uid = 1
		WHERE account_id = ? AND imap_folder = ?`, account, folder); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if _, err := database.Exec(`INSERT INTO mail_cjk_fts (entry_id, text)
			VALUES ('acc1:INBOX:1', '旧 邮 件')`); err != nil {
			t.Fatal(err)
		}
	}

	client := &Client{cfg: config.AccountConfig{ID: account}}
	if err := client.backfillCJK(database, slog.Default(), folder); err != nil {
		t.Fatalf("backfillCJK: %v", err)
	}

	var duplicates, done int
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_cjk_fts
		WHERE entry_id = 'acc1:INBOX:1'`).Scan(&duplicates); err != nil {
		t.Fatal(err)
	}
	if duplicates != 1 {
		t.Fatalf("legacy duplicates = %d, want 1", duplicates)
	}
	if err := database.QueryRow(`SELECT cjk_backfill_done FROM sync_state
		WHERE account_id = ? AND imap_folder = ?`, account, folder).Scan(&done); err != nil {
		t.Fatal(err)
	}
	if done != 1 {
		t.Fatalf("done=%d, want 1", done)
	}
}

func TestBackfillCJK_StopsAtPassBudgetAndResumes(t *testing.T) {
	oldBudget := backfillPassBudget
	backfillPassBudget = time.Nanosecond
	t.Cleanup(func() { backfillPassBudget = oldBudget })

	database := openTestDB(t)
	const account, folder = "acc1", "INBOX"
	for uid := 1; uid <= 250; uid++ {
		insertBackfillRow(t, database, account, folder, uid, "plain", "body")
	}

	client := &Client{cfg: config.AccountConfig{ID: account}}
	if err := client.backfillCJK(database, slog.Default(), folder); err != nil {
		t.Fatalf("first pass: %v", err)
	}
	var pending, done int
	var cursor uint32
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_content WHERE cjk_indexed = 0`).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if err := database.QueryRow(`SELECT cjk_backfill_done, cjk_cursor_uid FROM sync_state
		WHERE account_id = ? AND imap_folder = ?`, account, folder).Scan(&done, &cursor); err != nil {
		t.Fatal(err)
	}
	if pending != 50 || done != 0 || cursor != 200 {
		t.Fatalf("first pass pending=%d done=%d cursor=%d, want 50/0/200", pending, done, cursor)
	}

	// Each pass processes at least one batch and then yields; a later pass
	// reaches the end and marks the folder complete.
	for pass := 0; pass < 5; pass++ {
		if err := client.backfillCJK(database, slog.Default(), folder); err != nil {
			t.Fatalf("resume pass %d: %v", pass, err)
		}
		if err := database.QueryRow(`SELECT cjk_backfill_done FROM sync_state
			WHERE account_id = ? AND imap_folder = ?`, account, folder).Scan(&done); err != nil {
			t.Fatal(err)
		}
		if done == 1 {
			break
		}
	}
	if done != 1 {
		t.Fatal("backfill never marked the folder complete")
	}
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_content WHERE cjk_indexed = 0`).Scan(&pending); err != nil {
		t.Fatal(err)
	}
	if pending != 0 {
		t.Fatalf("pending=%d after resuming, want 0", pending)
	}
}

func TestBackfillPendingOnceIndexesDeselectedFolder(t *testing.T) {
	database := openTestDB(t)
	const account = "acc1"
	insertBackfillRow(t, database, account, "INBOX", 1, "通知", "正文")
	insertBackfillRow(t, database, account, "Archive", 1, "归档通知", "旧正文")

	cfg := &config.Config{Accounts: []config.AccountConfig{{ID: account}}}
	if !backfillPendingOnce(cfg, database, slog.Default()) {
		t.Fatal("backfillPendingOnce reported no work although both folders are pending")
	}

	var archiveDone, inboxDone, archiveIndexed int
	if err := database.QueryRow(`SELECT cjk_backfill_done FROM sync_state
		WHERE account_id = ? AND imap_folder = 'Archive'`, account).Scan(&archiveDone); err != nil {
		t.Fatal(err)
	}
	if err := database.QueryRow(`SELECT cjk_backfill_done FROM sync_state
		WHERE account_id = ? AND imap_folder = 'INBOX'`, account).Scan(&inboxDone); err != nil {
		t.Fatal(err)
	}
	if err := database.QueryRow(`SELECT cjk_indexed FROM mail_content
		WHERE entry_id = 'acc1:Archive:1'`).Scan(&archiveIndexed); err != nil {
		t.Fatal(err)
	}
	if archiveDone != 1 || inboxDone != 1 || archiveIndexed != 1 {
		t.Fatalf("Archive done=%d INBOX done=%d indexed=%d, want 1/1/1",
			archiveDone, inboxDone, archiveIndexed)
	}
}
