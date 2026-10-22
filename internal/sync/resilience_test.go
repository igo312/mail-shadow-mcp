// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// resilience_test.go:
// Tests for the poison-message isolation layer: UID set expansion, decode-error
// classification, the quarantine table and local raw-MIME parsing.

package imapsync

import (
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"

	"github.com/dryas/mail-shadow-mcp/internal/config"
)

// testLogger returns a logger that discards everything.
func testLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestExpandUIDSet(t *testing.T) {
	set := imap.UIDSet{
		{Start: 10, Stop: 12},
		{Start: 12, Stop: 13}, // overlaps the previous range
		{Start: 42, Stop: 42},
	}
	got := expandUIDSet(set)
	want := []uint32{10, 11, 12, 13, 42}

	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got %v, want %v", got, want)
		}
	}
}

func TestExpandUIDSet_IgnoresWildcard(t *testing.T) {
	// A range ending in "*" must never be expanded into four billion entries.
	if got := expandUIDSet(imap.UIDSet{{Start: 5, Stop: 0}}); len(got) != 0 {
		t.Errorf("expected no UIDs for a wildcard range, got %v", got)
	}
	if got := expandUIDSet(nil); len(got) != 0 {
		t.Errorf("expected no UIDs for a nil set, got %v", got)
	}
}

func TestUIDsNotIn(t *testing.T) {
	uids := []uint32{1, 2, 3, 4, 5}
	received := map[uint32]struct{}{1: {}, 2: {}, 4: {}}

	got := uidsNotIn(uids, received)
	if len(got) != 2 || got[0] != 3 || got[1] != 5 {
		t.Errorf("got %v, want [3 5]", got)
	}

	if all := uidsNotIn(uids, nil); len(all) != 5 {
		t.Errorf("expected all UIDs when nothing was received, got %v", all)
	}
}

func TestIsDecodeError(t *testing.T) {
	decode := []string{
		`in response-data: in body-type-mpart: in body-type-1part: imapwire: expected string, got "N"`,
		`in response-data: in body-type-1part: imapwire: expected string`,
		`in response-tagged: in fetch: bad item`,
		`in envelope: expected list`,
	}
	for _, s := range decode {
		if !isDecodeError(errString(s)) {
			t.Errorf("expected decode error for %q", s)
		}
	}

	network := []string{
		"use of closed network connection",
		"read tcp 10.0.0.1:443: i/o timeout",
		"EOF",
		"imap: NO [ALERT] mailbox unavailable",
	}
	for _, s := range network {
		if isDecodeError(errString(s)) {
			t.Errorf("did not expect decode error for %q", s)
		}
	}

	if isDecodeError(nil) {
		t.Error("nil must not be a decode error")
	}
}

// errString is a minimal error implementation for classification tests.
type errString string

func (e errString) Error() string { return string(e) }

func TestSetLastUID_NeverGoesBackwards(t *testing.T) {
	database := openTestDB(t)

	if err := setLastUID(database, "acc", "INBOX", 50); err != nil {
		t.Fatalf("setLastUID(50): %v", err)
	}
	// A stale worker must not rewind the checkpoint and cause a re-fetch.
	if err := setLastUID(database, "acc", "INBOX", 40); err != nil {
		t.Fatalf("setLastUID(40): %v", err)
	}

	uid, err := getLastUID(database, "acc", "INBOX")
	if err != nil {
		t.Fatalf("getLastUID: %v", err)
	}
	if uid != 50 {
		t.Errorf("expected the checkpoint to stay at 50, got %d", uid)
	}
}

func TestQuarantine_AccumulatesAttempts(t *testing.T) {
	database := openTestDB(t)
	client := &Client{cfg: config.AccountConfig{ID: "acc"}}

	for i := 0; i < 2; i++ {
		if err := client.quarantine(database, testLogger(), "INBOX", 77, errString("imapwire: boom")); err != nil {
			t.Fatalf("quarantine: %v", err)
		}
	}

	var attempts int
	var reason string
	if err := database.QueryRow(
		`SELECT attempts, reason FROM sync_skipped WHERE account_id = ? AND imap_folder = ? AND imap_uid = ?`,
		"acc", "INBOX", 77,
	).Scan(&attempts, &reason); err != nil {
		t.Fatalf("query quarantine row: %v", err)
	}
	if attempts != 2 {
		t.Errorf("attempts: want 2, got %d", attempts)
	}
	if !strings.Contains(reason, "imapwire") {
		t.Errorf("reason not recorded: %q", reason)
	}
}

func TestImportMessageBatch_ZeroExpectedDoesNotPanic(t *testing.T) {
	database := openTestDB(t)
	client := &Client{cfg: config.AccountConfig{ID: "acc"}}

	msg := &imapclient.FetchMessageBuffer{
		UID: imap.UID(5),
		Envelope: &imap.Envelope{
			Date:    time.Date(2026, 5, 1, 0, 0, 0, 0, time.UTC),
			Subject: "no progress total",
			From:    []imap.Address{{Mailbox: "a", Host: "b.c"}},
		},
	}

	// expected == 0 used to divide by zero in the progress log.
	texts := map[uint32]string{5: "body text"}
	if err := client.importMessageBatch(database, testLogger(), "INBOX", []*imapclient.FetchMessageBuffer{msg}, texts, 0, 0); err != nil {
		t.Fatalf("importMessageBatch: %v", err)
	}

	var n int
	if err := database.QueryRow(`SELECT COUNT(*) FROM mail_entries`).Scan(&n); err != nil {
		t.Fatalf("count: %v", err)
	}
	if n != 1 {
		t.Errorf("expected 1 row, got %d", n)
	}
}

func TestParseRawMessage_Multipart(t *testing.T) {
	raw := strings.Join([]string{
		`From: "Jane Doe" <jane@example.com>`,
		`To: bob@example.com`,
		`Cc: carol@example.com`,
		`Subject: =?UTF-8?B?5L2g5aW9?=`,
		`Date: Mon, 20 Oct 2026 10:00:00 +0800`,
		`Message-Id: <abc123@example.com>`,
		`In-Reply-To: <parent99@example.com>`,
		`Content-Type: multipart/mixed; boundary="BOUND"`,
		``,
		`--BOUND`,
		`Content-Type: text/plain; charset=utf-8`,
		`Content-Transfer-Encoding: quoted-printable`,
		``,
		`Hello=20world=0Aline two`,
		`--BOUND`,
		`Content-Type: application/pdf; name="invoice.pdf"`,
		`Content-Disposition: attachment; filename="invoice.pdf"`,
		``,
		`%PDF-1.4`,
		`--BOUND--`,
		``,
	}, "\r\n")

	msg, err := parseRawMessage([]byte(raw))
	if err != nil {
		t.Fatalf("parseRawMessage: %v", err)
	}

	if msg.Subject != "你好" {
		t.Errorf("subject: want 你好, got %q", msg.Subject)
	}
	if msg.From != `"Jane Doe" <jane@example.com>` {
		t.Errorf("from: got %q", msg.From)
	}
	if msg.To != "bob@example.com" {
		t.Errorf("to: got %q", msg.To)
	}
	if msg.Cc != "carol@example.com" {
		t.Errorf("cc: got %q", msg.Cc)
	}
	if msg.MessageID != "abc123@example.com" {
		t.Errorf("message-id: got %q", msg.MessageID)
	}
	if msg.InReplyTo != "parent99@example.com" {
		t.Errorf("in-reply-to: got %q", msg.InReplyTo)
	}
	if want := "Hello world\nline two"; msg.Text != want {
		t.Errorf("text: want %q, got %q", want, msg.Text)
	}
	if len(msg.Attachments) != 1 {
		t.Fatalf("attachments: want 1, got %d", len(msg.Attachments))
	}
	if msg.Attachments[0].Filename != "invoice.pdf" {
		t.Errorf("attachment filename: got %q", msg.Attachments[0].Filename)
	}
	if msg.Attachments[0].ContentType != "application/pdf" {
		t.Errorf("attachment type: got %q", msg.Attachments[0].ContentType)
	}
	if msg.Attachments[0].SizeBytes <= 0 {
		t.Errorf("attachment size: got %d", msg.Attachments[0].SizeBytes)
	}
}

func TestParseRawMessage_SinglePartBase64(t *testing.T) {
	raw := strings.Join([]string{
		`From: a@b.c`,
		`Subject: Plain`,
		`Content-Type: text/plain; charset=utf-8`,
		`Content-Transfer-Encoding: base64`,
		``,
		`SGVsbG8=`,
		``,
	}, "\r\n")

	msg, err := parseRawMessage([]byte(raw))
	if err != nil {
		t.Fatalf("parseRawMessage: %v", err)
	}
	if msg.Text != "Hello" {
		t.Errorf("text: want Hello, got %q", msg.Text)
	}
	if msg.Attachments != nil {
		t.Errorf("expected no attachments, got %v", msg.Attachments)
	}
}

func TestParseRawMessage_HTMLOnlyIsConvertedToText(t *testing.T) {
	// HTML-only mail contributes readable text, but never raw markup.
	raw := strings.Join([]string{
		`From: a@b.c`,
		`Subject: html only`,
		`Content-Type: multipart/alternative; boundary="B"`,
		``,
		`--B`,
		`Content-Type: text/html`,
		``,
		`<p>hi</p>`,
		`--B--`,
		``,
	}, "\r\n")

	msg, err := parseRawMessage([]byte(raw))
	if err != nil {
		t.Fatalf("parseRawMessage: %v", err)
	}
	if msg.Text != "hi" {
		t.Errorf("expected stripped text %q, got %q", "hi", msg.Text)
	}
}

func TestParseRawMessage_NoContentTypeDefaultsToText(t *testing.T) {
	raw := "From: a@b.c\r\nSubject: bare\r\n\r\njust text\r\n"

	msg, err := parseRawMessage([]byte(raw))
	if err != nil {
		t.Fatalf("parseRawMessage: %v", err)
	}
	if msg.Text != "just text" {
		t.Errorf("text: want 'just text', got %q", msg.Text)
	}
}

func TestNormaliseMessageIDs(t *testing.T) {
	cases := map[string]string{
		`<a@b> <c@d>`: "a@b c@d",
		`  <a@b>  `:   "a@b",
		``:            "",
		`a@b`:         "a@b",
	}
	for in, want := range cases {
		if got := normaliseMessageIDs(in); got != want {
			t.Errorf("normaliseMessageIDs(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestRawEntry_DerivedFields(t *testing.T) {
	buf := &imapclient.FetchMessageBuffer{
		UID:          imap.UID(9),
		Flags:        []imap.Flag{imap.FlagSeen},
		InternalDate: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
	}
	parsed := rawMessage{
		Subject:   "sub",
		From:      "a@b.c",
		MessageID: "mid@x",
		// No Date header: the internal date is used instead.
	}

	entry := rawEntry("acc", "INBOX", 9, buf, parsed)

	if entry.ID != "acc:INBOX:9" {
		t.Errorf("id: got %q", entry.ID)
	}
	if entry.DateUTC != "2026-01-01T00:00:00Z" {
		t.Errorf("date: got %v", entry.DateUTC)
	}
	if entry.IsRead == nil || *entry.IsRead != 1 {
		t.Errorf("is_read: got %v", entry.IsRead)
	}
	if entry.MessageID == nil || *entry.MessageID != "mid@x" {
		t.Errorf("message_id: got %v", entry.MessageID)
	}
	if entry.InReplyTo != nil {
		t.Errorf("in_reply_to: expected nil, got %v", entry.InReplyTo)
	}
}
