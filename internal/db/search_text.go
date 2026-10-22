// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// search_text.go:
// Helpers for text the FTS5 index cannot match well. The default unicode61
// tokenizer splits on non-alphanumeric characters only, so a run of CJK
// characters becomes a single token: "关于2026年度财务结算有关事项的通知" is
// indexed as one term and a query for "通知" finds nothing. Callers retry such
// queries as a literal substring match over subject and body.

package db

import (
	"database/sql"
	"strings"
	"unicode"
	"unicode/utf8"
)

// CJKIndexPending reports whether any account/folder may still have messages
// missing from the auxiliary CJK index.  The sync engine sets
// sync_state.cjk_backfill_done once a folder has no unindexed rows left.
//
// Callers must use the bounded literal substring fallback while this is true:
// querying the auxiliary index during the backfill returns a *silently
// incomplete* result set (the matches that happen to be indexed) rather than an
// error, which is worse than the slower scan.
func CJKIndexPending(db *sql.DB) bool {
	var pending int
	if err := db.QueryRow(
		`SELECT COUNT(*) FROM sync_state WHERE cjk_backfill_done = 0`,
	).Scan(&pending); err != nil {
		// An older schema without the column must not disable search.
		return false
	}
	return pending > 0
}

// SubstringMatchSQL matches a literal substring of the subject or the body of
// the mail entry aliased "e". It takes two parameters; build them with
// SubstringMatchArgs.
const SubstringMatchSQL = `(e.subject LIKE ? ESCAPE '\' OR ` +
	`EXISTS (SELECT 1 FROM mail_content c WHERE c.entry_id = e.id AND c.body_text LIKE ? ESCAPE '\'))`

// SubstringMatchArgs returns the two arguments SubstringMatchSQL expects.
func SubstringMatchArgs(query string) []any {
	pattern := LikePattern(query)
	return []any{pattern, pattern}
}

// LikePattern wraps s in % wildcards so it matches literally anywhere inside a
// column, escaping the characters that are special to LIKE. Use it together
// with ESCAPE '\'.
func LikePattern(s string) string {
	var b strings.Builder
	b.Grow(len(s) + 2)
	b.WriteByte('%')
	for i := 0; i < len(s); i++ {
		// '\\', '%' and '_' are single-byte ASCII, so copying every other byte
		// verbatim keeps multi-byte UTF-8 sequences intact.
		if c := s[i]; c == '\\' || c == '%' || c == '_' {
			b.WriteByte('\\')
		}
		b.WriteByte(s[i])
	}
	b.WriteByte('%')
	return b.String()
}

func isCJK(r rune) bool {
	return unicode.In(r, unicode.Han, unicode.Hiragana, unicode.Katakana, unicode.Hangul)
}

// HasCJK reports whether s contains a CJK character — Chinese, Japanese kana or
// Korean hangul — i.e. text for which the FTS5 tokenizer produces no useful
// word boundaries.
func HasCJK(s string) bool {
	for _, r := range s {
		if isCJK(r) {
			return true
		}
	}
	return false
}

// CJKIndexText prepares text for the auxiliary CJK FTS table.  Every CJK
// character is separated by a space so the unicode61 tokenizer treats each
// character as its own token; non-CJK text is left untouched.  A query for
// "通知" can then match the two adjacent tokens instead of failing to match the
// whole run as one unicode61 term.
func CJKIndexText(s string) string {
	// Bound the auxiliary index for pathological messages (newsletters with
	// megabytes of HTML).  The normal FTS still has the full body.
	const maxCJKIndexBytes = 256 << 10
	if len(s) > maxCJKIndexBytes {
		s = s[:maxCJKIndexBytes]
		for len(s) > 0 && !utf8.ValidString(s) {
			s = s[:len(s)-1]
		}
	}
	var b strings.Builder
	b.Grow(len(s) + len(s)/3)
	prevSpace := true
	for _, r := range s {
		if isCJK(r) {
			// Separate the character from *both* neighbours.  Without the
			// leading space, "2026年" would stay one token and a query for
			// "2026" would not match it.
			if !prevSpace {
				b.WriteByte(' ')
			}
			b.WriteRune(r)
			b.WriteByte(' ')
			prevSpace = true
			continue
		}
		b.WriteRune(r)
		prevSpace = unicode.IsSpace(r)
	}
	return b.String()
}

// CJKFTSQuery converts a user query into a safe FTS5 expression for the CJK
// index.  Each CJK run becomes a phrase of single-character tokens (adjacent
// characters must match in order); non-CJK terms are quoted and ANDed.  For
// example, "关于 2026 财务" becomes `"关 于" "2026" "财 务"`.
func CJKFTSQuery(q string) string {
	var parts []string
	var cjk []rune
	var other []rune

	flushCJK := func() {
		if len(cjk) == 0 {
			return
		}
		var b strings.Builder
		b.WriteByte('"')
		for i, r := range cjk {
			if i > 0 {
				b.WriteByte(' ')
			}
			b.WriteRune(r)
		}
		b.WriteByte('"')
		parts = append(parts, b.String())
		cjk = cjk[:0]
	}
	flushOther := func() {
		for _, field := range strings.Fields(string(other)) {
			field = strings.Trim(field, `"`)
			if field == "" {
				continue
			}
			parts = append(parts, `"`+strings.ReplaceAll(field, `"`, `""`)+`"`)
		}
		other = other[:0]
	}

	for _, r := range q {
		if isCJK(r) {
			flushOther()
			cjk = append(cjk, r)
			continue
		}
		flushCJK()
		other = append(other, r)
	}
	flushCJK()
	flushOther()
	return strings.Join(parts, " ")
}
