// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// rawparse.go:
// Local RFC 5322 / MIME parsing used by the raw-literal fallback.
//
// The regular sync path lets the IMAP server describe a message (ENVELOPE +
// BODYSTRUCTURE) and the client parse that description. Some servers emit a
// description the IMAP decoder rejects, which kills the whole connection. The
// fallback fetches the message itself (BODY.PEEK[]) and parses it here, using
// only the standard library: no server-provided structure is trusted.

package imapsync

import (
	"bytes"
	"io"
	"mime"
	"mime/multipart"
	"net/mail"
	"net/textproto"
	"strings"
	"time"
)

// maxMIMEDepth bounds recursion into nested multipart bodies.
const maxMIMEDepth = 10

// rawMessage holds everything the sync needs from a message parsed locally.
type rawMessage struct {
	Subject     string
	From        string
	To          string
	Cc          string
	Date        time.Time
	MessageID   string
	InReplyTo   string
	Text        string
	Attachments []attachmentInfo
}

// parseRawMessage parses a complete RFC 5322 message (headers + MIME body).
func parseRawMessage(raw []byte) (rawMessage, error) {
	msg, err := mail.ReadMessage(bytes.NewReader(raw))
	if err != nil {
		return rawMessage{}, err
	}

	dec := new(mime.WordDecoder)
	var out rawMessage
	out.Subject = decodeHeader(dec, msg.Header.Get("Subject"))
	out.From = decodeHeader(dec, msg.Header.Get("From"))
	out.To = decodeHeader(dec, msg.Header.Get("To"))
	out.Cc = decodeHeader(dec, msg.Header.Get("Cc"))
	if date, err := msg.Header.Date(); err == nil {
		out.Date = date
	}
	out.MessageID = normaliseMessageIDs(msg.Header.Get("Message-Id"))
	out.InReplyTo = normaliseMessageIDs(msg.Header.Get("In-Reply-To"))

	var attachments []attachmentInfo
	out.Text = collectText(dec, textproto.MIMEHeader(msg.Header), msg.Body, &attachments, 0)
	out.Attachments = attachments

	return out, nil
}

// collectText walks a MIME part and returns the concatenated text/plain
// content, collecting attachment metadata on the way.
func collectText(dec *mime.WordDecoder, header textproto.MIMEHeader, body io.Reader, attachments *[]attachmentInfo, depth int) string {
	if depth > maxMIMEDepth {
		return ""
	}

	contentType := header.Get("Content-Type")
	mediaType := "text/plain"
	var params map[string]string
	if contentType != "" {
		if mt, p, err := mime.ParseMediaType(contentType); err == nil {
			mediaType, params = mt, p
		} else {
			// A header we cannot parse must not be mistaken for text: treat it
			// as opaque so nothing binary ends up in the full-text index.
			mediaType = "application/octet-stream"
		}
	}

	if strings.HasPrefix(strings.ToLower(mediaType), "multipart/") {
		// A multipart/alternative carries the same content twice (text/plain
		// and text/html).  Return only the first usable alternative instead of
		// concatenating both.
		preferFirst := strings.EqualFold(mediaType, "multipart/alternative")
		boundary := params["boundary"]
		if boundary == "" {
			data, err := io.ReadAll(body)
			if err != nil {
				return ""
			}
			return strings.TrimSpace(decodeBody(data, header.Get("Content-Transfer-Encoding")))
		}

		var parts []string
		reader := multipart.NewReader(body, boundary)
		for {
			part, err := reader.NextPart()
			if err != nil {
				break // io.EOF or a broken boundary — keep what we already have
			}
			text := collectText(dec, part.Header, part, attachments, depth+1)
			if text == "" {
				continue
			}
			if preferFirst {
				return text
			}
			parts = append(parts, text)
		}
		return strings.Join(parts, "\n")
	}

	if isAttachment(header) {
		data, _ := io.ReadAll(body)
		*attachments = append(*attachments, attachmentInfo{
			Filename:    attachmentName(dec, header, params),
			ContentType: mediaType,
			SizeBytes:   int64(len(data)),
		})
		return ""
	}

	if strings.EqualFold(mediaType, "text/plain") || strings.EqualFold(mediaType, "text/html") {
		data, err := io.ReadAll(body)
		if err != nil {
			return ""
		}
		text := strings.TrimSpace(decodeBody(data, header.Get("Content-Transfer-Encoding")))
		if strings.EqualFold(mediaType, "text/html") {
			text = strings.TrimSpace(htmlToText(text))
		}
		return text
	}

	return ""
}

// isAttachment reports whether the part is marked as an attachment.
func isAttachment(header textproto.MIMEHeader) bool {
	disposition := header.Get("Content-Disposition")
	if disposition == "" {
		return false
	}
	value, _, err := mime.ParseMediaType(disposition)
	if err != nil {
		return false
	}
	return strings.EqualFold(value, "attachment")
}

// attachmentName returns the decoded file name of an attachment part.
func attachmentName(dec *mime.WordDecoder, header textproto.MIMEHeader, contentTypeParams map[string]string) string {
	if disposition := header.Get("Content-Disposition"); disposition != "" {
		if _, params, err := mime.ParseMediaType(disposition); err == nil {
			if name := params["filename"]; name != "" {
				return decodeHeader(dec, name)
			}
		}
	}
	if name := contentTypeParams["name"]; name != "" {
		return decodeHeader(dec, name)
	}
	return ""
}

// decodeHeader decodes RFC 2047 encoded words, falling back to the raw value.
func decodeHeader(dec *mime.WordDecoder, s string) string {
	if s == "" {
		return ""
	}
	if decoded, err := dec.DecodeHeader(s); err == nil {
		return decoded
	}
	return s
}

// normaliseMessageIDs strips the angle brackets around message identifiers so
// that locally parsed values match the ones the IMAP envelope decoder produces
// (which are stored without brackets and used for threading lookups).
func normaliseMessageIDs(s string) string {
	fields := strings.Fields(s)
	if len(fields) == 0 {
		return ""
	}
	for i, f := range fields {
		fields[i] = strings.Trim(f, "<>")
	}
	return strings.Join(fields, " ")
}
