// MAIL-SHADOW-MCP — Structured, email access for AI agents.
//
// Copyright (c) 2026 Benjamin Kaiser.
// SPDX-License-Identifier: Apache-2.0
// https://github.com/dryas/mail-shadow-mcp
//
// cmd_serve.go:
// Implements the "serve" subcommand. Opens the database, runs an initial
// sync, schedules periodic re-syncs, and starts the MCP server.
// Transport mode is controlled by the 'transport' field in config.yaml:
//
//	stdio (default) — stdin/stdout, used by Claude Desktop and local tools
//	http            — StreamableHTTP server, recommended for Docker/remote setups
//	sse             — legacy SSE transport
package main

import (
	"crypto/subtle"
	"database/sql"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/mark3labs/mcp-go/server"

	"github.com/dryas/mail-shadow-mcp/internal/config"
	"github.com/dryas/mail-shadow-mcp/internal/db"
	"github.com/dryas/mail-shadow-mcp/internal/fileserver"
	mcpserver "github.com/dryas/mail-shadow-mcp/internal/mcp"
	imapsync "github.com/dryas/mail-shadow-mcp/internal/sync"
)

// startMCPServer starts the MCP transport selected by cfg.Transport.
func startMCPServer(mcpSrv *server.MCPServer, cfg *config.Config) error {
	addr := cfg.HTTPAddr
	if addr == "" {
		addr = ":8080"
	}
	switch cfg.Transport {
	case "", "stdio":
		slog.Info("MCP transport: stdio")
		return server.ServeStdio(mcpSrv)
	case "http":
		slog.Info("MCP transport: StreamableHTTP", "addr", addr, "auth", cfg.HTTPBearerToken != "")
		handler := withBearer(cfg.HTTPBearerToken, server.NewStreamableHTTPServer(mcpSrv))
		return serveHTTP(addr, handler)
	case "sse":
		baseURL := cfg.HTTPBaseURL
		if baseURL == "" {
			baseURL = "http://localhost" + addr
		}
		slog.Info("MCP transport: SSE", "addr", addr, "base_url", baseURL, "auth", cfg.HTTPBearerToken != "")
		handler := withBearer(cfg.HTTPBearerToken, server.NewSSEServer(mcpSrv, server.WithBaseURL(baseURL)))
		return serveHTTP(addr, handler)
	default:
		return fmt.Errorf("unknown transport %q — valid values: stdio, http, sse", cfg.Transport)
	}
}

// serveHTTP starts the MCP HTTP transport with defensive timeouts.
//
// WriteTimeout is deliberately not set: StreamableHTTP/SSE responses can stay
// open for as long as the agent keeps the session.  ReadHeaderTimeout and
// IdleTimeout still bound slow/idle clients, and the handler's own read limits
// bound request bodies.
func serveHTTP(addr string, handler http.Handler) error {
	srv := &http.Server{
		Addr:              addr,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       2 * time.Minute,
		MaxHeaderBytes:    1 << 20,
	}
	return srv.ListenAndServe()
}

// withBearer wraps an http.Handler with Bearer token authentication.
// If token is empty the handler is returned unwrapped (no auth).
func withBearer(token string, next http.Handler) http.Handler {
	if token == "" {
		return next
	}
	const prefix = "Bearer "
	expected := []byte(token)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		auth := r.Header.Get("Authorization")
		ok := len(auth) >= len(prefix) &&
			strings.EqualFold(auth[:len(prefix)], prefix) &&
			subtle.ConstantTimeCompare([]byte(auth[len(prefix):]), expected) == 1
		if !ok {
			w.Header().Set("WWW-Authenticate", `Bearer realm="mail-shadow-mcp"`)
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func cmdServe(args []string) {
	fs := flag.NewFlagSet("serve", flag.ExitOnError)
	cfgPath := fs.String("config", "", "path to config.yaml (default: CONFIG_PATH env or config.yaml)")
	fs.Usage = func() {
		fmt.Fprintf(os.Stderr, "Usage: mail-shadow-mcp serve [flags]\n\nFlags:\n")
		fs.PrintDefaults()
	}
	fs.Parse(args) //nolint:errcheck

	if *cfgPath == "" {
		*cfgPath = os.Getenv("CONFIG_PATH")
	}
	if *cfgPath == "" {
		*cfgPath = "config.yaml"
	}

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "mail-shadow-mcp: load config: %v\n", err)
		os.Exit(1)
	}

	if err := setupLogging(cfg); err != nil {
		fmt.Fprintf(os.Stderr, "mail-shadow-mcp: setup logging: %v\n", err)
		os.Exit(1)
	}

	database := openDatabase(cfg)
	defer database.Close()

	if err := os.MkdirAll(cfg.AttachmentDir, 0o755); err != nil {
		fmt.Fprintf(os.Stderr, "mail-shadow-mcp: create attachment_dir: %v\n", err)
		os.Exit(1)
	}

	syncInterval := 5 * time.Minute
	if cfg.SyncIntervalMin > 0 {
		syncInterval = time.Duration(cfg.SyncIntervalMin) * time.Minute
	}

	fmt.Fprintf(os.Stderr, banner, version)
	slog.Info("mail-shadow-mcp starting",
		"version", version,
		"db", cfg.Database.Path,
		"accounts", len(cfg.Accounts),
		"sync_interval", syncInterval,
	)

	// Poll accounts: initial sync + periodic ticker.
	runSync := func() {
		results := imapsync.RunAll(cfg, database)
		for _, r := range results {
			if r.Err != nil {
				slog.Error("sync failed", "account", r.AccountID, "err", r.Err)
			} else {
				slog.Info("sync ok", "account", r.AccountID)
			}
		}
	}
	go func() {
		// A brand-new instance (no synced UID yet) fetches its mail
		// immediately; only a restart with existing data is jittered.
		time.Sleep(imapsync.InitialDelay(database, cfg, syncInterval))
		runSync()
	}()
	go func() {
		ticker := time.NewTicker(syncInterval)
		defer ticker.Stop()
		for range ticker.C {
			runSync()
		}
	}()

	// IDLE accounts: persistent push-based sync (no-op if none configured).
	go imapsync.RunIDLE(cfg, database)

	// The sync loop only gives the CJK backfill a short slice per folder so a
	// large mailbox cannot delay new-mail sync; this worker drains the rest in
	// the background with the same bounded passes.
	go imapsync.RunCJKBackfill(cfg, database)

	dlServer := startFileServer(cfg)

	mcpSrv := mcpserver.New(database, cfg, version, dlServer)
	if err := startMCPServer(mcpSrv, cfg); err != nil {
		fmt.Fprintf(os.Stderr, "mail-shadow-mcp: server error: %v\n", err)
		os.Exit(1)
	}
}

// openDatabase opens and migrates the SQLite database, exiting on error.
func openDatabase(cfg *config.Config) *sql.DB {
	dbPath := cfg.Database.Path
	if dbPath == "" {
		dbPath = "data/mail.db"
	}
	// SQLite temp b-tree pages and FTS merge spills must not go to the
	// container's 16 MB /tmp tmpfs: writes there count against the container
	// memory limit.  Put them on the /data volume instead.
	if dbPath != ":memory:" && !strings.HasPrefix(dbPath, "file:") {
		tmpDir := filepath.Join(filepath.Dir(dbPath), "tmp")
		if err := os.MkdirAll(tmpDir, 0o700); err != nil {
			slog.Warn("could not create sqlite temp dir", "dir", tmpDir, "err", err)
		} else {
			os.Setenv("SQLITE_TMPDIR", tmpDir)
			os.Setenv("TMPDIR", tmpDir)
		}
	}
	database, err := db.Open(dbPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "mail-shadow-mcp: open db: %v\n", err)
		os.Exit(1)
	}
	if err := db.Migrate(database); err != nil {
		database.Close()
		fmt.Fprintf(os.Stderr, "mail-shadow-mcp: migrate db: %v\n", err)
		os.Exit(1)
	}
	return database
}

// startFileServer starts the optional attachment download server if configured.
func startFileServer(cfg *config.Config) *fileserver.Server {
	if cfg.FileServerPort <= 0 {
		return nil
	}
	srv, err := fileserver.New(cfg.FileServerPort, cfg.FileServerTTL, cfg.FileServerHost)
	if err != nil {
		fmt.Fprintf(os.Stderr, "mail-shadow-mcp: start fileserver: %v\n", err)
		os.Exit(1)
	}
	return srv
}

// setupLogging configures the global slog logger.
// If cfg.LogFile is set the log output is written to that file (append mode);
// otherwise it falls back to stderr. cfg.LogLevel controls the minimum level
// (debug/info/warn/error); defaults to info.
func setupLogging(cfg *config.Config) error {
	var w io.Writer = os.Stderr
	if cfg.LogFile != "" {
		if err := os.MkdirAll(filepath.Dir(cfg.LogFile), 0o755); err != nil {
			return fmt.Errorf("create log dir: %w", err)
		}
		f, err := os.OpenFile(cfg.LogFile, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			return fmt.Errorf("open log file %q: %w", cfg.LogFile, err)
		}
		// Write banner to stderr even when logging to file so the terminal
		// gives immediate feedback.
		w = f
	}

	var level slog.Level
	switch cfg.LogLevel {
	case "debug":
		level = slog.LevelDebug
	case "warn":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	default:
		level = slog.LevelInfo
	}

	opts := &slog.HandlerOptions{Level: level}
	var h slog.Handler
	if cfg.LogFormat == "json" {
		h = slog.NewJSONHandler(w, opts)
	} else {
		h = slog.NewTextHandler(w, opts)
	}
	slog.SetDefault(slog.New(h))
	return nil
}
