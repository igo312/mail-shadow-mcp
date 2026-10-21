// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
//
// bodyfetch_test.go:
// Regression test for the field report that an account with 816 folders took
// hours to sync and kept tripping the server's per-command limit.
//
// Cause: the sync asked for BODY.PEEK[TEXT] in the same FETCH as the metadata.
// In IMAP, BODY[TEXT] is everything after the top-level headers, so every
// attachment was downloaded — one folder of 165 mails was 560 MB of PDFs whose
// readable text was 0.2% of that.
//
// The sync now runs two phases: BODYSTRUCTURE only, then the text/plain parts
// it selected. This test asserts both halves: the body is indexed, the
// attachment metadata is recorded, and no attachment payload is ever requested.

package imapsync

import (
	"strings"
	"testing"

	"github.com/dryas/mail-shadow-mcp/internal/config"
)

func TestSyncFolder_FetchesTextPartsOnly(t *testing.T) {
	const uid = 301
	const textPart = "Hello from the text part"

	server := newPoisonServer(t, 0, map[uint32]string{uid: "Ship report"}, map[uint32][]byte{
		uid: []byte("unused"),
	})
	// text/plain (part 1) plus a 14 KB PDF attachment (part 2).
	server.structures[uid] = `(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 26 1)` +
		`("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 14336 NIL` +
		` ("attachment" ("FILENAME" "report.pdf")) NIL) "MIXED" ("BOUNDARY" "b1") NIL NIL)`
	server.parts[uid] = map[string][]byte{
		"1": []byte(textPart),
		"2": []byte(strings.Repeat("PDFDATA", 1024)),
	}

	db := openTestDB(t)
	cfg := config.AccountConfig{
		ID:       "acc@example.com",
		Host:     "127.0.0.1",
		Port:     server.port(),
		Username: "user",
		Password: "pass",
		TLSMode:  "none",
		Folders:  []string{"INBOX"},
	}

	client, err := NewClient(cfg)
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	defer client.Close()

	if err := client.SyncFolder(db, "INBOX"); err != nil {
		t.Fatalf("SyncFolder: %v", err)
	}

	var body string
	if err := db.QueryRow(`SELECT body_text FROM mail_content c
		JOIN mail_entries e ON e.id = c.entry_id WHERE e.imap_uid = ?`, uid).Scan(&body); err != nil {
		t.Fatalf("query body: %v", err)
	}
	if body != textPart {
		t.Errorf("body text: want %q, got %q", textPart, body)
	}

	// Attachment metadata still comes from BODYSTRUCTURE, so the download entry
	// point keeps working without the payload ever being fetched.
	var filename string
	var size int64
	if err := db.QueryRow(`SELECT filename, size_bytes FROM mail_attachments
		WHERE entry_id = ?`, "acc@example.com:INBOX:301").Scan(&filename, &size); err != nil {
		t.Fatalf("query attachment: %v", err)
	}
	if filename != "report.pdf" {
		t.Errorf("attachment filename: want %q, got %q", "report.pdf", filename)
	}
	if size != 14336 {
		t.Errorf("attachment size: want 14336, got %d", size)
	}

	// The whole point: the PDF payload must never cross the wire.
	for _, items := range server.fetchRequests() {
		if strings.Contains(items, "BODY.PEEK[]") || strings.Contains(items, "BODY[]") {
			t.Errorf("sync fetched the whole message: %s", items)
		}
		if strings.Contains(items, "BODY.PEEK[2]") || strings.Contains(items, "BODY[2]") {
			t.Errorf("sync fetched the attachment part: %s", items)
		}
		if strings.Contains(items, "BODY.PEEK[TEXT]") || strings.Contains(items, "BODY[TEXT]") {
			t.Errorf("sync fetched BODY[TEXT], which includes attachments: %s", items)
		}
	}
	if !strings.Contains(strings.Join(server.fetchRequests(), "\n"), "BODY.PEEK[1]") {
		t.Errorf("expected the text part to be fetched with BODY.PEEK[1], got %v", server.fetchRequests())
	}
}
