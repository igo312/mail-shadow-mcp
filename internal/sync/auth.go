// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// auth.go:
// Capability-aware session authentication shared by the sync engine and the
// IMAP IDLE watcher.

package imapsync

import (
	"fmt"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"
	"github.com/emersion/go-sasl"
)

// authenticate logs the connection in with the account credentials.
//
// SASL PLAIN is used only when the server advertises the AUTH=PLAIN mechanism.
// A provider can support SASL-IR yet still reject the PLAIN mechanism: Alibaba /
// Aliyun enterprise mail (imap.qiye.aliyun.com) advertises SASL-IR, ID, IDLE,
// UIDPLUS, XLIST and AUTH=XOAUTH/XOAUTH2/EXTERNAL, but answers
//
//	AUTHENTICATE PLAIN <base64>
//
// with "BAD invalid command or parameters" while its classic LOGIN command
// works fine. Negotiating on the advertised capabilities keeps both kinds of
// server working.
func authenticate(c *imapclient.Client, username, password string) error {
	if c.Caps().Has(imap.AuthCap("PLAIN")) {
		if err := c.Authenticate(sasl.NewPlainClient("", username, password)); err != nil {
			return fmt.Errorf("AUTHENTICATE PLAIN: %w", err)
		}
		return nil
	}

	if err := c.Login(username, password).Wait(); err != nil {
		return fmt.Errorf("LOGIN: %w", err)
	}
	return nil
}
