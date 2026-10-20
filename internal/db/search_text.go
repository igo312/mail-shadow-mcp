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
	"strings"
	"unicode"
)

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

// HasCJK reports whether s contains a CJK character — Chinese, Japanese kana or
// Korean hangul — i.e. text for which the FTS5 tokenizer produces no useful
// word boundaries.
func HasCJK(s string) bool {
	for _, r := range s {
		if unicode.In(r, unicode.Han, unicode.Hiragana, unicode.Katakana, unicode.Hangul) {
			return true
		}
	}
	return false
}
