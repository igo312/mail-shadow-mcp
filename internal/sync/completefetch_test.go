// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
//
// completefetch_test.go:
// Regression tests for silent message loss during sync.
//
// completeFetch used to require `buf.Flags != nil`, but go-imap decodes
// `FLAGS ()` — the response for mail that has never been read — to a nil slice.
// Every unflagged message was therefore treated as an incomplete response,
// dropped from the batch, and then stepped over by the checkpoint, because the
// FETCH command itself had reported no error. A measured field account lost 455
// of 720 messages in one folder this way, with `skipped=0` and no error logged.
//
// The second test covers the structural safeguard: a server that answers OK
// without sending a response for one UID must not be taken as success either.

package imapsync

import (
	"strings"
	"testing"

	"github.com/dryas/mail-shadow-mcp/internal/config"
)

func newTestClient(t *testing.T, server *poisonServer) *Client {
	t.Helper()
	client, err := NewClient(config.AccountConfig{
		ID:       "acc@example.com",
		Host:     "127.0.0.1",
		Port:     server.port(),
		Username: "user",
		Password: "pass",
		TLSMode:  "none",
		Folders:  []string{"INBOX"},
	})
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	t.Cleanup(func() { client.Close() })
	return client
}

func TestSyncFolder_ImportsMessagesWithoutFlags(t *testing.T) {
	subjects := map[uint32]string{401: "unread 401", 402: "unread 402", 403: "read 403"}
	bodies := map[uint32][]byte{401: []byte("body 401"), 402: []byte("body 402"), 403: []byte("body 403")}

	server := newPoisonServer(t, 0, subjects, bodies)
	// Two of three messages have no flags at all: `FLAGS ()`.
	server.flagsEmpty[401] = true
	server.flagsEmpty[402] = true

	db := openTestDB(t)
	client := newTestClient(t, server)

	if err := client.SyncFolder(db, "INBOX"); err != nil {
		t.Fatalf("SyncFolder: %v", err)
	}

	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM mail_entries WHERE account_id = ?`, "acc@example.com").Scan(&count); err != nil {
		t.Fatalf("count: %v", err)
	}
	if count != 3 {
		t.Fatalf("every message must be imported, got %d of 3", count)
	}

	// A message with no flags is unread, not unknown.
	var isRead int
	if err := db.QueryRow(`SELECT is_read FROM mail_entries WHERE imap_uid = ?`, 401).Scan(&isRead); err != nil {
		t.Fatalf("query is_read: %v", err)
	}
	if isRead != 0 {
		t.Errorf("is_read for an unflagged message: want 0, got %d", isRead)
	}

	lastUID, err := getLastUID(db, "acc@example.com", "INBOX")
	if err != nil {
		t.Fatalf("getLastUID: %v", err)
	}
	if lastUID != 403 {
		t.Errorf("last_uid: want 403, got %d", lastUID)
	}
}

func TestSyncFolder_DoesNotSkipOmittedResponse(t *testing.T) {
	subjects := map[uint32]string{601: "ok 601", 602: "silently skipped 602", 603: "ok 603"}
	bodies := map[uint32][]byte{
		601: []byte("From: a@example.com\r\nSubject: ok 601\r\n\r\nbody 601\r\n"),
		602: []byte("From: raw@example.com\r\nSubject: Recovered 602\r\n\r\nrecovered body\r\n"),
		603: []byte("From: c@example.com\r\nSubject: ok 603\r\n\r\nbody 603\r\n"),
	}

	server := newPoisonServer(t, 0, subjects, bodies)
	// The metadata FETCH returns OK but never mentions UID 602.
	server.omitMetadata[602] = true

	db := openTestDB(t)
	client := newTestClient(t, server)

	if err := client.SyncFolder(db, "INBOX"); err != nil {
		t.Fatalf("SyncFolder: %v", err)
	}

	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM mail_entries WHERE account_id = ?`, "acc@example.com").Scan(&count); err != nil {
		t.Fatalf("count: %v", err)
	}
	if count != 3 {
		t.Fatalf("a missing response must not be skipped silently, got %d of 3", count)
	}

	// The omitted UID is recovered through the raw-literal fallback.
	var subject string
	if err := db.QueryRow(`SELECT subject FROM mail_entries WHERE imap_uid = ?`, 602).Scan(&subject); err != nil {
		t.Fatalf("query recovered message: %v", err)
	}
	if !strings.Contains(subject, "Recovered 602") {
		t.Errorf("recovered subject: got %q", subject)
	}

	lastUID, err := getLastUID(db, "acc@example.com", "INBOX")
	if err != nil {
		t.Fatalf("getLastUID: %v", err)
	}
	if lastUID != 603 {
		t.Errorf("last_uid: want 603, got %d", lastUID)
	}
}
