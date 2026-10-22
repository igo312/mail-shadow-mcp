// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// search_text_test.go:
// Unit tests for the CJK-aware substring fallback helpers.
package db

import (
	"strings"
	"testing"
)

func TestHasCJK(t *testing.T) {
	tests := []struct {
		in   string
		want bool
	}{
		{"", false},
		{"invoice", false},
		{"Re: Q4 invoice", false},
		{"café", false},
		{"Привет", false},
		{"通知", true},
		{"关于2026年度财务结算有关事项的通知", true},
		{"こんにちは", true},
		{"안녕하세요", true},
		{"mixed english 中文 text", true},
	}
	for _, tt := range tests {
		if got := HasCJK(tt.in); got != tt.want {
			t.Errorf("HasCJK(%q) = %v, want %v", tt.in, got, tt.want)
		}
	}
}

func TestLikePattern(t *testing.T) {
	tests := []struct {
		in   string
		want string
	}{
		{"invoice", "%invoice%"},
		{"通知", "%通知%"},
		{"100%", `%100\%%`},
		{"a_b", `%a\_b%`},
		{`a\b`, `%a\\b%`},
		{`50%_off\`, `%50\%\_off\\%`},
	}
	for _, tt := range tests {
		got := LikePattern(tt.in)
		if got != tt.want {
			t.Errorf("LikePattern(%q) = %q, want %q", tt.in, got, tt.want)
		}
		if !strings.HasPrefix(got, "%") || !strings.HasSuffix(got, "%") {
			t.Errorf("LikePattern(%q) = %q, must be wrapped in %%", tt.in, got)
		}
	}
}

func TestSubstringMatchArgs(t *testing.T) {
	args := SubstringMatchArgs("通知")
	if len(args) != 2 {
		t.Fatalf("expected 2 args, got %d", len(args))
	}
	for i, a := range args {
		s, ok := a.(string)
		if !ok {
			t.Fatalf("arg %d is %T, want string", i, a)
		}
		if s != "%通知%" {
			t.Errorf("arg %d = %q, want %q", i, s, "%通知%")
		}
	}
}

func TestCJKIndexText(t *testing.T) {
	got := CJKIndexText("关于2026年度财务结算的通知")
	want := "关 于 2026 年 度 财 务 结 算 的 通 知 "
	if got != want {
		t.Errorf("CJKIndexText: got %q, want %q", got, want)
	}
	if got := CJKIndexText("plain english"); got != "plain english" {
		t.Errorf("non-CJK text must be unchanged, got %q", got)
	}
}

func TestCJKFTSQuery(t *testing.T) {
	tests := []struct{ in, want string }{
		{"通知", `"通 知"`},
		{"财务结算", `"财 务 结 算"`},
		{"关于 2026 财务", `"关 于" "2026" "财 务"`},
		{"Re: 通知", `"Re:" "通 知"`},
	}
	for _, tt := range tests {
		if got := CJKFTSQuery(tt.in); got != tt.want {
			t.Errorf("CJKFTSQuery(%q) = %q, want %q", tt.in, got, tt.want)
		}
	}
}
