// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// poison_test.go:
// End-to-end regression test for the failure reported from the field:
//
//	fetch close error err="in response-data: in body-type-mpart:
//	  in body-type-1part: imapwire: expected string, got \"N\""
//
// One message whose BODYSTRUCTURE cannot be decoded used to kill the IMAP
// connection, which silently truncated the folder, left every later folder
// failing with "use of closed network connection", and — because the checkpoint
// never moved past the bad message — made the next run hang on the same UID.
//
// The test runs a scripted IMAP server that emits exactly that malformed
// response for one UID and asserts that the other messages still sync, that the
// bad one is recovered through the raw-literal fallback, and that the
// checkpoint ends up past all of them.

package imapsync

import (
	"bufio"
	"fmt"
	"net"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/dryas/mail-shadow-mcp/internal/config"
)

// poisonServer is a minimal IMAP server. UID poisonUID gets a BODYSTRUCTURE
// whose subtype is an atom instead of a string, which is precisely what makes
// go-imap's decoder fail with `expected string, got "N"`.
type poisonServer struct {
	ln        net.Listener
	poisonUID uint32
	subjects  map[uint32]string
	bodies    map[uint32][]byte

	// structures overrides the default single-part BODYSTRUCTURE per UID.
	structures map[uint32]string
	// parts holds the payload returned for a numbered body part ("1", "1.1", …).
	parts map[uint32]map[string][]byte

	mu       sync.Mutex
	conns    int
	requests []string
}

func newPoisonServer(t *testing.T, poisonUID uint32, subjects map[uint32]string, bodies map[uint32][]byte) *poisonServer {
	t.Helper()

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}

	s := &poisonServer{
		ln:         ln,
		poisonUID:  poisonUID,
		subjects:   subjects,
		bodies:     bodies,
		structures: map[uint32]string{},
		parts:      map[uint32]map[string][]byte{},
	}
	go s.serve()
	t.Cleanup(func() { ln.Close() })
	return s
}

// fetchRequests returns every FETCH data item list the server received.
func (s *poisonServer) fetchRequests() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]string, len(s.requests))
	copy(out, s.requests)
	return out
}

func (s *poisonServer) port() int {
	return s.ln.Addr().(*net.TCPAddr).Port
}

func (s *poisonServer) connectionCount() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.conns
}

func (s *poisonServer) uids() []uint32 {
	out := make([]uint32, 0, len(s.subjects))
	for uid := range s.subjects {
		out = append(out, uid)
	}
	// The fixture uses two-digit UIDs, so lexical order is enough here.
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j] < out[i] {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}

func (s *poisonServer) serve() {
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		s.mu.Lock()
		s.conns++
		s.mu.Unlock()
		go s.handle(conn)
	}
}

func (s *poisonServer) handle(conn net.Conn) {
	defer conn.Close()

	reader := bufio.NewReader(conn)
	writer := bufio.NewWriter(conn)

	fmt.Fprint(writer, "* OK fake IMAP4rev1 ready\r\n")
	writer.Flush()

	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			return
		}
		line = strings.TrimRight(line, "\r\n")

		tag, command, args := splitCommand(line)
		if tag == "" {
			return
		}

		switch {
		case command == "CAPABILITY":
			fmt.Fprintf(writer, "* CAPABILITY IMAP4rev1\r\n%s OK CAPABILITY completed\r\n", tag)

		case command == "LOGIN":
			fmt.Fprintf(writer, "%s OK LOGIN completed\r\n", tag)

		case command == "NOOP":
			fmt.Fprintf(writer, "%s OK NOOP completed\r\n", tag)

		case command == "SELECT":
			fmt.Fprintf(writer, "* %d EXISTS\r\n", len(s.subjects))
			fmt.Fprint(writer, "* OK [UIDVALIDITY 1] UIDs valid\r\n")
			fmt.Fprintf(writer, "%s OK [READ-WRITE] SELECT completed\r\n", tag)

		case command == "UID" && strings.HasPrefix(args, "SEARCH"):
			fmt.Fprintf(writer, "* SEARCH %s\r\n", joinUIDs(s.uids()))
			fmt.Fprintf(writer, "%s OK SEARCH completed\r\n", tag)

		case command == "UID" && strings.HasPrefix(args, "FETCH"):
			if !s.writeFetch(writer, args) {
				// The malformed response is followed by a closed connection,
				// exactly like a server that keeps going but is no longer
				// readable to this client.
				writer.Flush()
				return
			}
			fmt.Fprintf(writer, "%s OK FETCH completed\r\n", tag)

		default:
			fmt.Fprintf(writer, "%s BAD unsupported\r\n", tag)
		}

		if err := writer.Flush(); err != nil {
			return
		}
	}
}

// writeFetch emits one FETCH response per requested UID. It returns false after
// writing the poison response, meaning the connection must not be used again.
func (s *poisonServer) writeFetch(writer *bufio.Writer, args string) bool {
	// args looks like: "FETCH 101:103 (UID ENVELOPE ... BODY.PEEK[TEXT])"
	rest := strings.TrimPrefix(args, "FETCH ")
	space := strings.IndexByte(rest, ' ')
	if space < 0 {
		return true
	}
	uidSet := rest[:space]
	items := rest[space+1:]

	s.mu.Lock()
	s.requests = append(s.requests, items)
	s.mu.Unlock()

	wantWholeMessage := strings.Contains(items, "BODY.PEEK[]") || strings.Contains(items, "BODY[]")
	wantBodyStructure := strings.Contains(items, "BODYSTRUCTURE")
	wantEnvelope := strings.Contains(items, "ENVELOPE")
	wantFlags := strings.Contains(items, "FLAGS")
	wantInternalDate := strings.Contains(items, "INTERNALDATE")
	wantSize := strings.Contains(items, "RFC822.SIZE")
	sections := bodySections(items)

	for _, uid := range parseUIDSet(uidSet) {
		if _, ok := s.subjects[uid]; !ok {
			continue
		}
		fmt.Fprintf(writer, "* %d FETCH (UID %d", uidIndex(s.uids(), uid), uid)

		if wantEnvelope {
			fmt.Fprintf(writer, " ENVELOPE (%q %q NIL NIL NIL NIL NIL NIL NIL NIL)",
				imapQuotedDate(), s.subjects[uid])
		}
		if wantFlags {
			fmt.Fprint(writer, ` FLAGS (\Seen)`)
		}
		if wantInternalDate {
			fmt.Fprintf(writer, " INTERNALDATE %q", imapQuotedDate())
		}
		if wantSize {
			fmt.Fprintf(writer, " RFC822.SIZE %d", len(s.bodies[uid]))
		}

		if wantBodyStructure {
			if uid == s.poisonUID {
				// The subtype is an atom instead of a quoted string: the decoder
				// reports `expected string, got "N"` and stops for good.
				fmt.Fprint(writer, ` BODYSTRUCTURE ("TEXT" NIL ("CHARSET" "utf-8") NIL NIL "7BIT" 20 1)`)
				return false
			}
			if st, ok := s.structures[uid]; ok {
				fmt.Fprintf(writer, " BODYSTRUCTURE %s", st)
			} else {
				fmt.Fprintf(writer, ` BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" %d 1)`,
					len(s.bodies[uid]))
			}
		}

		switch {
		case wantWholeMessage:
			if !writeLiteral(writer, "BODY[]", s.bodies[uid]) {
				return false
			}
		default:
			for _, label := range sections {
				payload, ok := s.bodyPayload(uid, label)
				if !ok {
					continue
				}
				if !writeLiteral(writer, "BODY["+label+"]", payload) {
					return false
				}
			}
		}

		fmt.Fprint(writer, ")\r\n")
	}

	return true
}

// bodyPayload resolves the bytes for one BODY[...] label.
func (s *poisonServer) bodyPayload(uid uint32, label string) ([]byte, bool) {
	if label == "" || label == "TEXT" {
		body, ok := s.bodies[uid]
		return body, ok
	}
	if parts, ok := s.parts[uid]; ok {
		if payload, ok := parts[label]; ok {
			return payload, true
		}
	}
	return nil, false
}

// writeLiteral emits one literal item: ` BODY[x] {n}\r\n<payload>`.
func writeLiteral(writer *bufio.Writer, item string, payload []byte) bool {
	fmt.Fprintf(writer, " %s {%d}\r\n", item, len(payload))
	_, err := writer.Write(payload)
	return err == nil
}

// bodySections extracts the labels of every BODY[...] item in a FETCH list.
func bodySections(items string) []string {
	var out []string
	rest := items
	for {
		idx := strings.Index(rest, "BODY[")
		peekIdx := strings.Index(rest, "BODY.PEEK[")
		if peekIdx >= 0 && (idx < 0 || peekIdx < idx) {
			idx = peekIdx
		}
		if idx < 0 {
			return out
		}
		open := strings.IndexByte(rest[idx:], '[')
		if open < 0 {
			return out
		}
		open += idx
		closeIdx := strings.IndexByte(rest[open:], ']')
		if closeIdx < 0 {
			return out
		}
		end := open + closeIdx
		out = append(out, rest[open+1:end])
		rest = rest[end+1:]
	}
}

func splitCommand(line string) (tag, command, args string) {
	parts := strings.SplitN(line, " ", 3)
	if len(parts) < 2 {
		return "", "", ""
	}
	tag = parts[0]
	command = strings.ToUpper(parts[1])
	if len(parts) == 3 {
		args = parts[2]
	}
	return tag, command, args
}

func joinUIDs(uids []uint32) string {
	parts := make([]string, 0, len(uids))
	for _, uid := range uids {
		parts = append(parts, strconv.FormatUint(uint64(uid), 10))
	}
	return strings.Join(parts, " ")
}

func uidIndex(uids []uint32, uid uint32) int {
	for i, u := range uids {
		if u == uid {
			return i + 1
		}
	}
	return 1
}

// parseUIDSet expands a simple IMAP sequence set ("101", "101:103", "101,103").
func parseUIDSet(set string) []uint32 {
	var out []uint32
	for _, part := range strings.Split(set, ",") {
		if part == "" {
			continue
		}
		if colon := strings.IndexByte(part, ':'); colon >= 0 {
			start, err1 := strconv.ParseUint(part[:colon], 10, 32)
			stop, err2 := strconv.ParseUint(part[colon+1:], 10, 32)
			if err1 != nil || err2 != nil {
				continue
			}
			for u := start; u <= stop; u++ {
				out = append(out, uint32(u))
			}
			continue
		}
		if n, err := strconv.ParseUint(part, 10, 32); err == nil {
			out = append(out, uint32(n))
		}
	}
	return out
}

func imapQuotedDate() string {
	return time.Date(2026, 10, 20, 10, 0, 0, 0, time.FixedZone("CST", 8*3600)).
		Format("02-Jan-2006 15:04:05 -0700")
}

// TestSyncFolder_SurvivesUndecodableMessage is the regression test for the
// field report: three messages, one of them malformed.
func TestSyncFolder_SurvivesUndecodableMessage(t *testing.T) {
	const poisonUID = 102

	subjects := map[uint32]string{
		101: "Message 101",
		102: "Message 102",
		103: "Message 103",
	}
	bodies := map[uint32][]byte{
		101: []byte("Body of 101"),
		102: []byte("From: raw-sender@example.com\r\nSubject: Recovered 102\r\n" +
			"Date: Mon, 20 Oct 2026 10:00:00 +0800\r\n\r\nRecovered body text 102\r\n"),
		103: []byte("Body of 103"),
	}

	server := newPoisonServer(t, poisonUID, subjects, bodies)

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
		t.Fatalf("SyncFolder must tolerate one undecodable message, got: %v", err)
	}

	// Every message must be present: the two decodable ones from the structured
	// fetch, the malformed one from the raw-literal fallback.
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM mail_entries WHERE account_id = ? AND imap_folder = ?`,
		cfg.ID, "INBOX").Scan(&count); err != nil {
		t.Fatalf("count entries: %v", err)
	}
	if count != 3 {
		t.Fatalf("expected all 3 messages to be imported, got %d", count)
	}

	// The checkpoint must be past the last UID, otherwise the next run would
	// fetch the malformed message again and stall forever.
	lastUID, err := getLastUID(db, cfg.ID, "INBOX")
	if err != nil {
		t.Fatalf("getLastUID: %v", err)
	}
	if lastUID != 103 {
		t.Errorf("last_uid: want 103, got %d", lastUID)
	}

	// Nothing may be left quarantined: the raw fallback recovered the message.
	var skipped int
	if err := db.QueryRow(`SELECT COUNT(*) FROM sync_skipped WHERE account_id = ?`, cfg.ID).Scan(&skipped); err != nil {
		t.Fatalf("count skipped: %v", err)
	}
	if skipped != 0 {
		t.Errorf("expected no quarantined messages, got %d", skipped)
	}

	// The recovered message must carry the headers parsed from the raw literal.
	var subject, bodyText string
	if err := db.QueryRow(`SELECT e.subject, c.body_text
		FROM mail_entries e LEFT JOIN mail_content c ON c.entry_id = e.id
		WHERE e.account_id = ? AND e.imap_uid = ?`, cfg.ID, poisonUID).Scan(&subject, &bodyText); err != nil {
		t.Fatalf("query recovered message: %v", err)
	}
	if subject != "Recovered 102" {
		t.Errorf("recovered subject: want %q, got %q", "Recovered 102", subject)
	}
	if !strings.Contains(bodyText, "Recovered body text 102") {
		t.Errorf("recovered body not indexed: %q", bodyText)
	}

	// Isolation required at least one reconnect (the poison killed the first
	// connection); more than one proves the batch was bisected.
	if conns := server.connectionCount(); conns < 2 {
		t.Errorf("expected the client to reconnect after the decode error, got %d connection(s)", conns)
	}
}

// TestSyncFolder_QuarantinesWhenRawAlsoFails covers the last resort: if even the
// raw literal cannot be parsed, the message is quarantined and the sync still
// completes with the checkpoint moved past it.
func TestSyncFolder_QuarantinesWhenRawAlsoFails(t *testing.T) {
	const poisonUID = 202

	subjects := map[uint32]string{201: "ok 201", 202: "ok 202", 203: "ok 203"}
	bodies := map[uint32][]byte{
		201: []byte("body 201"),
		// Not a parseable RFC 5322 message: no headers at all.
		202: nil,
		203: []byte("body 203"),
	}

	server := newPoisonServer(t, poisonUID, subjects, bodies)

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
		t.Fatalf("SyncFolder must not fail because of one bad message, got: %v", err)
	}

	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM mail_entries WHERE account_id = ?`, cfg.ID).Scan(&count); err != nil {
		t.Fatalf("count entries: %v", err)
	}
	if count != 2 {
		t.Errorf("expected the 2 readable messages to be imported, got %d", count)
	}

	var reason string
	var attempts int
	if err := db.QueryRow(`SELECT reason, attempts FROM sync_skipped
		WHERE account_id = ? AND imap_uid = ?`, cfg.ID, poisonUID).Scan(&reason, &attempts); err != nil {
		t.Fatalf("expected uid %d to be quarantined: %v", poisonUID, err)
	}
	if attempts < 1 {
		t.Errorf("attempts: want >= 1, got %d", attempts)
	}

	lastUID, err := getLastUID(db, cfg.ID, "INBOX")
	if err != nil {
		t.Fatalf("getLastUID: %v", err)
	}
	if lastUID != 203 {
		t.Errorf("last_uid: want 203 (quarantine must not stall the mailbox), got %d", lastUID)
	}
}
