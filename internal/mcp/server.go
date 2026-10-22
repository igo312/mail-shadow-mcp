// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// server.go:
// Wires seven MCP tools onto a mark3labs/mcp-go server:
//
//	list_accounts_and_folders — enumerate synced accounts and folders
//	get_recent_activity       — N most recent emails, with optional filters
//	get_email_content         — full body + attachments for a single email
//	search_emails             — FTS5 full-text search with metadata filters
//	get_thread                — all emails in a thread via message-id/in-reply-to
//	download_attachments      — fetch attachment files from IMAP on demand
//	get_download_link         — generate a temporary HTTP download URL for an attachment (fallback only)
//	delete_mail               — soft-delete an email by moving it to a configured trash folder via IMAP MOVE

// Package mcpserver wires the MCP tools onto a mark3labs/mcp-go server.
package mcpserver

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"

	"github.com/emersion/go-imap/v2"

	"github.com/dryas/mail-shadow-mcp/internal/attachment"
	"github.com/dryas/mail-shadow-mcp/internal/config"
	maildb "github.com/dryas/mail-shadow-mcp/internal/db"
	"github.com/dryas/mail-shadow-mcp/internal/fileserver"
	imapsync "github.com/dryas/mail-shadow-mcp/internal/sync"
)

const (
	// maxEmailBodyRunes bounds get_email_content responses.  The SQL query
	// fetches at most one rune more than the limit, so a huge body never has to
	// be materialised in memory just to be truncated in Go.
	maxEmailBodyRunes = 20_000

	// maxSearchBodyRunes bounds include_body results.  Without this a single
	// search over 100 HTML/newsletter bodies could return hundreds of
	// megabytes, which makes MCP clients appear to hang.
	maxSearchBodyRunes = 4_000

	// maxThreadMessages bounds get_thread; a pathological thread can otherwise
	// return an unbounded number of messages.
	maxThreadMessages = 500

	// dbQueryTimeout bounds read-only tool queries.  A CJK substring fallback or
	// an unindexed filter on a multi-million-row mailbox can otherwise keep the
	// single SQLite connection busy long enough for the agent to time out.  A
	// clear error is better than a silent hang; the caller can narrow filters.
	dbQueryTimeout = 20 * time.Second
)

// substringScanLimit bounds how many of the most recent messages the CJK
// substring fallback examines.  A LIKE '%term%' over every body in a large
// mailbox is a full table scan and was the main reason a Chinese search could
// appear to hang.  The default keeps the scan in the low tens of thousands of
// rows; callers see scan_limited=true when this path was used.
var substringScanLimit = 20_000

// queryCtx adds the read-query budget unless the caller already supplied an
// earlier deadline.
func queryCtx(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, dbQueryTimeout)
}

// truncateRunes shortens s to at most limit runes and reports whether it did.
func truncateRunes(s string, limit int) (string, bool) {
	if limit <= 0 {
		return s, false
	}
	runes := []rune(s)
	if len(runes) <= limit {
		return s, false
	}
	return string(runes[:limit]) + "\n[... truncated]", true
}

// New creates and returns a configured MCP server with all tools registered.
// db is the open SQLite connection; cfg is the loaded configuration.
// fs may be nil if the file server is disabled.
func New(db *sql.DB, cfg *config.Config, version string, fs *fileserver.Server) *server.MCPServer {
	s := server.NewMCPServer(
		"mail-shadow-mcp",
		version,
		server.WithToolCapabilities(false),
		server.WithRecovery(),
	)

	s.AddTool(toolListAccountsAndFolders(), handleListAccountsAndFolders(db, cfg))
	s.AddTool(toolGetRecentActivity(), handleGetRecentActivity(db, cfg))
	s.AddTool(toolGetEmailContent(), handleGetEmailContent(db))
	s.AddTool(toolSearchEmails(), handleSearchEmails(db, cfg))
	s.AddTool(toolGetThread(), handleGetThread(db, cfg))
	s.AddTool(toolDownloadAttachments(), handleDownloadAttachments(cfg))
	s.AddTool(toolDeleteMail(), handleDeleteMail(cfg, db))
	if fs != nil {
		s.AddTool(toolGetDownloadLink(), handleGetDownloadLink(cfg, fs))
	}

	return s
}

// ---------------------------------------------------------------------------
// normalizeDateTo expands a plain YYYY-MM-DD date_to value to the end of that day
// so that same-day messages are included in the filter.
// "2026-05-28" → "2026-05-28T23:59:59Z"  (RFC3339 values are returned unchanged)
func normalizeDateTo(v string) string {
	if len(v) == 10 { // "2006-01-02"
		return v + "T23:59:59Z"
	}
	return v
}

// validateDate checks that a date string is parseable as RFC3339 or YYYY-MM-DD.
// Returns a human-readable error string, or "" if valid or empty.
func validateDate(param, value string) string {
	if value == "" {
		return ""
	}
	for _, layout := range []string{time.RFC3339, "2006-01-02"} {
		if _, err := time.Parse(layout, value); err == nil {
			return ""
		}
	}
	return fmt.Sprintf("invalid %s %q: use RFC3339 (e.g. 2024-01-15T00:00:00Z) or YYYY-MM-DD (e.g. 2024-01-15)", param, value)
}

// validateHasAttachments checks that has_attachments is "", "true", or "false".
func validateHasAttachments(value string) string {
	switch value {
	case "", "true", "false":
		return ""
	}
	return fmt.Sprintf("invalid has_attachments %q: must be \"true\", \"false\", or omitted", value)
}

func validateIsRead(value string) string {
	switch value {
	case "", "true", "false":
		return ""
	}
	return fmt.Sprintf("invalid is_read %q: must be \"true\", \"false\", or omitted", value)
}

// fmtDBError formats a database error, providing extra guidance for FTS5 syntax errors.
func fmtDBError(err error) string {
	if errors.Is(err, context.DeadlineExceeded) {
		return fmt.Sprintf("query timed out after %s; narrow the filters (account, folder, date range) and retry", dbQueryTimeout)
	}
	s := err.Error()
	if strings.Contains(s, "fts5") || strings.Contains(s, "syntax error") {
		return fmt.Sprintf("invalid search query syntax (%v). Use simple space-separated keywords (e.g. invoice april).", err)
	}
	return fmt.Sprintf("db error: %v", err)
}

// ---------------------------------------------------------------------------
// queryBuilder — minimal SQL fragment assembler
// ---------------------------------------------------------------------------

type queryBuilder struct {
	parts []string
	args  []any
}

// write appends a raw SQL fragment.
func (qb *queryBuilder) write(s string) {
	qb.parts = append(qb.parts, s)
}

// and appends " AND <cond>" — use when WHERE is already in the base query.
func (qb *queryBuilder) and(cond string, args ...any) {
	qb.parts = append(qb.parts, " AND "+cond)
	qb.args = append(qb.args, args...)
}

func (qb *queryBuilder) sql() string {
	return strings.Join(qb.parts, "")
}

// toCountQuery returns a new queryBuilder that runs SELECT COUNT(*) with the
// same WHERE conditions but without ORDER BY / LIMIT / OFFSET.
// Convention: the last part is always the ORDER BY+LIMIT+OFFSET fragment, and
// the last two args are always limit and offset — both are stripped here.
// The inner query is wrapped as a subquery to avoid SELECT-clause surgery.
func (qb *queryBuilder) toCountQuery() *queryBuilder {
	// Build inner query: all parts except the last (ORDER BY LIMIT OFFSET).
	inner := strings.Join(qb.parts[:len(qb.parts)-1], "")
	// Args: drop last two (limit, offset).
	var innerArgs []any
	if len(qb.args) >= 2 {
		innerArgs = qb.args[:len(qb.args)-2]
	}
	cq := &queryBuilder{}
	cq.parts = []string{"SELECT COUNT(*) FROM (" + inner + ")"}
	cq.args = append(cq.args, innerArgs...)
	return cq
}

// ---------------------------------------------------------------------------
// Tool: list_accounts_and_folders
// ---------------------------------------------------------------------------

func toolListAccountsAndFolders() mcp.Tool {
	return mcp.NewTool("list_accounts_and_folders",
		mcp.WithDescription("Lists all configured email accounts and their synced folders, including sync state (last seen UID)."),
		mcp.WithTitleAnnotation("List Accounts & Folders"),
		mcp.WithReadOnlyHintAnnotation(true),
		mcp.WithDestructiveHintAnnotation(false),
		mcp.WithIdempotentHintAnnotation(true),
		mcp.WithOpenWorldHintAnnotation(false),
	)
}

type folderInfo struct {
	Folder string `json:"folder"`
}

type accountInfo struct {
	AccountID string       `json:"account_id"`
	Folders   []folderInfo `json:"folders"`
}

func handleListAccountsAndFolders(db *sql.DB, cfg *config.Config) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		ctx, cancel := queryCtx(ctx)
		defer cancel()
		slog.Info("tool called", "tool", "list_accounts_and_folders")
		rows, err := db.QueryContext(ctx,
			`SELECT account_id, imap_folder, last_uid FROM sync_state ORDER BY account_id, imap_folder`)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("db error: %v", err)), nil
		}
		defer rows.Close()

		byAccount := map[string]*accountInfo{}
		for rows.Next() {
			var aid, folder string
			var lastUID int64
			if err := rows.Scan(&aid, &folder, &lastUID); err != nil {
				continue
			}
			if _, ok := byAccount[aid]; !ok {
				byAccount[aid] = &accountInfo{AccountID: aid}
			}
			byAccount[aid].Folders = append(byAccount[aid].Folders, folderInfo{Folder: folder})
		}
		if err := rows.Err(); err != nil {
			slog.Warn("list_accounts_and_folders: row iteration error", "err", err)
		}

		// Add accounts from config that have no sync state yet.
		for _, acc := range cfg.Accounts {
			if _, ok := byAccount[acc.ID]; !ok {
				byAccount[acc.ID] = &accountInfo{AccountID: acc.ID, Folders: []folderInfo{}}
			}
		}

		result := make([]*accountInfo, 0, len(byAccount))
		for _, a := range byAccount {
			result = append(result, a)
		}

		out, _ := json.MarshalIndent(result, "", "  ")
		return mcp.NewToolResultText(string(out)), nil
	}
}

// pagedResult wraps a paginated tool response with total_count metadata.
type pagedResult[T any] struct {
	TotalCount  int  `json:"total_count"`
	Offset      int  `json:"offset"`
	Limit       int  `json:"limit"`
	ScanLimited bool `json:"scan_limited,omitempty"`
	ScanLimit   int  `json:"scan_limit,omitempty"`
	Results     []T  `json:"results"`
}

// ---------------------------------------------------------------------------
// Tool: get_recent_activity
// ---------------------------------------------------------------------------

func toolGetRecentActivity() mcp.Tool {
	return mcp.NewTool("get_recent_activity",
		mcp.WithDescription("Returns the N most recently received emails across ALL folders and accounts, sorted by date descending. All parameters are optional filters — omit them to get a global view across everything. The response includes total_count so you know how many results exist in total for pagination."),
		mcp.WithTitleAnnotation("Get Recent Activity"),
		mcp.WithReadOnlyHintAnnotation(true),
		mcp.WithDestructiveHintAnnotation(false),
		mcp.WithIdempotentHintAnnotation(true),
		mcp.WithOpenWorldHintAnnotation(false),
		mcp.WithString("account",
			mcp.Description("Optional: restrict to a specific account ID. Omit to include all accounts."),
		),
		mcp.WithString("folder",
			mcp.Description("Optional: restrict to a specific IMAP folder (e.g. INBOX). Omit to include all folders."),
		),
		mcp.WithNumber("limit",
			mcp.Description("Optional: maximum number of results. Defaults to 10. Max 100. Always set this explicitly when the user asks for a specific number."),
			mcp.DefaultNumber(10),
			mcp.Min(1),
			mcp.Max(100),
		),
		mcp.WithNumber("offset",
			mcp.Description("Optional: number of results to skip for pagination. Use with limit to page through results. Defaults to 0."),
			mcp.DefaultNumber(0),
			mcp.Min(0),
		),
		mcp.WithString("has_attachments",
			mcp.Description(`Optional: filter by attachment presence. "true" = only emails with attachments, "false" = only without. Omit to include all.`),
		),
		mcp.WithString("is_read",
			mcp.Description(`Optional: filter by read status. "true" = only read emails, "false" = only unread. Omit to include all.`),
		),
	)
}

type mailSummary struct {
	ID           string             `json:"id"`
	AccountID    string             `json:"account_id"`
	Folder       string             `json:"folder"`
	Subject      string             `json:"subject"`
	Sender       string             `json:"sender"`
	RecipientsTo string             `json:"recipients_to"`
	DateUTC      *string            `json:"date_utc"`
	IsRead       *bool              `json:"is_read"`
	IsReplied    *bool              `json:"is_replied"`
	Attachments  []attachmentDetail `json:"attachments"`
}

// fetchAttachmentsSubquery is reused across the read handlers
const fetchAttachmentsSubquery = `COALESCE((SELECT json_group_array(json_object('filename',COALESCE(filename,''),'content_type',COALESCE(content_type,''),'size_bytes',COALESCE(size_bytes,0)))
			           FROM mail_attachments WHERE entry_id = e.id), '[]')`

func handleGetRecentActivity(db *sql.DB, cfg *config.Config) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		ctx, cancel := queryCtx(ctx)
		defer cancel()
		account := req.GetString("account", "")
		folder := req.GetString("folder", "")
		limit := int(req.GetFloat("limit", 10))
		offset := int(req.GetFloat("offset", 0))
		hasAttachments := req.GetString("has_attachments", "")
		isRead := req.GetString("is_read", "")
		slog.Info("tool called", "tool", "get_recent_activity", "account", account, "folder", folder, "limit", limit, "offset", offset, "has_attachments", hasAttachments)
		if msg := validateHasAttachments(hasAttachments); msg != "" {
			return mcp.NewToolResultError(msg), nil
		}
		if msg := validateIsRead(isRead); msg != "" {
			return mcp.NewToolResultError(msg), nil
		}
		if limit <= 0 || limit > 100 {
			limit = 10
		}
		if offset < 0 {
			offset = 0
		}

		qb := &queryBuilder{}
		qb.write(`SELECT e.id, e.account_id, e.imap_folder, e.subject, e.sender, e.recipients_to, e.date_utc, e.is_read, e.is_replied, ` +
			fetchAttachmentsSubquery +
			` FROM mail_entries e WHERE 1=1`)
		if account != "" {
			qb.and("e.account_id = ?", account)
		}
		if folder != "" {
			qb.and("e.imap_folder = ?", folder)
		}
		if hasAttachments == "true" {
			qb.and("EXISTS (SELECT 1 FROM mail_attachments WHERE entry_id = e.id)")
		} else if hasAttachments == "false" {
			qb.and("NOT EXISTS (SELECT 1 FROM mail_attachments WHERE entry_id = e.id)")
		}
		if isRead == "true" {
			qb.and("e.is_read = 1")
		} else if isRead == "false" {
			qb.and("e.is_read = 0")
		}
		applyTrashExclusions(qb, cfg, "e")
		qb.write(` ORDER BY e.date_utc DESC NULLS LAST LIMIT ? OFFSET ?`)
		qb.args = append(qb.args, limit, offset)

		rows, err := db.QueryContext(ctx, qb.sql(), qb.args...)
		if err != nil {
			return mcp.NewToolResultError(fmtDBError(err)), nil
		}
		defer rows.Close()

		var results []mailSummary
		for rows.Next() {
			var m mailSummary
			var rawDateStr sql.NullString
			var rawIsRead, rawIsReplied sql.NullInt64
			var attJSON string
			if err := rows.Scan(&m.ID, &m.AccountID, &m.Folder, &m.Subject, &m.Sender, &m.RecipientsTo, &rawDateStr, &rawIsRead, &rawIsReplied, &attJSON); err != nil {
				slog.Warn("get_recent_activity: row scan failed", "err", err)
				continue
			}
			if rawDateStr.Valid && rawDateStr.String != "" {
				m.DateUTC = &rawDateStr.String
			}
			if rawIsRead.Valid {
				v := rawIsRead.Int64 != 0
				m.IsRead = &v
			}
			if rawIsReplied.Valid {
				v := rawIsReplied.Int64 != 0
				m.IsReplied = &v
			}
			if err := json.Unmarshal([]byte(attJSON), &m.Attachments); err != nil {
				m.Attachments = []attachmentDetail{}
			}
			results = append(results, m)
		}
		if err := rows.Err(); err != nil {
			slog.Warn("get_recent_activity: row iteration error", "err", err)
		}

		var total int
		countQB := qb.toCountQuery()
		if err := db.QueryRowContext(ctx, countQB.sql(), countQB.args...).Scan(&total); err != nil {
			slog.Warn("get_recent_activity: count query failed", "err", err)
		}
		if results == nil {
			results = []mailSummary{}
		}
		out, _ := json.MarshalIndent(pagedResult[mailSummary]{
			TotalCount: total,
			Offset:     offset,
			Limit:      limit,
			Results:    results,
		}, "", "  ")
		return mcp.NewToolResultText(string(out)), nil
	}
}

// ---------------------------------------------------------------------------
// Tool: get_email_content
// ---------------------------------------------------------------------------

func toolGetEmailContent() mcp.Tool {
	return mcp.NewTool("get_email_content",
		mcp.WithDescription("Returns the full content of a single email by its entry_id, including body text and attachment metadata."),
		mcp.WithTitleAnnotation("Get Email Content"),
		mcp.WithReadOnlyHintAnnotation(true),
		mcp.WithDestructiveHintAnnotation(false),
		mcp.WithIdempotentHintAnnotation(true),
		mcp.WithOpenWorldHintAnnotation(false),
		mcp.WithString("entry_id",
			mcp.Required(),
			mcp.Description("The unique mail entry ID (format: account_id:folder:uid)."),
		),
	)
}

type attachmentDetail struct {
	Filename    string `json:"filename"`
	ContentType string `json:"content_type"`
	SizeBytes   int64  `json:"size_bytes"`
}

type mailDetail struct {
	mailSummary
	RecipientsCC string `json:"recipients_cc"`
	BodyText     string `json:"body_text"`
}

func handleGetEmailContent(db *sql.DB) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		ctx, cancel := queryCtx(ctx)
		defer cancel()
		entryID, err := req.RequireString("entry_id")
		if err != nil {
			return mcp.NewToolResultError(err.Error()), nil
		}
		slog.Info("tool called", "tool", "get_email_content", "entry_id", entryID)

		var m mailDetail
		var rawDateStr sql.NullString
		var rawIsRead, rawIsReplied sql.NullInt64
		err = db.QueryRowContext(ctx, `
			SELECT e.id, e.account_id, e.imap_folder, e.subject, e.sender,
			       e.recipients_to, e.recipients_cc, e.date_utc, e.is_read, e.is_replied,
			       COALESCE((SELECT substr(body_text, 1, ?) FROM mail_content WHERE entry_id = e.id), '')
			FROM mail_entries e
			WHERE e.id = ?`, maxEmailBodyRunes+1, entryID,
		).Scan(
			&m.ID, &m.AccountID, &m.Folder, &m.Subject, &m.Sender,
			&m.RecipientsTo, &m.RecipientsCC, &rawDateStr, &rawIsRead, &rawIsReplied,
			&m.BodyText,
		)
		if err == sql.ErrNoRows {
			return mcp.NewToolResultError(fmt.Sprintf("entry not found: %q — use list_accounts_and_folders and get_recent_activity to find valid IDs", entryID)), nil
		}
		if err != nil {
			return mcp.NewToolResultError(fmtDBError(err)), nil
		}
		if rawDateStr.Valid && rawDateStr.String != "" {
			m.DateUTC = &rawDateStr.String
		}
		if rawIsRead.Valid {
			v := rawIsRead.Int64 != 0
			m.IsRead = &v
		}
		if rawIsReplied.Valid {
			v := rawIsReplied.Int64 != 0
			m.IsReplied = &v
		}

		// Truncate to avoid flooding the agent context window.  The SQL above
		// already fetched only limit+1 runes, so this does not hold the whole
		// body in memory first.
		m.BodyText, _ = truncateRunes(m.BodyText, maxEmailBodyRunes)

		attRows, err := db.QueryContext(ctx,
			`SELECT COALESCE(filename,''), COALESCE(content_type,''), COALESCE(size_bytes,0) FROM mail_attachments WHERE entry_id = ?`, entryID)
		if err == nil {
			defer attRows.Close()
			for attRows.Next() {
				var a attachmentDetail
				if err := attRows.Scan(&a.Filename, &a.ContentType, &a.SizeBytes); err != nil {
					continue
				}
				m.Attachments = append(m.Attachments, a)
			}
			if err := attRows.Err(); err != nil {
				slog.Warn("get_email_content: attachment row iteration error", "err", err)
			}
		}
		if m.Attachments == nil {
			m.Attachments = []attachmentDetail{}
		}

		out, _ := json.MarshalIndent(m, "", "  ")
		return mcp.NewToolResultText(string(out)), nil
	}
}

// ---------------------------------------------------------------------------
// Tool: search_emails
// ---------------------------------------------------------------------------

func toolSearchEmails() mcp.Tool {
	return mcp.NewTool("search_emails",
		mcp.WithDescription("Full-text search across email subjects and bodies, with optional metadata filters. The response includes total_count so you know how many results match in total for pagination. For Chinese/Japanese/Korean queries that the FTS tokenizer cannot segment, a bounded substring scan of the most recent messages is used; scan_limited=true means older messages may not have been examined."),
		mcp.WithTitleAnnotation("Search Emails"),
		mcp.WithReadOnlyHintAnnotation(true),
		mcp.WithDestructiveHintAnnotation(false),
		mcp.WithIdempotentHintAnnotation(true),
		mcp.WithOpenWorldHintAnnotation(false),
		mcp.WithString("query",
			mcp.Description("Full-text search terms. Searches across both the email subject AND the full message body (via FTS5). Space-separated terms are ANDed and matched literally. Leave empty to use metadata filters only."),
		),
		mcp.WithString("account",
			mcp.Description("Filter by account ID."),
		),
		mcp.WithString("subject",
			mcp.Description("Filter by subject (SQL LIKE, case-insensitive)."),
		),
		mcp.WithString("sender",
			mcp.Description("Filter by sender address (SQL LIKE, case-insensitive)."),
		),
		mcp.WithString("recipient",
			mcp.Description("Filter by recipient in To or CC (SQL LIKE, case-insensitive)."),
		),
		mcp.WithString("date_from",
			mcp.Description("Include only messages on or after this date (RFC3339 or YYYY-MM-DD)."),
		),
		mcp.WithString("date_to",
			mcp.Description("Include only messages on or before this date (RFC3339 or YYYY-MM-DD)."),
		),
		mcp.WithString("folder",
			mcp.Description("Filter by IMAP folder name."),
		),
		mcp.WithNumber("limit",
			mcp.Description("Optional: maximum number of results. Defaults to 10. Max 100. Always set this explicitly when the user asks for a specific number."),
			mcp.DefaultNumber(10),
			mcp.Min(1),
			mcp.Max(100),
		),
		mcp.WithNumber("offset",
			mcp.Description("Optional: number of results to skip for pagination. Use with limit to page through results. Defaults to 0."),
			mcp.DefaultNumber(0),
			mcp.Min(0),
		),
		mcp.WithBoolean("include_body",
			mcp.Description("If true, include the plain-text body of each email in the results."),
		),
		mcp.WithString("has_attachments",
			mcp.Description(`Optional: filter by attachment presence. "true" = only emails with attachments, "false" = only without. Omit to include all.`),
		),
		mcp.WithString("is_read",
			mcp.Description(`Optional: filter by read status. "true" = only read emails, "false" = only unread. Omit to include all.`),
		),
		mcp.WithString("sent_by",
			mcp.Description("Filter by sending account ID (exact match). Use this instead of 'sender' when searching the Sent folder to find mail sent from a specific account."),
		),
	)
}

// searchParams holds the parsed parameters for a search_emails request.
type searchParams struct {
	query, account, subject, sender, recipient string
	dateFrom, dateTo, folder, hasAttachments   string
	isRead, sentBy                             string
	limit, offset                              int
	includeBody                                bool
}

func parseSearchParams(req mcp.CallToolRequest) searchParams {
	p := searchParams{
		query:          req.GetString("query", ""),
		account:        req.GetString("account", ""),
		subject:        req.GetString("subject", ""),
		sender:         req.GetString("sender", ""),
		recipient:      req.GetString("recipient", ""),
		dateFrom:       req.GetString("date_from", ""),
		dateTo:         req.GetString("date_to", ""),
		folder:         req.GetString("folder", ""),
		limit:          int(req.GetFloat("limit", 10)),
		offset:         int(req.GetFloat("offset", 0)),
		includeBody:    req.GetBool("include_body", false),
		hasAttachments: req.GetString("has_attachments", ""),
		isRead:         req.GetString("is_read", ""),
		sentBy:         req.GetString("sent_by", ""),
	}
	if p.limit <= 0 || p.limit > 100 {
		p.limit = 10
	}
	if p.offset < 0 {
		p.offset = 0
	}
	return p
}

// buildFTSQuery turns a user query into a safe FTS5 expression.  Each
// whitespace-separated term is quoted (so ":", "-", "*", "(" etc. are literal
// text instead of FTS5 syntax) and the terms are joined by spaces, which FTS5
// treats as an implicit AND.  Quoting the whole string instead would turn
// "invoice april" into an exact phrase and silently miss most matches.
func buildFTSQuery(query string) string {
	fields := strings.Fields(query)
	quoted := make([]string, 0, len(fields))
	for _, field := range fields {
		field = strings.Trim(field, `"`)
		if field == "" {
			continue
		}
		quoted = append(quoted, `"`+strings.ReplaceAll(field, `"`, `""`)+`"`)
	}
	return strings.Join(quoted, " ")
}

func buildSearchQuery(p searchParams, cfg *config.Config) *queryBuilder {
	bodyExpr := `''`
	if p.includeBody {
		// Fetch one extra rune so truncation is detectable without loading the
		// entire body.
		bodyExpr = fmt.Sprintf(
			`COALESCE((SELECT substr(body_text, 1, %d) FROM mail_content WHERE entry_id = e.id), '')`,
			maxSearchBodyRunes+1,
		)
	}
	selectClause := `SELECT e.id, e.account_id, e.imap_folder, e.subject, e.sender, e.recipients_to, e.date_utc, e.is_read, e.is_replied, ` + fetchAttachmentsSubquery + `, ` + bodyExpr

	qb := &queryBuilder{}
	if p.query != "" {
		if maildb.HasCJK(p.query) {
			// The auxiliary CJK index stores one token per character, so a
			// two-character term like "通知" matches without a full LIKE scan.
			qb.write(selectClause + `
		FROM mail_entries e
		WHERE e.id IN (SELECT entry_id FROM mail_cjk_fts WHERE mail_cjk_fts MATCH ?)`)
			qb.args = append(qb.args, maildb.CJKFTSQuery(p.query))
		} else {
			qb.write(selectClause + `
		FROM mail_entries e
		WHERE e.id IN (SELECT entry_id FROM mail_content_fts WHERE mail_content_fts MATCH ?)`)
			qb.args = append(qb.args, buildFTSQuery(p.query))
		}
	} else {
		qb.write(selectClause + ` FROM mail_entries e WHERE 1=1`)
	}
	appendSearchFilters(qb, p, cfg, "e")

	qb.write(` ORDER BY e.date_utc DESC NULLS LAST LIMIT ? OFFSET ?`)
	qb.args = append(qb.args, p.limit, p.offset)
	return qb
}

// buildSubstringSearchQuery matches p.query as a literal substring of the
// subject or body.  FTS5 cannot segment a run of CJK characters, so this is
// the fallback for Chinese/Japanese/Korean queries.
//
// The LIKE expression cannot use an index.  To keep the fallback bounded, the
// outer query only considers the most recent substringScanLimit messages that
// match the other filters.  scan_limited in the response tells the caller that
// older messages were not examined.
func buildSubstringSearchQuery(p searchParams, cfg *config.Config) *queryBuilder {
	bodyExpr := `''`
	if p.includeBody {
		bodyExpr = fmt.Sprintf(
			`COALESCE((SELECT substr(body_text, 1, %d) FROM mail_content WHERE entry_id = e.id), '')`,
			maxSearchBodyRunes+1,
		)
	}
	selectClause := `SELECT e.id, e.account_id, e.imap_folder, e.subject, e.sender, e.recipients_to, e.date_utc, e.is_read, e.is_replied, ` + fetchAttachmentsSubquery + `, ` + bodyExpr

	qb := &queryBuilder{}
	qb.write(selectClause + `
		FROM mail_entries e
		WHERE e.id IN (
			SELECT e2.id FROM mail_entries e2 WHERE 1=1`)
	appendSearchFilters(qb, p, cfg, "e2")
	qb.write(` ORDER BY e2.date_utc DESC NULLS LAST LIMIT ?`)
	qb.args = append(qb.args, substringScanLimit)
	qb.write(`) AND ` + maildb.SubstringMatchSQL)
	qb.args = append(qb.args, maildb.SubstringMatchArgs(p.query)...)
	qb.write(` ORDER BY e.date_utc DESC NULLS LAST LIMIT ? OFFSET ?`)
	qb.args = append(qb.args, p.limit, p.offset)
	return qb
}

// appendSearchFilters adds the shared metadata filters for the given table
// alias.  The CJK fallback applies them inside its bounded candidate subquery,
// while a normal FTS search applies them directly to the outer query.
func appendSearchFilters(qb *queryBuilder, p searchParams, cfg *config.Config, alias string) {
	col := func(name string) string { return alias + "." + name }
	if p.account != "" {
		qb.and(col("account_id")+" = ?", p.account)
	}
	if p.subject != "" {
		qb.and(col("subject")+" LIKE ?", "%"+p.subject+"%")
	}
	if p.sender != "" {
		qb.and(col("sender")+" LIKE ?", "%"+p.sender+"%")
	}
	if p.recipient != "" {
		qb.and("("+col("recipients_to")+" LIKE ? OR "+col("recipients_cc")+" LIKE ?)", "%"+p.recipient+"%", "%"+p.recipient+"%")
	}
	if p.dateFrom != "" {
		qb.and(col("date_utc")+" >= ?", p.dateFrom)
	}
	if p.dateTo != "" {
		// Normalize plain YYYY-MM-DD to end-of-day so same-day emails are included.
		qb.and(col("date_utc")+" <= ?", normalizeDateTo(p.dateTo))
	}
	if p.folder != "" {
		qb.and(col("imap_folder")+" = ?", p.folder)
	}
	if p.sentBy != "" {
		qb.and(col("account_id")+" = ?", p.sentBy)
	}
	applyAttachmentFilter(qb, p.hasAttachments, alias)
	if p.isRead == "true" {
		qb.and(col("is_read") + " = 1")
	} else if p.isRead == "false" {
		qb.and(col("is_read") + " = 0")
	}
	applyTrashExclusions(qb, cfg, alias)
}

func applyAttachmentFilter(qb *queryBuilder, hasAttachments, alias string) {
	id := alias + ".id"
	switch hasAttachments {
	case "true":
		qb.and("EXISTS (SELECT 1 FROM mail_attachments WHERE entry_id = " + id + ")")
	case "false":
		qb.and("NOT EXISTS (SELECT 1 FROM mail_attachments WHERE entry_id = " + id + ")")
	}
}

// applyTrashExclusions adds a NOT (account_id = ? AND imap_folder = ?) condition
// for every account that has a trash_folder configured.
// This ensures soft-deleted emails never appear in query results, regardless of
// whether the trash folder is included in the sync configuration.
func applyTrashExclusions(qb *queryBuilder, cfg *config.Config, alias string) {
	for _, acc := range cfg.Accounts {
		if acc.TrashFolder != "" {
			qb.and("NOT ("+alias+".account_id = ? AND "+alias+".imap_folder = ?)", acc.ID, acc.TrashFolder)
		}
	}
}

func handleSearchEmails(db *sql.DB, cfg *config.Config) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		ctx, cancel := queryCtx(ctx)
		defer cancel()
		p := parseSearchParams(req)
		slog.Info("tool called", "tool", "search_emails", "query", p.query, "account", p.account, "sender", p.sender, "date_from", p.dateFrom, "date_to", p.dateTo, "limit", p.limit, "offset", p.offset)
		if msg := validateHasAttachments(p.hasAttachments); msg != "" {
			return mcp.NewToolResultError(msg), nil
		}
		if msg := validateIsRead(p.isRead); msg != "" {
			return mcp.NewToolResultError(msg), nil
		}
		if msg := validateDate("date_from", p.dateFrom); msg != "" {
			return mcp.NewToolResultError(msg), nil
		}
		if msg := validateDate("date_to", p.dateTo); msg != "" {
			return mcp.NewToolResultError(msg), nil
		}
		cjkQuery := p.query != "" && maildb.HasCJK(p.query)
		// While the auxiliary CJK index is still being backfilled it can match
		// *some* rows and miss others.  Returning that partial set as a normal
		// result would silently hide older mail, so use the bounded substring
		// scan (which reports scan_limited) until every folder is backfilled.
		usedFallback := cjkQuery && maildb.CJKIndexPending(db)
		var qb *queryBuilder
		if usedFallback {
			qb = buildSubstringSearchQuery(p, cfg)
		} else {
			qb = buildSearchQuery(p, cfg)
		}
		results, total, err := runSearchQuery(ctx, db, qb)
		if err != nil {
			return mcp.NewToolResultError(fmtDBError(err)), nil
		}

		// A CJK row inserted after the backfill pass (or a query the index
		// tokenised differently) can still miss.  Fall back when it finds
		// nothing, even though the index is otherwise complete.
		if !usedFallback && cjkQuery && total == 0 {
			fallback := buildSubstringSearchQuery(p, cfg)
			results, total, err = runSearchQuery(ctx, db, fallback)
			if err != nil {
				return mcp.NewToolResultError(fmtDBError(err)), nil
			}
			usedFallback = true
			slog.Info("search_emails: no FTS5 match, used bounded substring fallback",
				"query", p.query, "total", total, "scan_limit", substringScanLimit)
		}

		page := pagedResult[searchResult]{
			TotalCount: total,
			Offset:     p.offset,
			Limit:      p.limit,
			Results:    results,
		}
		if usedFallback {
			page.ScanLimited = true
			page.ScanLimit = substringScanLimit
		}
		out, _ := json.MarshalIndent(page, "", "  ")
		return mcp.NewToolResultText(string(out)), nil
	}
}

// searchResult is one hit returned by the search_emails tool.
type searchResult struct {
	mailSummary
	BodyText      string `json:"body_text,omitempty"`
	BodyTruncated bool   `json:"body_truncated,omitempty"`
}

// runSearchQuery executes qb and returns the requested page of results together
// with the total number of matching rows (ignoring limit and offset).
func runSearchQuery(ctx context.Context, db *sql.DB, qb *queryBuilder) ([]searchResult, int, error) {
	rows, err := db.QueryContext(ctx, qb.sql(), qb.args...)
	if err != nil {
		return nil, 0, err
	}
	defer rows.Close()

	var results []searchResult
	for rows.Next() {
		var m searchResult
		var rawDateStr sql.NullString
		var rawIsRead, rawIsReplied sql.NullInt64
		var attJSON string
		if err := rows.Scan(&m.ID, &m.AccountID, &m.Folder, &m.Subject, &m.Sender, &m.RecipientsTo, &rawDateStr, &rawIsRead, &rawIsReplied, &attJSON, &m.BodyText); err != nil {
			slog.Warn("search_emails: row scan failed", "err", err)
			continue
		}
		if rawDateStr.Valid && rawDateStr.String != "" {
			m.DateUTC = &rawDateStr.String
		}
		if rawIsRead.Valid {
			v := rawIsRead.Int64 != 0
			m.IsRead = &v
		}
		if rawIsReplied.Valid {
			v := rawIsReplied.Int64 != 0
			m.IsReplied = &v
		}
		if err := json.Unmarshal([]byte(attJSON), &m.Attachments); err != nil {
			m.Attachments = []attachmentDetail{}
		}
		m.BodyText, m.BodyTruncated = truncateRunes(m.BodyText, maxSearchBodyRunes)
		results = append(results, m)
	}
	if err := rows.Err(); err != nil {
		slog.Warn("search_emails: row iteration error", "err", err)
	}

	var total int
	countQB := qb.toCountQuery()
	if err := db.QueryRowContext(ctx, countQB.sql(), countQB.args...).Scan(&total); err != nil {
		slog.Warn("search_emails: count query failed", "err", err)
	}
	if results == nil {
		results = []searchResult{}
	}
	return results, total, nil
}

// ---------------------------------------------------------------------------
// Tool: download_attachments
// ---------------------------------------------------------------------------

func toolDownloadAttachments() mcp.Tool {
	return mcp.NewTool("download_attachments",
		mcp.WithDescription("Downloads all attachments for a specific email directly from the IMAP server and saves them to disk. Returns the paths of the saved files."),
		mcp.WithTitleAnnotation("Download Attachments"),
		mcp.WithReadOnlyHintAnnotation(false),
		mcp.WithDestructiveHintAnnotation(false),
		mcp.WithIdempotentHintAnnotation(false),
		mcp.WithOpenWorldHintAnnotation(true),
		mcp.WithString("email_id",
			mcp.Required(),
			mcp.Description("The email ID in the format 'account:folder:uid' as returned by search_emails or get_recent_activity."),
		),
		mcp.WithString("output_dir",
			mcp.Description("Override the default attachment directory from config. If omitted, uses attachment_dir from config.yaml."),
		),
	)
}

func handleDownloadAttachments(cfg *config.Config) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		emailID, err := req.RequireString("email_id")
		if err != nil {
			return mcp.NewToolResultError("email_id is required"), nil
		}
		slog.Info("tool called", "tool", "download_attachments", "email_id", emailID)

		// Parse email_id: "account:folder:uid"
		// Note: account and folder may themselves contain colons, so split from right.
		lastColon := strings.LastIndex(emailID, ":")
		if lastColon < 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid email_id format (expected account:folder:uid): %q", emailID)), nil
		}
		uidStr := emailID[lastColon+1:]
		remainder := emailID[:lastColon]
		secondColon := strings.Index(remainder, ":")
		if secondColon < 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid email_id format (expected account:folder:uid): %q", emailID)), nil
		}
		accountID := remainder[:secondColon]
		folder := remainder[secondColon+1:]

		var uid uint32
		if _, err := fmt.Sscanf(uidStr, "%d", &uid); err != nil || uid == 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid uid in email_id: %q", uidStr)), nil
		}

		outDir := req.GetString("output_dir", cfg.AttachmentDir)

		files, err := attachment.DownloadForEmail(cfg, accountID, folder, uid, outDir)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("download failed: %v", err)), nil
		}
		if len(files) == 0 {
			return mcp.NewToolResultText("no attachments found for this email"), nil
		}

		out, _ := json.MarshalIndent(files, "", "  ")
		return mcp.NewToolResultText(string(out)), nil
	}
}

// ---------------------------------------------------------------------------
// Tool: get_download_link
// ---------------------------------------------------------------------------

func toolGetDownloadLink() mcp.Tool {
	return mcp.NewTool("get_download_link",
		mcp.WithDescription(
			"FALLBACK ONLY: Generates a temporary HTTP download URL for the attachments of an email. "+
				"IMPORTANT: Only use this tool if (a) the user explicitly asked for a download link, OR "+
				"(b) you already tried to send the file via your normal communication tools (e.g. WhatsApp, "+
				"email, file transfer) and it failed. Direct delivery via communication tools always takes "+
				"priority. The link is single-use and expires after a configurable TTL (default 15 minutes). "+
				"The fileserver must be enabled in config.yaml (fileserver_port) for this tool to be available.",
		),
		mcp.WithString("email_id",
			mcp.Required(),
			mcp.Description("The email ID in the format 'account:folder:uid' as returned by search_emails or get_recent_activity."),
		),
		mcp.WithString("output_dir",
			mcp.Description("Override the default attachment directory. If omitted, uses attachment_dir from config.yaml."),
		),
	)
}

func handleGetDownloadLink(cfg *config.Config, fs *fileserver.Server) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		emailID, err := req.RequireString("email_id")
		if err != nil {
			return mcp.NewToolResultError("email_id is required"), nil
		}
		slog.Info("tool called", "tool", "get_download_link", "email_id", emailID)

		lastColon := strings.LastIndex(emailID, ":")
		if lastColon < 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid email_id format (expected account:folder:uid): %q", emailID)), nil
		}
		uidStr := emailID[lastColon+1:]
		remainder := emailID[:lastColon]
		secondColon := strings.Index(remainder, ":")
		if secondColon < 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid email_id format (expected account:folder:uid): %q", emailID)), nil
		}
		accountID := remainder[:secondColon]
		folder := remainder[secondColon+1:]

		var uid uint32
		if _, err := fmt.Sscanf(uidStr, "%d", &uid); err != nil || uid == 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid uid in email_id: %q", uidStr)), nil
		}

		outDir := req.GetString("output_dir", cfg.AttachmentDir)

		files, err := attachment.DownloadForEmail(cfg, accountID, folder, uid, outDir)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("download failed: %v", err)), nil
		}
		if len(files) == 0 {
			return mcp.NewToolResultText("no attachments found for this email"), nil
		}

		type linkResult struct {
			File string `json:"file"`
			URL  string `json:"url"`
		}
		var results []linkResult
		for _, f := range files {
			url, err := fs.CreateLink(f.Path)
			if err != nil {
				return mcp.NewToolResultError(fmt.Sprintf("could not create download link for %q: %v", f.Path, err)), nil
			}
			results = append(results, linkResult{File: f.Path, URL: url})
		}

		out, _ := json.MarshalIndent(results, "", "  ")
		return mcp.NewToolResultText(string(out)), nil
	}
}

// ---------------------------------------------------------------------------
// Tool: get_thread
// ---------------------------------------------------------------------------

func toolGetThread() mcp.Tool {
	return mcp.NewTool("get_thread",
		mcp.WithDescription("Returns all emails belonging to the same thread as the given email, sorted by date ascending. Uses message-id and in-reply-to headers to find related messages. Returns at most the first 500 messages of a very large thread (oldest first); later messages are omitted. Note: emails synced before this feature was added may have NULL thread headers and will not appear in thread results until the background envelope backfill completes."),
		mcp.WithTitleAnnotation("Get Email Thread"),
		mcp.WithReadOnlyHintAnnotation(true),
		mcp.WithDestructiveHintAnnotation(false),
		mcp.WithIdempotentHintAnnotation(true),
		mcp.WithOpenWorldHintAnnotation(false),
		mcp.WithString("email_id",
			mcp.Required(),
			mcp.Description("The email ID in the format 'account:folder:uid' as returned by search_emails or get_recent_activity."),
		),
	)
}

func handleGetThread(db *sql.DB, cfg *config.Config) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		ctx, cancel := queryCtx(ctx)
		defer cancel()
		emailID, err := req.RequireString("email_id")
		if err != nil {
			return mcp.NewToolResultError("email_id is required"), nil
		}
		slog.Info("tool called", "tool", "get_thread", "email_id", emailID)

		// Recursive CTE: start from the given email and walk the thread
		// both forward (replies to this mail) and backward (what this mail replies to).
		const queryCTE = `
			WITH RECURSIVE thread(id, message_id, in_reply_to) AS (
				SELECT id, message_id, in_reply_to
				FROM mail_entries WHERE id = ?
				UNION
				SELECT e.id, e.message_id, e.in_reply_to
				FROM mail_entries e
				JOIN thread t ON
					(e.in_reply_to IS NOT NULL AND e.in_reply_to = t.message_id)
					OR (e.message_id IS NOT NULL AND e.message_id = t.in_reply_to)
			)
			SELECT DISTINCT e.id, e.account_id, e.imap_folder, e.subject, e.sender,
				e.recipients_to, e.date_utc, e.is_read, e.is_replied, ` +
			fetchAttachmentsSubquery + `
			FROM mail_entries e
			JOIN thread t ON e.id = t.id`

		// Build trash exclusion conditions (applied as WHERE clause on the outer SELECT).
		var trashConds []string
		threadArgs := []any{emailID}
		for _, acc := range cfg.Accounts {
			if acc.TrashFolder != "" {
				trashConds = append(trashConds, "NOT (e.account_id = ? AND e.imap_folder = ?)")
				threadArgs = append(threadArgs, acc.ID, acc.TrashFolder)
			}
		}
		query := queryCTE
		if len(trashConds) > 0 {
			query += " WHERE " + strings.Join(trashConds, " AND ")
		}
		query += " ORDER BY e.date_utc ASC NULLS LAST LIMIT ?"
		threadArgs = append(threadArgs, maxThreadMessages+1)

		rows, err := db.QueryContext(ctx, query, threadArgs...)
		if err != nil {
			return mcp.NewToolResultError(fmtDBError(err)), nil
		}
		defer rows.Close()

		var results []mailSummary
		for rows.Next() {
			var m mailSummary
			var rawDateStr sql.NullString
			var rawIsRead, rawIsReplied sql.NullInt64
			var attJSON string
			if err := rows.Scan(&m.ID, &m.AccountID, &m.Folder, &m.Subject, &m.Sender, &m.RecipientsTo, &rawDateStr, &rawIsRead, &rawIsReplied, &attJSON); err != nil {
				slog.Warn("get_thread: row scan failed", "err", err)
				continue
			}
			if rawDateStr.Valid && rawDateStr.String != "" {
				m.DateUTC = &rawDateStr.String
			}
			if rawIsRead.Valid {
				v := rawIsRead.Int64 != 0
				m.IsRead = &v
			}
			if rawIsReplied.Valid {
				v := rawIsReplied.Int64 != 0
				m.IsReplied = &v
			}
			if err := json.Unmarshal([]byte(attJSON), &m.Attachments); err != nil {
				m.Attachments = []attachmentDetail{}
			}
			results = append(results, m)
		}
		if err := rows.Err(); err != nil {
			slog.Warn("get_thread: row iteration error", "err", err)
		}
		if len(results) > maxThreadMessages {
			slog.Warn("get_thread: thread truncated", "email_id", emailID, "limit", maxThreadMessages)
			results = results[:maxThreadMessages]
		}
		if results == nil {
			results = []mailSummary{}
		}
		out, _ := json.MarshalIndent(results, "", "  ")
		return mcp.NewToolResultText(string(out)), nil
	}
}

// ---------------------------------------------------------------------------
// Tool: delete_mail
// ---------------------------------------------------------------------------

func toolDeleteMail() mcp.Tool {
	return mcp.NewTool("delete_mail",
		mcp.WithDescription(
			"Soft-deletes an email by moving it to the configured trash folder on the IMAP server (IMAP MOVE). "+
				"The email is NOT permanently deleted — it is moved to the trash_folder defined in config.yaml for this account. "+
				"The local database entry is removed immediately; the moved email will be picked up by the next sync of the trash folder. "+
				"Returns an error if trash_folder is not configured for the account.",
		),
		mcp.WithTitleAnnotation("Delete Mail (Move to Trash)"),
		mcp.WithReadOnlyHintAnnotation(false),
		mcp.WithDestructiveHintAnnotation(true),
		mcp.WithIdempotentHintAnnotation(false),
		mcp.WithOpenWorldHintAnnotation(true),
		mcp.WithString("email_id",
			mcp.Required(),
			mcp.Description("The email ID in the format 'account:folder:uid' as returned by search_emails or get_recent_activity."),
		),
	)
}

func handleDeleteMail(cfg *config.Config, db *sql.DB) server.ToolHandlerFunc {
	return func(ctx context.Context, req mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		emailID, err := req.RequireString("email_id")
		if err != nil {
			return mcp.NewToolResultError("email_id is required"), nil
		}
		slog.Info("tool called", "tool", "delete_mail", "email_id", emailID)

		// Parse email_id: "account:folder:uid"
		lastColon := strings.LastIndex(emailID, ":")
		if lastColon < 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid email_id format (expected account:folder:uid): %q", emailID)), nil
		}
		uidStr := emailID[lastColon+1:]
		remainder := emailID[:lastColon]
		secondColon := strings.Index(remainder, ":")
		if secondColon < 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid email_id format (expected account:folder:uid): %q", emailID)), nil
		}
		accountID := remainder[:secondColon]
		folder := remainder[secondColon+1:]

		var uid uint32
		if _, err := fmt.Sscanf(uidStr, "%d", &uid); err != nil || uid == 0 {
			return mcp.NewToolResultError(fmt.Sprintf("invalid uid in email_id: %q", uidStr)), nil
		}

		// Find account config.
		var acc *config.AccountConfig
		for i := range cfg.Accounts {
			if cfg.Accounts[i].ID == accountID {
				acc = &cfg.Accounts[i]
				break
			}
		}
		if acc == nil {
			return mcp.NewToolResultError(fmt.Sprintf("account %q not found in config", accountID)), nil
		}

		// Require trash_folder to be configured.
		if acc.TrashFolder == "" {
			return mcp.NewToolResultError(fmt.Sprintf(
				"delete_mail is not configured for account %q — set trash_folder in config.yaml", accountID,
			)), nil
		}

		// Verify the email exists in the local DB before attempting IMAP operations.
		var exists int
		if err := db.QueryRowContext(ctx, `SELECT COUNT(*) FROM mail_entries WHERE id = ?`, emailID).Scan(&exists); err != nil || exists == 0 {
			return mcp.NewToolResultError(fmt.Sprintf(
				"email %q not found — use get_recent_activity or search_emails to get a valid email ID", emailID,
			)), nil
		}

		// Open a short-lived IMAP connection and move the message.
		syncClient, err := imapsync.NewClient(*acc)
		if err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("could not connect to IMAP server: %v", err)), nil
		}
		defer syncClient.Close()
		c := syncClient.RawClient()

		if _, err := c.Select(folder, nil).Wait(); err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("could not select folder %q: %v", folder, err)), nil
		}

		var uidSet imap.UIDSet
		uidSet.AddNum(imap.UID(uid))
		if _, err := c.Move(uidSet, acc.TrashFolder).Wait(); err != nil {
			return mcp.NewToolResultError(fmt.Sprintf("IMAP MOVE failed: %v", err)), nil
		}

		// Clean up the local DB entry. Order matters: FTS and content first,
		// then mail_entries (which cascades to mail_attachments).
		if _, err := db.ExecContext(ctx, `DELETE FROM mail_content_fts WHERE entry_id = ?`, emailID); err != nil {
			slog.Warn("delete_mail: failed to remove FTS entry", "email_id", emailID, "err", err)
		}
		if _, err := db.ExecContext(ctx, `DELETE FROM mail_cjk_fts WHERE entry_id = ?`, emailID); err != nil {
			slog.Warn("delete_mail: failed to remove CJK index entry", "email_id", emailID, "err", err)
		}
		if _, err := db.ExecContext(ctx, `DELETE FROM mail_content WHERE entry_id = ?`, emailID); err != nil {
			slog.Warn("delete_mail: failed to remove content entry", "email_id", emailID, "err", err)
		}
		if _, err := db.ExecContext(ctx, `DELETE FROM mail_entries WHERE id = ?`, emailID); err != nil {
			slog.Warn("delete_mail: failed to remove mail entry", "email_id", emailID, "err", err)
		}

		slog.Info("delete_mail: moved to trash", "email_id", emailID, "trash_folder", acc.TrashFolder)
		return mcp.NewToolResultText(fmt.Sprintf(
			"Mail %s moved to %q (soft-delete). The email remains on the server in the trash folder.", emailID, acc.TrashFolder,
		)), nil
	}
}
