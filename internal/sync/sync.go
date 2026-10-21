// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// sync.go:
// Incremental IMAP → SQLite synchronisation. Connects to each configured
// account, fetches only messages newer than the stored last_uid, writes
// envelope metadata + body text + attachments in batched transactions, and
// updates the FTS5 index. All IMAP operations are read-only (PEEK fetch,
// no STORE/APPEND/EXPUNGE).
//
// Poison-message handling
// -----------------------
// An IMAP response is decoded by a single streaming decoder owned by the
// client. When one response cannot be decoded — e.g. a server that emits a
// malformed BODYSTRUCTURE ("imapwire: expected string, got \"N\"") — the
// decoder stops for good, the connection is torn down, and *every* later
// command on that client fails with "use of closed network connection".
// A single bad message therefore takes the whole mailbox (and, before the
// rework below, every remaining folder of the account) hostage.
//
// The only way to make this per-message is to treat a failed stream as a
// failed *batch*: the batch is retried on a fresh connection and, if it keeps
// failing, bisected until the unparsable UID is isolated. That UID is then
// fetched as a raw literal (BODY.PEEK[]) and parsed locally, which bypasses the
// strict IMAP structure parser entirely. Only if even that fails is the message
// recorded in sync_skipped and skipped, so that the next messages always sync.

// Package imapsync handles incremental synchronization of IMAP mailboxes
// into the local SQLite database.
package imapsync

import (
	"bytes"
	"crypto/tls"
	"database/sql"
	"encoding/base64"
	"fmt"
	"io"
	"log/slog"
	"math"
	"mime/quotedprintable"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"

	"github.com/dryas/mail-shadow-mcp/internal/config"
)

const (
	// syncWindowSize is the number of UIDs requested per FETCH command.
	// Small windows bound the work lost when a connection dies mid-stream and
	// make the bisection that isolates an unparsable message cheap.
	syncWindowSize = 100

	// fetchAttempts is how often a window is retried on a transient failure
	// before it is bisected. Decode errors are never retried: they are
	// deterministic for a given message.
	fetchAttempts = 2

	// rawMessageLimit caps the size of a message fetched as a single literal in
	// the fallback path. Bigger messages are quarantined instead of buffered.
	rawMessageLimit = 32 << 20

	// fetchStallTimeout is how long a FETCH may go without delivering anything
	// before the connection is considered dead. Some servers accept a command
	// and then answer nothing at all (they close the socket minutes later);
	// go-imap has no read deadline, so without this the sync waits forever.
	fetchStallTimeout = 60 * time.Second
)

// Client wraps an authenticated IMAP connection for a single account.
type Client struct {
	cfg    config.AccountConfig
	client *imapclient.Client
}

// NewClient dials the IMAP server and authenticates using PLAIN SASL.
// The caller is responsible for calling Close() when done.
func NewClient(cfg config.AccountConfig) (*Client, error) {
	c, err := dialClient(cfg)
	if err != nil {
		return nil, err
	}
	return &Client{cfg: cfg, client: c}, nil
}

// dialClient opens and authenticates a new IMAP connection.
func dialClient(cfg config.AccountConfig) (*imapclient.Client, error) {
	addr := fmt.Sprintf("%s:%d", cfg.Host, cfg.Port)

	var tlsCfg *tls.Config
	if cfg.TLSSkipVerify {
		tlsCfg = &tls.Config{InsecureSkipVerify: true} //nolint:gosec // intentional, user-configured
		slog.Warn("TLS certificate verification disabled", "account", cfg.ID, "host", cfg.Host)
	}

	opts := &imapclient.Options{TLSConfig: tlsCfg}

	var c *imapclient.Client
	var err error

	switch cfg.TLSMode {
	case "starttls":
		slog.Debug("connecting via STARTTLS", "account", cfg.ID, "addr", addr)
		c, err = imapclient.DialStartTLS(addr, opts)
	case "none":
		slog.Warn("connecting without TLS", "account", cfg.ID, "addr", addr)
		c, err = imapclient.DialInsecure(addr, opts)
	default: // "tls" or empty
		c, err = imapclient.DialTLS(addr, opts)
	}
	if err != nil {
		return nil, fmt.Errorf("sync: dial %s: %w", addr, err)
	}

	if err := authenticate(c, cfg.Username, cfg.Password); err != nil {
		c.Close()
		return nil, fmt.Errorf("sync: authenticate %s: %w", cfg.Username, err)
	}

	return c, nil
}

// Close terminates the IMAP connection.
func (c *Client) Close() error {
	if c.client == nil {
		return nil
	}
	return c.client.Close()
}

// RawClient exposes the underlying imapclient.Client for packages that need
// direct IMAP access (e.g. on-demand attachment download).
func (c *Client) RawClient() *imapclient.Client {
	return c.client
}

// ensureConnected makes sure c.client is a live, authenticated connection with
// folder selected. A client whose decoder hit a bad response is dead forever
// (see the package comment), so it is replaced instead of reused.
func (c *Client) ensureConnected(folder string) error {
	if c.client != nil {
		select {
		case <-c.client.Closed():
			c.client.Close() //nolint:errcheck // already dead
			c.client = nil
		default:
			return nil
		}
	}

	c.client = nil
	nc, err := dialClient(c.cfg)
	if err != nil {
		return err
	}
	if folder != "" {
		if _, err := nc.Select(folder, nil).Wait(); err != nil {
			nc.Close()
			return fmt.Errorf("sync: reselect folder %q: %w", folder, err)
		}
	}
	c.client = nc
	return nil
}

// ListFolders returns all available mailbox names on the server.
func (c *Client) ListFolders() ([]string, error) {
	cmd := c.client.List("", "*", nil)
	mailboxes, err := cmd.Collect()
	if err != nil {
		return nil, fmt.Errorf("sync: list folders: %w", err)
	}

	folders := make([]string, 0, len(mailboxes))
	for _, mb := range mailboxes {
		folders = append(folders, mb.Mailbox)
	}
	return folders, nil
}

// SyncFolder incrementally syncs one IMAP folder into the database.
// It fetches only messages with UIDs greater than the stored last_uid,
// writes them to mail_entries, and updates sync_state after every window of
// syncWindowSize messages so that an interrupted run resumes where it stopped.
// IMPORTANT: Only read commands (SELECT, UID SEARCH, UID FETCH) are issued —
// never APPEND, STORE, COPY, EXPUNGE or any other write command.
func (c *Client) SyncFolder(db *sql.DB, folder string) error {
	logger := slog.With("account", c.cfg.ID, "folder", folder)

	lastUID, err := getLastUID(db, c.cfg.ID, folder)
	if err != nil {
		return fmt.Errorf("sync: get last_uid: %w", err)
	}

	// SELECT the mailbox (read-only via EXAMINE would also work, but SELECT
	// is required to get the real UID validity value in some libs).
	selData, err := c.client.Select(folder, nil).Wait()
	if err != nil {
		return fmt.Errorf("sync: select folder %q: %w", folder, err)
	}

	lastUID, err = checkAndResetUIDValidity(db, logger, c.cfg.ID, folder, lastUID, selData)
	if err != nil {
		return err
	}

	// Ask the server which UIDs are still missing. UID SEARCH transfers no
	// message data, so the list is cheap even for large mailboxes and it gives
	// the bisection below a concrete set of UIDs to work with.
	pending, err := c.pendingUIDs(lastUID)
	if err != nil {
		return fmt.Errorf("sync: list pending uids: %w", err)
	}

	logger.Info("sync start",
		"pending", len(pending),
		"mailbox_total", selData.NumMessages,
		"last_uid", lastUID,
	)

	if len(pending) == 0 {
		logger.Info("folder up to date, no new messages")
		return c.finishFolder(db, logger, folder)
	}

	// Disable FTS5 auto-merge during bulk import — dramatically faster for large
	// batches. We'll run a manual optimize() at the end instead.
	// Use defer to guarantee re-enabling even if we return early on error.
	if _, err := db.Exec(`INSERT INTO mail_content_fts(mail_content_fts, rank) VALUES('automerge', 0)`); err != nil {
		logger.Warn("could not disable FTS automerge", "err", err)
	} else {
		defer func() {
			// Re-enable automerge unless optimizeFTS already did so (it runs on success).
			// The INSERT is idempotent — running it twice is harmless.
			if _, err := db.Exec(`INSERT INTO mail_content_fts(mail_content_fts, rank) VALUES('automerge', 8)`); err != nil {
				logger.Warn("could not re-enable FTS automerge (deferred)", "err", err)
			}
		}()
	}

	imported, skipped, err := c.importWindows(db, logger, folder, pending)
	if err != nil {
		return err
	}

	logger.Info("sync complete, optimizing FTS index...",
		"new_messages", imported, "skipped", skipped)
	persistUIDValidity(db, logger, c.cfg.ID, folder, uint32(selData.UIDValidity))
	optimizeFTS(db, logger)
	return c.finishFolder(db, logger, folder)
}

// importWindows walks the pending UIDs in windows of syncWindowSize and resolves
// every UID in a window before advancing the checkpoint past it.
func (c *Client) importWindows(db *sql.DB, logger *slog.Logger, folder string, pending []uint32) (imported, skipped int, err error) {
	expected := len(pending)
	done := 0

	for start := 0; start < len(pending); start += syncWindowSize {
		end := start + syncWindowSize
		if end > len(pending) {
			end = len(pending)
		}
		window := pending[start:end]

		msgs, bad, ferr := c.fetchResilient(folder, window, metaFetchOptions())
		if ferr != nil {
			return imported, skipped, fmt.Errorf("sync: fetch uid %d..%d: %w", window[0], window[len(window)-1], ferr)
		}

		// Second phase: only the readable text of each message, never the
		// attachment payloads. A message whose text cannot be fetched is left to
		// the raw fallback below rather than imported with an empty body.
		texts, badBody, terr := c.fetchTextBodies(folder, msgs)
		if terr != nil {
			return imported, skipped, fmt.Errorf("sync: fetch body text uid %d..%d: %w",
				window[0], window[len(window)-1], terr)
		}
		bad = append(bad, badBody...)
		if len(badBody) > 0 {
			msgs = withoutUIDs(msgs, badBody)
		}

		if len(msgs) > 0 {
			if serr := c.importMessageBatch(db, logger, folder, msgs, texts, done, expected); serr != nil {
				return imported, skipped, serr
			}
			imported += len(msgs)
			done += len(msgs)
		}

		// The structured fetch could not decode these messages. Try the raw
		// literal path, which does not depend on the IMAP structure parser.
		for _, uid := range bad {
			rerr := c.ingestRaw(db, logger, folder, uid)
			if rerr == nil {
				imported++
				done++
				continue
			}
			logger.Error("message unreadable in every form — quarantining so the rest can sync",
				"uid", uid, "err", rerr)
			if qerr := c.quarantine(db, logger, folder, uid, rerr); qerr != nil {
				return imported, skipped, qerr
			}
			skipped++
		}

		// Every UID in this window has now been stored, recovered or
		// quarantined, so the checkpoint may move past it.
		if serr := setLastUID(db, c.cfg.ID, folder, window[len(window)-1]); serr != nil {
			return imported, skipped, serr
		}
	}

	return imported, skipped, nil
}

// metaFetchOptions is the first of the two fetch phases: message metadata only,
// no body payload.
//
// BODY[TEXT] looks like "the text part" but is actually everything after the
// top-level headers, so it drags every attachment across the wire. One ship
// folder measured here was 165 messages / 560 MB of mostly PDFs whose readable
// text was 0.2% of the payload — and a 100-message window of that size exceeds
// the server's ~5 minute per-command limit, which is what made the whole sync
// crawl. Bodies are therefore fetched separately, part by part, in
// fetchTextBodies.
func metaFetchOptions() *imap.FetchOptions {
	return &imap.FetchOptions{
		UID:           true,
		Envelope:      true,
		InternalDate:  true,
		Flags:         true,
		BodyStructure: &imap.FetchItemBodyStructure{Extended: true},
	}
}

// textSections lists the body sections that hold a message's readable text.
//
// Only text/plain leaf parts qualify: attachments (PDFs, images, archives) are
// never requested, text/html is skipped because decoding it would only add
// markup to the index, and a single-part message that is not text (a bare PDF
// mail, say) contributes no section at all instead of downloading its payload.
// Messages without sections are still indexed by subject and sender.
func textSections(bs imap.BodyStructure) []*imap.FetchItemBodySection {
	switch root := bs.(type) {
	case *imap.BodyStructureSinglePart:
		if root.MediaType() != "text/plain" {
			return nil
		}
		// A non-multipart message has no part numbers: its body is the text.
		return []*imap.FetchItemBodySection{{Specifier: imap.PartSpecifierText, Peek: true}}

	case *imap.BodyStructureMultiPart:
		var out []*imap.FetchItemBodySection
		root.Walk(func(path []int, part imap.BodyStructure) bool {
			single, ok := part.(*imap.BodyStructureSinglePart)
			if !ok {
				return true // multipart node: keep descending
			}
			if single.MediaType() != "text/plain" {
				return true
			}
			if disp := single.Disposition(); disp != nil && strings.EqualFold(disp.Value, "attachment") {
				return true
			}
			if len(path) == 0 {
				return true
			}
			out = append(out, &imap.FetchItemBodySection{
				Part: append([]int(nil), path...),
				Peek: true,
			})
			return true
		})
		return out

	default:
		return nil
	}
}

// sectionKey identifies a group of body sections so that every message needing
// the same parts can share one FETCH command.
func sectionKey(sections []*imap.FetchItemBodySection) string {
	var sb strings.Builder
	for _, s := range sections {
		sb.WriteString(string(s.Specifier))
		for _, n := range s.Part {
			sb.WriteString(strconv.Itoa(n))
			sb.WriteByte('.')
		}
		sb.WriteByte('|')
	}
	return sb.String()
}

// fetchResilient fetches the given UIDs and returns the buffers that could be
// decoded plus the UIDs that the server sends in an unparsable form.
//
// It never gives up on the rest of the batch: when the response stream breaks,
// the batch is split in half and each half is retried on a fresh connection
// until the offending UID is isolated. A non-nil error is only returned when the
// connection cannot be re-established at all.
func (c *Client) fetchResilient(folder string, uids []uint32, opts *imap.FetchOptions) (msgs []*imapclient.FetchMessageBuffer, bad []uint32, err error) {
	if len(uids) == 0 {
		return nil, nil, nil
	}

	got, received, ferr := c.fetchWindow(folder, uids, opts)
	msgs = append(msgs, got...)

	// A UID counts as resolved only if its response actually arrived. A command
	// that ends without an error but without every response (a server that drops
	// some UIDs, a response rejected as incomplete) must not be mistaken for
	// success: the checkpoint advances over this window, so a missing message
	// would never be fetched again.
	rest := uidsNotIn(uids, received)
	if len(rest) == 0 {
		// Everything arrived; an error raised after the last response is
		// harmless because the data is complete.
		return msgs, nil, nil
	}
	if len(rest) == 1 {
		// The single remaining UID has now failed on its own: it is the message
		// the server does not send in a usable form.
		return msgs, rest, nil
	}

	if ferr != nil {
		// The stream is dead; a new connection is needed before anything else.
		if cerr := c.ensureConnected(folder); cerr != nil {
			return msgs, nil, cerr
		}
	}

	mid := len(rest) / 2
	leftMsgs, leftBad, lerr := c.fetchResilient(folder, rest[:mid], opts)
	if lerr != nil {
		return append(msgs, leftMsgs...), leftBad, lerr
	}
	rightMsgs, rightBad, rerr := c.fetchResilient(folder, rest[mid:], opts)
	msgs = append(append(msgs, leftMsgs...), rightMsgs...)
	bad = append(leftBad, rightBad...)
	return msgs, bad, rerr
}

// fetchTextBodies is the second fetch phase: it downloads only the sections
// chosen by textSections, grouping messages that need the same parts into a
// single command. A window therefore costs a couple of small fetches instead of
// one response that drags every attachment along.
//
// The returned map holds the decoded body text per UID (empty when the message
// has nothing readable). UIDs whose text could not be fetched are returned in
// bad so the caller can fall back to the raw literal.
func (c *Client) fetchTextBodies(folder string, msgs []*imapclient.FetchMessageBuffer) (map[uint32]string, []uint32, error) {
	texts := make(map[uint32]string, len(msgs))
	if len(msgs) == 0 {
		return texts, nil, nil
	}

	byUID := make(map[uint32]*imapclient.FetchMessageBuffer, len(msgs))
	var order []string
	sections := make(map[string][]*imap.FetchItemBodySection)
	grouped := make(map[string][]uint32)

	for _, msg := range msgs {
		uid := uint32(msg.UID)
		byUID[uid] = msg
		secs := textSections(msg.BodyStructure)
		if len(secs) == 0 {
			// Nothing readable (attachment-only or HTML-only mail): index the
			// metadata and leave the body empty rather than fetching a payload.
			texts[uid] = ""
			continue
		}
		k := sectionKey(secs)
		if _, ok := grouped[k]; !ok {
			order = append(order, k)
			sections[k] = secs
		}
		grouped[k] = append(grouped[k], uid)
	}

	var bad []uint32
	for _, k := range order {
		uids := grouped[k]
		got, failed, err := c.fetchResilient(folder, uids, &imap.FetchOptions{
			UID:         true,
			BodySection: sections[k],
		})
		if err != nil {
			return nil, nil, err
		}

		arrived := make(map[uint32]struct{}, len(got))
		for _, buf := range got {
			uid := uint32(buf.UID)
			src := byUID[uid]
			if src == nil {
				continue
			}
			arrived[uid] = struct{}{}
			texts[uid] = extractTextFromSections(src.BodyStructure, buf)
		}

		bad = append(bad, failed...)
		for _, uid := range uids {
			if _, ok := arrived[uid]; ok {
				continue
			}
			if slices.Contains(failed, uid) {
				continue
			}
			// The server returned no section for this UID; treat it as failed so
			// the message is recovered instead of stored without a body.
			bad = append(bad, uid)
		}
	}

	slices.Sort(bad)
	return texts, slices.Compact(bad), nil
}

// withoutUIDs returns msgs with the UIDs listed in drop removed.
func withoutUIDs(msgs []*imapclient.FetchMessageBuffer, drop []uint32) []*imapclient.FetchMessageBuffer {
	skip := make(map[uint32]struct{}, len(drop))
	for _, uid := range drop {
		skip[uid] = struct{}{}
	}
	out := make([]*imapclient.FetchMessageBuffer, 0, len(msgs))
	for _, msg := range msgs {
		if _, ok := skip[uint32(msg.UID)]; ok {
			continue
		}
		out = append(out, msg)
	}
	return out
}

// fetchWindow runs a single FETCH and reports which UIDs actually arrived.
// A non-nil error means the response stream was not fully usable.
func (c *Client) fetchWindow(folder string, uids []uint32, opts *imap.FetchOptions) ([]*imapclient.FetchMessageBuffer, map[uint32]struct{}, error) {
	var (
		lastMsgs     []*imapclient.FetchMessageBuffer
		lastReceived map[uint32]struct{}
		lastErr      error
	)

	for attempt := 1; attempt <= fetchAttempts; attempt++ {
		if err := c.ensureConnected(folder); err != nil {
			return nil, nil, err
		}
		msgs, received, err := c.fetchOnce(uids, opts)
		lastMsgs, lastReceived = msgs, received
		if err == nil {
			return msgs, received, nil
		}
		lastErr = err
		if isDecodeError(err) {
			// Deterministic for this data — retrying the same request would
			// write an identical malformed response.
			break
		}
	}
	return lastMsgs, lastReceived, lastErr
}

// fetchOnce issues one FETCH and collects everything that could be decoded.
//
// A response that stops arriving mid-command would otherwise block the whole
// sync forever: go-imap has no read deadline, so the guard below closes the
// connection and lets fetchWindow retry (and, if needed, bisect) instead of
// hanging on one silent server.
func (c *Client) fetchOnce(uids []uint32, opts *imap.FetchOptions) ([]*imapclient.FetchMessageBuffer, map[uint32]struct{}, error) {
	var uidSet imap.UIDSet
	for _, uid := range uids {
		uidSet.AddNum(imap.UID(uid))
	}

	client := c.client
	guard := newStallGuard(fetchStallTimeout, func() {
		slog.Warn("imap fetch stalled, dropping the connection", "uids", len(uids), "timeout", fetchStallTimeout)
		client.Close() //nolint:errcheck // the connection is considered lost
	})
	defer guard.stop()

	fetchCmd := client.Fetch(uidSet, opts)
	received := make(map[uint32]struct{}, len(uids))
	var msgs []*imapclient.FetchMessageBuffer

	for {
		msg := fetchCmd.Next()
		if msg == nil {
			break
		}
		guard.touch()
		buf, err := msg.Collect()
		if err != nil {
			// The literal could not be read; the stream is most likely gone.
			break
		}
		guard.touch()
		if !completeFetch(buf, opts) {
			// A response cut short by a decode error can still deliver a partial
			// buffer (typically ENVELOPE before a malformed BODYSTRUCTURE).
			// Storing it would silently lose the body text, so leave the UID
			// unresolved: the caller recovers it from the raw message instead.
			continue
		}
		received[uint32(buf.UID)] = struct{}{}
		msgs = append(msgs, buf)
	}

	closeErr := fetchCmd.Close()
	return msgs, received, closeErr
}

// stallGuard calls onStall when touch has not been called for d. It is used to
// bound a FETCH command that the server never finishes.
type stallGuard struct {
	mu     sync.Mutex
	after  time.Duration
	timer  *time.Timer
	fired  bool
	closed bool
}

func newStallGuard(after time.Duration, onStall func()) *stallGuard {
	g := &stallGuard{after: after}
	g.timer = time.AfterFunc(after, func() {
		g.mu.Lock()
		if g.closed {
			g.mu.Unlock()
			return
		}
		g.fired = true
		g.closed = true
		g.mu.Unlock()
		onStall()
	})
	return g
}

// touch restarts the countdown; call it for every sign of progress.
func (g *stallGuard) touch() {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.closed {
		return
	}
	g.timer.Reset(g.after)
}

// stop disarms the guard.
func (g *stallGuard) stop() {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.closed = true
	g.timer.Stop()
}

// completeFetch reports whether a fetch buffer contains every requested item.
//
// FLAGS is deliberately not checked. go-imap decodes `FLAGS ()` — the normal
// response for mail that has never been read — to a nil slice, so a message
// without flags is indistinguishable from one whose FLAGS item never arrived.
// Treating nil as "missing" silently dropped every unflagged message while the
// checkpoint still advanced past it, i.e. permanent data loss with no error.
// is_read then stays NULL and is resolved by backfillFlags.
func completeFetch(buf *imapclient.FetchMessageBuffer, opts *imap.FetchOptions) bool {
	if opts.Envelope && buf.Envelope == nil {
		return false
	}
	if opts.BodyStructure != nil && buf.BodyStructure == nil {
		return false
	}
	if len(opts.BodySection) > 0 && len(buf.BodySection) == 0 {
		return false
	}
	return true
}

// isDecodeError reports whether the error comes from the IMAP response decoder
// rather than from the network. Such errors repeat identically for the same
// message, so retrying is pointless.
func isDecodeError(err error) bool {
	if err == nil {
		return false
	}
	s := err.Error()
	for _, marker := range []string{
		"imapwire",
		"in response",
		"in body-type",
		"in body-structure",
		"in envelope",
		"in fetch",
		"in address",
	} {
		if strings.Contains(s, marker) {
			return true
		}
	}
	return false
}

// pendingUIDs returns every UID in the selected mailbox greater than after,
// in ascending order.
func (c *Client) pendingUIDs(after uint32) ([]uint32, error) {
	if after == math.MaxUint32 {
		return nil, nil
	}

	var set imap.UIDSet
	set.AddRange(imap.UID(after+1), 0) // 0 means "*" (highest UID)

	data, err := c.client.UIDSearch(&imap.SearchCriteria{UID: []imap.UIDSet{set}}, nil).Wait()
	if err != nil {
		return nil, err
	}

	uids := expandUIDSet(data.All)
	// RFC 3501: a range of "x:*" always matches the last message, even when x is
	// higher than every assigned UID. Drop whatever the checkpoint already covers.
	out := make([]uint32, 0, len(uids))
	for _, uid := range uids {
		if uid > after {
			out = append(out, uid)
		}
	}
	return out, nil
}

// expandUIDSet flattens a UID set response into a sorted, duplicate-free slice.
func expandUIDSet(set imap.NumSet) []uint32 {
	uidSet, ok := set.(imap.UIDSet)
	if !ok {
		return nil
	}

	var out []uint32
	for _, r := range uidSet {
		start, stop := uint32(r.Start), uint32(r.Stop)
		if stop == 0 || stop < start {
			// A wildcard never appears in a server response; ignore it rather
			// than expanding it to four billion UIDs.
			continue
		}
		for u := start; ; u++ {
			out = append(out, u)
			if u == stop || u == math.MaxUint32 {
				break
			}
		}
	}
	slices.Sort(out)
	return slices.Compact(out)
}

// uidsNotIn returns the entries of uids that are not present in received,
// preserving order.
func uidsNotIn(uids []uint32, received map[uint32]struct{}) []uint32 {
	if len(received) == 0 {
		return uids
	}
	out := make([]uint32, 0, len(uids))
	for _, uid := range uids {
		if _, ok := received[uid]; !ok {
			out = append(out, uid)
		}
	}
	return out
}

// finishFolder runs the resumable post-processing steps. None of them may fail
// the folder sync: they are best effort and retried on the next run.
func (c *Client) finishFolder(db *sql.DB, logger *slog.Logger, folder string) error {
	if err := c.retrySkipped(db, logger, folder); err != nil {
		logger.Warn("skipped-message retry failed", "err", err)
	}
	if err := c.backfillFlags(db, logger, folder); err != nil {
		logger.Warn("flag backfill failed", "err", err)
	}
	if err := c.backfillEnvelope(db, logger, folder); err != nil {
		logger.Warn("envelope backfill failed", "err", err)
	}
	return nil
}

// quarantine records a message that cannot be fetched in any form. The UID is
// remembered so it is retried later instead of silently disappearing, and it is
// never retried in the main sync path — that is what would stall the mailbox.
func (c *Client) quarantine(db *sql.DB, logger *slog.Logger, folder string, uid uint32, cause error) error {
	reason := ""
	if cause != nil {
		reason = cause.Error()
	}
	now := time.Now().UTC().Format(time.RFC3339)

	if _, err := db.Exec(`
		INSERT INTO sync_skipped (account_id, imap_folder, imap_uid, reason, attempts, first_seen, last_seen)
		VALUES (?, ?, ?, ?, 1, ?, ?)
		ON CONFLICT(account_id, imap_folder, imap_uid) DO UPDATE SET
			reason    = excluded.reason,
			attempts  = sync_skipped.attempts + 1,
			last_seen = excluded.last_seen`,
		c.cfg.ID, folder, uid, reason, now, now,
	); err != nil {
		return fmt.Errorf("sync: quarantine uid=%d: %w", uid, err)
	}

	logger.Error("message quarantined — the server response for this UID cannot be parsed",
		"uid", uid, "reason", reason,
		"hint", "listed in table sync_skipped; retried on every sync via the raw-literal fallback")
	return nil
}

// retrySkipped re-attempts every quarantined message through the raw-literal
// path. Messages that succeed are removed from sync_skipped.
func (c *Client) retrySkipped(db *sql.DB, logger *slog.Logger, folder string) error {
	rows, err := db.Query(
		`SELECT imap_uid FROM sync_skipped WHERE account_id = ? AND imap_folder = ? ORDER BY imap_uid`,
		c.cfg.ID, folder,
	)
	if err != nil {
		return fmt.Errorf("retrySkipped: query: %w", err)
	}
	var uids []uint32
	for rows.Next() {
		var uid uint32
		if err := rows.Scan(&uid); err == nil {
			uids = append(uids, uid)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("retrySkipped: iterate: %w", err)
	}
	rows.Close()

	if len(uids) == 0 {
		return nil
	}

	logger.Info("retrying quarantined messages", "count", len(uids))
	for _, uid := range uids {
		if err := c.ingestRaw(db, logger, folder, uid); err != nil {
			_, _ = db.Exec(
				`UPDATE sync_skipped SET attempts = attempts + 1, reason = ?, last_seen = ?
				 WHERE account_id = ? AND imap_folder = ? AND imap_uid = ?`,
				err.Error(), time.Now().UTC().Format(time.RFC3339), c.cfg.ID, folder, uid,
			)
			continue
		}
		if _, err := db.Exec(
			`DELETE FROM sync_skipped WHERE account_id = ? AND imap_folder = ? AND imap_uid = ?`,
			c.cfg.ID, folder, uid,
		); err != nil {
			logger.Warn("could not clear quarantined message", "uid", uid, "err", err)
		}
	}
	return nil
}

// ingestRaw fetches one message as a single raw literal and parses it locally.
// This is the fallback for messages whose BODYSTRUCTURE or ENVELOPE the server
// sends in a form the IMAP decoder rejects: a literal is length-prefixed, so no
// structure parsing is involved at all.
func (c *Client) ingestRaw(db *sql.DB, logger *slog.Logger, folder string, uid uint32) error {
	if err := c.ensureConnected(folder); err != nil {
		return err
	}

	var uidSet imap.UIDSet
	uidSet.AddNum(imap.UID(uid))

	fetchCmd := c.client.Fetch(uidSet, &imap.FetchOptions{
		UID:          true,
		Flags:        true,
		InternalDate: true,
		RFC822Size:   true,
		BodySection:  []*imap.FetchItemBodySection{{Peek: true}}, // BODY.PEEK[]
	})

	var buf *imapclient.FetchMessageBuffer
	for {
		msg := fetchCmd.Next()
		if msg == nil {
			break
		}
		b, err := msg.Collect()
		if err != nil {
			break
		}
		buf = b
	}
	if err := fetchCmd.Close(); err != nil {
		return fmt.Errorf("raw fetch uid=%d: %w", uid, err)
	}
	if buf == nil {
		return fmt.Errorf("raw fetch uid=%d: no message returned", uid)
	}
	if buf.RFC822Size > rawMessageLimit {
		return fmt.Errorf("raw fetch uid=%d: message exceeds the %d byte raw limit", uid, rawMessageLimit)
	}

	var raw []byte
	for _, section := range buf.BodySection {
		raw = section.Bytes
		break
	}
	if len(raw) == 0 {
		return fmt.Errorf("raw fetch uid=%d: empty body", uid)
	}

	parsed, err := parseRawMessage(raw)
	if err != nil {
		return fmt.Errorf("raw parse uid=%d: %w", uid, err)
	}

	entry := rawEntry(c.cfg.ID, folder, uid, buf, parsed)
	if err := c.storeEntry(db, entry, parsed.Text, parsed.Attachments); err != nil {
		return err
	}

	logger.Info("message recovered via raw literal fetch",
		"uid", uid, "subject", entry.Subject, "attachments", len(parsed.Attachments))
	return nil
}

// rawEntry builds the DB row for a message that was parsed locally.
func rawEntry(accountID, folder string, uid uint32, buf *imapclient.FetchMessageBuffer, p rawMessage) mailEntry {
	var dateUTC any
	switch {
	case !p.Date.IsZero():
		dateUTC = p.Date.UTC().Format(time.RFC3339)
	case !buf.InternalDate.IsZero():
		dateUTC = buf.InternalDate.UTC().Format(time.RFC3339)
	}

	isRead, isReplied := flagInts(buf.Flags)

	return mailEntry{
		ID:           fmt.Sprintf("%s:%s:%d", accountID, folder, uid),
		AccountID:    accountID,
		IMAPUID:      uid,
		IMAPFolder:   folder,
		Subject:      p.Subject,
		Sender:       p.From,
		RecipientsTo: p.To,
		RecipientsCC: p.Cc,
		DateUTC:      dateUTC,
		IsRead:       isRead,
		IsReplied:    isReplied,
		MessageID:    nullableString(p.MessageID),
		InReplyTo:    nullableString(p.InReplyTo),
	}
}

// checkAndResetUIDValidity compares the server UIDValidity against the stored value.
// If they differ the local data is purged and lastUID is reset to 0.
func checkAndResetUIDValidity(db *sql.DB, logger *slog.Logger, accountID, folder string, lastUID uint32, selData *imap.SelectData) (uint32, error) {
	if uint32(selData.UIDValidity) == 0 {
		return lastUID, nil
	}
	storedValidity, err := getStoredUIDValidity(db, accountID, folder)
	if err != nil {
		return 0, fmt.Errorf("sync: get uid_validity: %w", err)
	}
	if storedValidity == 0 || storedValidity == uint32(selData.UIDValidity) {
		return lastUID, nil
	}
	logger.Warn("UIDValidity changed — purging stale data and re-syncing",
		"stored", storedValidity, "server", selData.UIDValidity)
	if _, err := db.Exec(
		`DELETE FROM mail_entries WHERE account_id = ? AND imap_folder = ?`,
		accountID, folder,
	); err != nil {
		return 0, fmt.Errorf("sync: purge stale entries: %w", err)
	}
	if _, err := db.Exec(
		`UPDATE sync_state SET last_uid = 0, uid_validity = ? WHERE account_id = ? AND imap_folder = ?`,
		uint32(selData.UIDValidity), accountID, folder,
	); err != nil {
		return 0, fmt.Errorf("sync: reset sync_state: %w", err)
	}
	return 0, nil
}

// importMessageBatch writes one slice of fetched messages into a single DB
// transaction. The checkpoint is advanced by the caller once every UID of the
// window is resolved, so an interrupted run can never skip a message that was
// never stored.
func (c *Client) importMessageBatch(db *sql.DB, logger *slog.Logger, folder string, batch []*imapclient.FetchMessageBuffer, texts map[uint32]string, doneBefore, expected int) error {
	tx, err := db.Begin()
	if err != nil {
		return fmt.Errorf("sync: begin transaction: %w", err)
	}

	stmts, err := prepareBatchStmts(tx)
	if err != nil {
		tx.Rollback()
		return err
	}

	const progressInterval = 50
	dbWindowStart := time.Now()
	for j, msg := range batch {
		if msg.Envelope == nil {
			continue
		}
		uid := uint32(msg.UID)
		entry := buildEntry(c.cfg.ID, folder, uid, msg)

		res, err := stmts.entry.Exec(
			entry.ID, entry.AccountID, entry.IMAPUID, entry.IMAPFolder,
			entry.Subject, entry.Sender, entry.RecipientsTo, entry.RecipientsCC,
			entry.DateUTC, entry.IsRead, entry.IsReplied,
			entry.MessageID, entry.InReplyTo,
		)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("sync: insert entry uid=%d: %w", uid, err)
		}

		// Only index in FTS if the entry was genuinely new (rows affected = 1).
		if n, _ := res.RowsAffected(); n > 0 {
			if err := indexNewMessage(tx, stmts, entry, msg, texts[uid], uid); err != nil {
				tx.Rollback()
				return err
			}
		}

		done := doneBefore + j + 1
		if done%progressInterval == 0 || (expected > 0 && done == expected) {
			percent := 0
			if expected > 0 {
				percent = done * 100 / expected
			}
			logger.Info("import progress",
				"processed", done, "total", expected,
				"percent", percent,
				"last_50_db_ms", time.Since(dbWindowStart).Milliseconds(),
			)
			dbWindowStart = time.Now()
		}
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("sync: commit batch: %w", err)
	}
	return nil
}

// storeEntry writes one locally parsed message (raw-literal fallback path).
func (c *Client) storeEntry(db *sql.DB, entry mailEntry, body string, attachments []attachmentInfo) error {
	tx, err := db.Begin()
	if err != nil {
		return fmt.Errorf("sync: begin transaction: %w", err)
	}
	stmts, err := prepareBatchStmts(tx)
	if err != nil {
		tx.Rollback()
		return err
	}

	res, err := stmts.entry.Exec(
		entry.ID, entry.AccountID, entry.IMAPUID, entry.IMAPFolder,
		entry.Subject, entry.Sender, entry.RecipientsTo, entry.RecipientsCC,
		entry.DateUTC, entry.IsRead, entry.IsReplied,
		entry.MessageID, entry.InReplyTo,
	)
	if err != nil {
		tx.Rollback()
		return fmt.Errorf("sync: insert raw entry uid=%d: %w", entry.IMAPUID, err)
	}

	if n, _ := res.RowsAffected(); n > 0 {
		if _, err := stmts.ftsIns.Exec(entry.ID, entry.Subject, body); err != nil {
			tx.Rollback()
			return fmt.Errorf("sync: fts insert uid=%d: %w", entry.IMAPUID, err)
		}
		if _, err := stmts.contentIns.Exec(entry.ID, body); err != nil {
			tx.Rollback()
			return fmt.Errorf("sync: content insert uid=%d: %w", entry.IMAPUID, err)
		}
		for _, att := range attachments {
			if _, err := stmts.att.Exec(entry.ID, att.Filename, att.ContentType, att.SizeBytes); err != nil {
				tx.Rollback()
				return fmt.Errorf("sync: insert attachment uid=%d: %w", entry.IMAPUID, err)
			}
		}
	}

	if err := tx.Commit(); err != nil {
		return fmt.Errorf("sync: commit raw entry uid=%d: %w", entry.IMAPUID, err)
	}
	return nil
}

// batchStmts holds pre-compiled statements for a single import transaction.
type batchStmts struct {
	entry      *sql.Stmt
	ftsIns     *sql.Stmt
	contentIns *sql.Stmt
	att        *sql.Stmt
}

func prepareBatchStmts(tx *sql.Tx) (*batchStmts, error) {
	entry, err := tx.Prepare(`
		INSERT INTO mail_entries
			(id, account_id, imap_uid, imap_folder, subject, sender,
			 recipients_to, recipients_cc, date_utc, is_read, is_replied,
			 message_id, in_reply_to)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		ON CONFLICT(id) DO NOTHING`)
	if err != nil {
		return nil, fmt.Errorf("sync: prepare entry stmt: %w", err)
	}
	ftsIns, err := tx.Prepare(`INSERT INTO mail_content_fts (entry_id, subject, body_text) VALUES (?, ?, ?)`)
	if err != nil {
		return nil, fmt.Errorf("sync: prepare fts ins stmt: %w", err)
	}
	contentIns, err := tx.Prepare(`INSERT OR IGNORE INTO mail_content (entry_id, body_text) VALUES (?, ?)`)
	if err != nil {
		return nil, fmt.Errorf("sync: prepare content ins stmt: %w", err)
	}
	att, err := tx.Prepare(`INSERT OR IGNORE INTO mail_attachments (entry_id, filename, content_type, size_bytes) VALUES (?, ?, ?, ?)`)
	if err != nil {
		return nil, fmt.Errorf("sync: prepare att stmt: %w", err)
	}
	return &batchStmts{entry: entry, ftsIns: ftsIns, contentIns: contentIns, att: att}, nil
}

// indexNewMessage inserts FTS, content, and attachment rows for a newly imported message.
func indexNewMessage(tx *sql.Tx, stmts *batchStmts, entry mailEntry, msg *imapclient.FetchMessageBuffer, body string, uid uint32) error {
	if _, err := stmts.ftsIns.Exec(entry.ID, entry.Subject, body); err != nil {
		return fmt.Errorf("sync: fts insert uid=%d: %w", uid, err)
	}
	if _, err := stmts.contentIns.Exec(entry.ID, body); err != nil {
		return fmt.Errorf("sync: content insert uid=%d: %w", uid, err)
	}
	for _, att := range extractAttachments(msg.BodyStructure) {
		if _, err := stmts.att.Exec(entry.ID, att.Filename, att.ContentType, att.SizeBytes); err != nil {
			return fmt.Errorf("sync: insert attachment uid=%d: %w", uid, err)
		}
	}
	return nil
}

// persistUIDValidity saves the server UIDValidity value to sync_state.
func persistUIDValidity(db *sql.DB, logger *slog.Logger, accountID, folder string, validity uint32) {
	if validity == 0 {
		return
	}
	if _, err := db.Exec(
		`INSERT INTO sync_state (account_id, imap_folder, last_uid, uid_validity) VALUES (?, ?, 0, ?)
		ON CONFLICT(account_id, imap_folder) DO UPDATE SET uid_validity = excluded.uid_validity`,
		accountID, folder, validity,
	); err != nil {
		logger.Warn("could not persist uid_validity", "err", err)
	}
}

// optimizeFTS runs FTS5 optimize and re-enables auto-merge.
func optimizeFTS(db *sql.DB, logger *slog.Logger) {
	if _, err := db.Exec(`INSERT INTO mail_content_fts(mail_content_fts) VALUES('optimize')`); err != nil {
		logger.Warn("FTS optimize failed", "err", err)
	}
	if _, err := db.Exec(`INSERT INTO mail_content_fts(mail_content_fts, rank) VALUES('automerge', 8)`); err != nil {
		logger.Warn("could not re-enable FTS automerge", "err", err)
	}
}

// backfillFlags fetches FLAGS from IMAP for all mail_entries rows in this folder
// that still have is_read IS NULL (i.e. synced before R2 was deployed).
// It processes UIDs in batches of 1000 so a crash mid-way is resumable.
func (c *Client) backfillFlags(db *sql.DB, logger *slog.Logger, folder string) error {
	const batchSize = 1000

	// Collect all UIDs that still need backfill.
	rows, err := db.Query(
		`SELECT imap_uid FROM mail_entries WHERE account_id = ? AND imap_folder = ? AND is_read IS NULL ORDER BY imap_uid`,
		c.cfg.ID, folder,
	)
	if err != nil {
		return fmt.Errorf("backfillFlags: query null rows: %w", err)
	}
	var uids []uint32
	for rows.Next() {
		var uid uint32
		if err := rows.Scan(&uid); err == nil {
			uids = append(uids, uid)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("backfillFlags: iterate rows: %w", err)
	}
	rows.Close()

	if len(uids) == 0 {
		return nil
	}
	logger.Info("flag backfill needed", "count", len(uids))

	for start := 0; start < len(uids); start += batchSize {
		end := start + batchSize
		if end > len(uids) {
			end = len(uids)
		}
		batch := uids[start:end]

		var uidSet imap.UIDSet
		for _, uid := range batch {
			uidSet.AddNum(imap.UID(uid))
		}

		fetchCmd := c.client.Fetch(uidSet, &imap.FetchOptions{UID: true, Flags: true})
		type flagResult struct {
			uid       uint32
			isRead    int
			isReplied int
		}
		var results []flagResult
		for {
			msg := fetchCmd.Next()
			if msg == nil {
				break
			}
			buf, err := msg.Collect()
			if err != nil {
				logger.Warn("backfillFlags: collect error", "err", err)
				continue
			}
			isRead, isReplied := 0, 0
			for _, f := range buf.Flags {
				switch f {
				case imap.FlagSeen:
					isRead = 1
				case imap.FlagAnswered:
					isReplied = 1
				}
			}
			results = append(results, flagResult{uint32(buf.UID), isRead, isReplied})
		}
		if err := fetchCmd.Close(); err != nil {
			// Leave the rows NULL instead of writing guessed flags: the batch is
			// retried on the next sync, and the next folder still gets its turn.
			logger.Warn("backfillFlags: fetch failed, leaving batch untouched", "err", err)
			if cerr := c.ensureConnected(folder); cerr != nil {
				return fmt.Errorf("backfillFlags: reconnect: %w", cerr)
			}
			continue
		}

		// Write results in a single transaction per batch.
		tx, err := db.Begin()
		if err != nil {
			return fmt.Errorf("backfillFlags: begin tx: %w", err)
		}
		stmt, err := tx.Prepare(`UPDATE mail_entries SET is_read=?, is_replied=? WHERE account_id=? AND imap_folder=? AND imap_uid=?`)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("backfillFlags: prepare: %w", err)
		}
		for _, r := range results {
			if _, err := stmt.Exec(r.isRead, r.isReplied, c.cfg.ID, folder, r.uid); err != nil {
				logger.Warn("backfillFlags: update row", "uid", r.uid, "err", err)
			}
		}
		stmt.Close()
		if err := tx.Commit(); err != nil {
			return fmt.Errorf("backfillFlags: commit: %w", err)
		}
		logger.Info("flag backfill batch done", "uids_in_batch", len(batch), "flags_received", len(results))
	}
	return nil
}

// backfillEnvelope fetches ENVELOPE from IMAP for all mail_entries rows in this
// folder that still have message_id IS NULL (i.e. synced before R6 was deployed).
// It processes UIDs in batches of 500 and is resumable: a crash mid-way will
// be continued on the next server start. This can take a while for large mailboxes.
func (c *Client) backfillEnvelope(db *sql.DB, logger *slog.Logger, folder string) error {
	const batchSize = 500

	// Only fetch UIDs that still have no message_id.
	rows, err := db.Query(
		`SELECT imap_uid FROM mail_entries WHERE account_id = ? AND imap_folder = ? AND message_id IS NULL ORDER BY imap_uid`,
		c.cfg.ID, folder,
	)
	if err != nil {
		return fmt.Errorf("backfillEnvelope: query null rows: %w", err)
	}
	var uids []uint32
	for rows.Next() {
		var uid uint32
		if err := rows.Scan(&uid); err == nil {
			uids = append(uids, uid)
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return fmt.Errorf("backfillEnvelope: iterate rows: %w", err)
	}
	rows.Close()

	if len(uids) == 0 {
		return nil
	}
	logger.Info("envelope backfill needed — fetching message-id/in-reply-to for existing mails (this may take a while)",
		"count", len(uids))

	total := len(uids)
	for start := 0; start < total; start += batchSize {
		end := start + batchSize
		if end > total {
			end = total
		}
		batch := uids[start:end]

		// A malformed ENVELOPE kills the connection just like a malformed
		// BODYSTRUCTURE, so the batch is bisected on failure and the offending
		// UIDs are written as "processed but empty" by the pre-population below.
		msgs, bad, err := c.fetchResilient(folder, batch, &imap.FetchOptions{UID: true, Envelope: true})
		if err != nil {
			return fmt.Errorf("backfillEnvelope: fetch: %w", err)
		}
		for _, uid := range bad {
			logger.Warn("backfillEnvelope: unparsable envelope, leaving message-id empty", "uid", uid)
		}

		// Use string (not *string) so that empty values write "" instead of NULL.
		// The backfill query selects WHERE message_id IS NULL; writing "" marks a row
		// as "processed" even when the mail has no Message-ID header, preventing the
		// backfill from running again on every restart for those messages.
		type envResult struct {
			uid       uint32
			msgID     string
			inReplyTo string
		}
		// Pre-populate results with empty strings for every UID in the batch.
		// If IMAP doesn't return a message (e.g. expunged), the row is still marked
		// as processed so it doesn't trigger the backfill again.
		uidToIdx := make(map[uint32]int, len(batch))
		results := make([]envResult, len(batch))
		for i, uid := range batch {
			results[i] = envResult{uid: uid}
			uidToIdx[uid] = i
		}
		for _, buf := range msgs {
			if buf.Envelope == nil {
				continue
			}
			if idx, ok := uidToIdx[uint32(buf.UID)]; ok {
				results[idx].msgID = strings.TrimSpace(buf.Envelope.MessageID)
				results[idx].inReplyTo = strings.TrimSpace(strings.Join(buf.Envelope.InReplyTo, " "))
			}
		}

		tx, err := db.Begin()
		if err != nil {
			return fmt.Errorf("backfillEnvelope: begin tx: %w", err)
		}
		stmt, err := tx.Prepare(`UPDATE mail_entries SET message_id=?, in_reply_to=? WHERE account_id=? AND imap_folder=? AND imap_uid=?`)
		if err != nil {
			tx.Rollback()
			return fmt.Errorf("backfillEnvelope: prepare: %w", err)
		}
		for _, r := range results {
			if _, err := stmt.Exec(r.msgID, r.inReplyTo, c.cfg.ID, folder, r.uid); err != nil {
				logger.Warn("backfillEnvelope: update row", "uid", r.uid, "err", err)
			}
		}
		stmt.Close()
		if err := tx.Commit(); err != nil {
			return fmt.Errorf("backfillEnvelope: commit: %w", err)
		}
		logger.Info("envelope backfill batch done",
			"processed", end,
			"total", total,
			"percent", end*100/total,
		)
	}
	return nil
}

type mailEntry struct {
	ID           string
	AccountID    string
	IMAPUID      uint32
	IMAPFolder   string
	Subject      string
	Sender       string
	RecipientsTo string
	RecipientsCC string
	DateUTC      any     // nil when the message has no Date header
	IsRead       *int    // nil = unknown; 0 = unread; 1 = read
	IsReplied    *int    // nil = unknown; 0 = not replied; 1 = replied
	MessageID    *string // nil = not yet fetched
	InReplyTo    *string // nil = not yet fetched
}

func buildEntry(accountID, folder string, uid uint32, msg *imapclient.FetchMessageBuffer) mailEntry {
	env := msg.Envelope

	id := fmt.Sprintf("%s:%s:%d", accountID, folder, uid)

	var sender string
	if len(env.From) > 0 {
		sender = formatAddresses(env.From)
	}

	var dateUTC any
	if !env.Date.IsZero() {
		// Store as RFC3339 string so SQLite string comparisons work correctly.
		// Passing time.Time directly causes the driver to store Go's default
		// "2006-01-02 15:04:05 +0000 UTC" format, which breaks >= / <= filters
		// against RFC3339 strings (space 0x20 < 'T' 0x54 lexicographically).
		dateUTC = env.Date.UTC().Format(time.RFC3339)
	}

	isRead, isReplied := flagInts(msg.Flags)
	msgID := nullableString(strings.TrimSpace(env.MessageID))
	inReplyTo := nullableString(strings.TrimSpace(strings.Join(env.InReplyTo, " ")))
	return mailEntry{
		ID:           id,
		AccountID:    accountID,
		IMAPUID:      uid,
		IMAPFolder:   folder,
		Subject:      env.Subject,
		Sender:       sender,
		RecipientsTo: formatAddresses(env.To),
		RecipientsCC: formatAddresses(env.Cc),
		DateUTC:      dateUTC,
		IsRead:       isRead,
		IsReplied:    isReplied,
		MessageID:    msgID,
		InReplyTo:    inReplyTo,
	}
}

// nullableString returns nil for empty strings, otherwise a pointer to the value.
func nullableString(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// flagInts converts an IMAP flags slice into int pointers.
//
// A nil slice is *not* "unknown": go-imap decodes `FLAGS ()` — a message that
// has no flags, i.e. any mail that was never read — to nil, and treating that as
// unknown left every unread message with is_read NULL and sent backfillFlags
// after it on each run. Nil therefore means "no flags": unread, unanswered.
func flagInts(flags []imap.Flag) (*int, *int) {
	read, replied := 0, 0
	for _, f := range flags {
		switch f {
		case imap.FlagSeen:
			read = 1
		case imap.FlagAnswered:
			replied = 1
		}
	}
	return &read, &replied
}

func getLastUID(db *sql.DB, accountID, folder string) (uint32, error) {
	var lastUID uint32
	err := db.QueryRow(
		`SELECT last_uid FROM sync_state WHERE account_id = ? AND imap_folder = ?`,
		accountID, folder,
	).Scan(&lastUID)
	if err == sql.ErrNoRows {
		return 0, nil
	}
	return lastUID, err
}

// setLastUID advances the checkpoint for a folder without moving it backwards.
// It is the only place the checkpoint is written, so a window is only ever
// marked as done after every UID in it has been stored, recovered or
// quarantined.
func setLastUID(db *sql.DB, accountID, folder string, uid uint32) error {
	if _, err := db.Exec(`
		INSERT INTO sync_state (account_id, imap_folder, last_uid) VALUES (?, ?, ?)
		ON CONFLICT(account_id, imap_folder) DO UPDATE SET last_uid = MAX(sync_state.last_uid, excluded.last_uid)`,
		accountID, folder, uid,
	); err != nil {
		return fmt.Errorf("sync: advance last_uid to %d: %w", uid, err)
	}
	return nil
}

func getStoredUIDValidity(db *sql.DB, accountID, folder string) (uint32, error) {
	var v uint32
	err := db.QueryRow(
		`SELECT uid_validity FROM sync_state WHERE account_id = ? AND imap_folder = ?`,
		accountID, folder,
	).Scan(&v)
	if err == sql.ErrNoRows {
		return 0, nil
	}
	return v, err
}

// formatAddresses joins a list of IMAP addresses as "Name <addr>" comma-separated strings.
func formatAddresses(addrs []imap.Address) string {
	parts := make([]string, 0, len(addrs))
	for _, a := range addrs {
		addr := a.Addr()
		if addr == "" {
			continue
		}
		if a.Name != "" {
			parts = append(parts, fmt.Sprintf("%s <%s>", a.Name, addr))
		} else {
			parts = append(parts, addr)
		}
	}
	return strings.Join(parts, ", ")
}

// attachmentInfo holds metadata for one attachment part.
type attachmentInfo struct {
	Filename    string
	ContentType string
	SizeBytes   int64
}

// extractAttachments walks the BodyStructure and collects metadata for all
// parts that have a Content-Disposition of "attachment".
func extractAttachments(bs imap.BodyStructure) []attachmentInfo {
	if bs == nil {
		return nil
	}
	var result []attachmentInfo
	bs.Walk(func(path []int, part imap.BodyStructure) bool {
		single, ok := part.(*imap.BodyStructureSinglePart)
		if !ok {
			return true // descend into multipart
		}
		disp := single.Disposition()
		if disp == nil || !strings.EqualFold(disp.Value, "attachment") {
			return true
		}
		result = append(result, attachmentInfo{
			Filename:    single.Filename(),
			ContentType: single.MediaType(),
			SizeBytes:   int64(single.Size),
		})
		return true
	})
	return result
}

// extractTextFromSections decodes the body sections returned by the text fetch
// phase and joins them. bs is the message's BODYSTRUCTURE from the metadata
// phase and supplies the transfer encoding of each part; it may be nil, in
// which case the bytes are taken as they arrived.
func extractTextFromSections(bs imap.BodyStructure, msg *imapclient.FetchMessageBuffer) string {
	if msg == nil {
		return ""
	}

	var parts []string
	for _, section := range msg.BodySection {
		text := strings.TrimSpace(decodeBody(section.Bytes, encodingForSection(bs, section.Section)))
		if text != "" {
			parts = append(parts, text)
		}
	}
	return strings.Join(parts, "\n")
}

// encodingForSection finds the Content-Transfer-Encoding of the part a fetched
// body section belongs to.
func encodingForSection(bs imap.BodyStructure, section *imap.FetchItemBodySection) string {
	if bs == nil || section == nil {
		return ""
	}
	if section.Specifier == imap.PartSpecifierText {
		// BODY[TEXT] of a non-multipart message: the root part's encoding.
		if sp, ok := bs.(*imap.BodyStructureSinglePart); ok {
			return sp.Encoding
		}
		return ""
	}
	if len(section.Part) == 0 {
		return ""
	}

	var enc string
	bs.Walk(func(path []int, part imap.BodyStructure) bool {
		if !slices.Equal(path, section.Part) {
			return true
		}
		if sp, ok := part.(*imap.BodyStructureSinglePart); ok {
			enc = sp.Encoding
		}
		return false
	})
	return enc
}

// decodeBody decodes a message body from its transfer encoding (quoted-printable or base64).
func decodeBody(b []byte, encoding string) string {
	switch strings.ToLower(strings.TrimSpace(encoding)) {
	case "quoted-printable":
		decoded, err := io.ReadAll(quotedprintable.NewReader(bytes.NewReader(b)))
		if err == nil {
			return string(decoded)
		}
	case "base64":
		// Strip whitespace that MIME base64 may contain
		clean := strings.Map(func(r rune) rune {
			if r == '\n' || r == '\r' || r == ' ' || r == '\t' {
				return -1
			}
			return r
		}, string(b))
		decoded, err := io.ReadAll(base64.NewDecoder(base64.StdEncoding, strings.NewReader(clean)))
		if err == nil {
			return string(decoded)
		}
	}
	return string(b)
}
