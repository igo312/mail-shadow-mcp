package imapsync

import (
	"crypto/tls"
	"database/sql"
	"fmt"
	"log/slog"
	"math/rand"
	"sync"
	"time"

	"github.com/emersion/go-imap/v2"
	"github.com/emersion/go-imap/v2/imapclient"

	"github.com/dryas/mail-shadow-mcp/internal/config"
)

// AccountResult holds the outcome of syncing a single account.
type AccountResult struct {
	AccountID string
	Err       error
}

// accountLocks prevents concurrent RunAll calls for the same account.
// If a full account sync is already running when the ticker fires, the new run is skipped.
//
// folderLocks prevents concurrent syncs of the same account+folder pair.
// This is important during the initial import: RunAll and the IDLE initial sync
// could otherwise both start fetching the same folder from UID 0, writing every
// message twice. Callers use TryLock and skip the folder when it is busy.
var (
	accountLocksMu sync.Mutex
	accountLocks   = map[string]*sync.Mutex{}

	folderLocksMu sync.Mutex
	folderLocks   = map[string]*sync.Mutex{}
)

func accountMutex(id string) *sync.Mutex {
	accountLocksMu.Lock()
	defer accountLocksMu.Unlock()
	if _, ok := accountLocks[id]; !ok {
		accountLocks[id] = &sync.Mutex{}
	}
	return accountLocks[id]
}

// folderMutex returns the mutex for a specific account+folder pair.
func folderMutex(accountID, folder string) *sync.Mutex {
	key := accountID + "\x00" + folder
	folderLocksMu.Lock()
	defer folderLocksMu.Unlock()
	if _, ok := folderLocks[key]; !ok {
		folderLocks[key] = &sync.Mutex{}
	}
	return folderLocks[key]
}

// Jitter returns a random delay up to half the sync interval, capped at the
// configured maximum (five minutes when that is zero).  With 100 containers a
// host restart would otherwise start every initial sync and every IDLE initial
// sync at the same moment, producing a memory spike across the whole host.
// Spreading the starts keeps the peak close to the per-container steady state.
func Jitter(interval, maxJitter time.Duration) time.Duration {
	if interval <= 0 {
		return 0
	}
	if maxJitter <= 0 {
		maxJitter = 5 * time.Minute
	}
	max := interval / 2
	if maxJitter < max {
		max = maxJitter
	}
	if max <= 0 {
		return 0
	}
	return time.Duration(rand.Int63n(int64(max)))
}

// InitialDelay is the delay before the first sync after the process starts.
//
// Jitter exists to avoid 100 containers hitting their IMAP servers together
// after a host restart, but applying it to a brand-new instance means a user
// who just added a mailbox sees no mail (and no searchable data) for up to half
// the sync interval.  Only an instance that already has synced data is a
// restart; a fresh database syncs immediately.
func InitialDelay(db *sql.DB, cfg *config.Config, interval time.Duration) time.Duration {
	if interval <= 0 {
		return 0
	}
	var synced int
	if err := db.QueryRow(
		`SELECT COUNT(*) FROM sync_state WHERE last_uid > 0`,
	).Scan(&synced); err != nil {
		// The table always exists after migration; if it does not, prefer the
		// immediate first sync over an unexplained delay.
		return 0
	}
	if synced == 0 {
		return 0
	}
	return Jitter(interval, time.Duration(cfg.SyncJitterSec)*time.Second)
}

// RunAll syncs every account in the config concurrently.
// Each account runs in its own goroutine; a failure in one account does
// not prevent the others from completing.
// If a sync for an account is already in progress (e.g. from the previous
// ticker interval), that account is skipped rather than running twice.
func RunAll(cfg *config.Config, db *sql.DB) []AccountResult {
	results := make([]AccountResult, len(cfg.Accounts))
	var wg sync.WaitGroup

	for i, acc := range cfg.Accounts {
		wg.Add(1)
		go func(idx int, accCfg config.AccountConfig) {
			defer wg.Done()
			defer func() {
				if r := recover(); r != nil {
					slog.Error("sync panic recovered", "account", accCfg.ID, "panic", r)
					results[idx] = AccountResult{
						AccountID: accCfg.ID,
						Err:       fmt.Errorf("panic: %v", r),
					}
				}
			}()

			mu := accountMutex(accCfg.ID)
			if !mu.TryLock() {
				slog.Warn("sync already running, skipping", "account", accCfg.ID)
				results[idx] = AccountResult{AccountID: accCfg.ID}
				return
			}
			defer mu.Unlock()

			results[idx] = AccountResult{
				AccountID: accCfg.ID,
				Err:       syncAccount(accCfg, db),
			}
		}(i, acc)
	}

	wg.Wait()
	return results
}

// syncAccount syncs every folder of one account.
//
// Every folder gets its own IMAP connection. A message whose response the
// decoder cannot handle destroys the connection it was fetched on, so a shared
// connection would take all remaining folders down with it — which is exactly
// how one malformed message used to fail a whole account.
func syncAccount(accCfg config.AccountConfig, db *sql.DB) error {
	logger := slog.With("account", accCfg.ID)

	// Folder discovery needs a connection, but only when no explicit list is
	// configured. It is a short-lived connection of its own.
	var listClient *Client
	if len(accCfg.Folders) == 0 {
		c, err := NewClient(accCfg)
		if err != nil {
			return fmt.Errorf("connect: %w", err)
		}
		listClient = c
	}

	folders, err := resolveFolders(listClient, accCfg)
	if listClient != nil {
		listClient.Close()
	}
	if err != nil {
		return fmt.Errorf("resolve folders: %w", err)
	}

	logger.Info("starting account sync", "folders", len(folders), "folder_list", folders)

	var errs []error
	for i, folder := range folders {
		logger.Info("syncing folder", "folder", folder, "progress", fmt.Sprintf("%d/%d", i+1, len(folders)))
		fmu := folderMutex(accCfg.ID, folder)
		if !fmu.TryLock() {
			logger.Warn("folder sync already running, skipping", "folder", folder)
			continue
		}
		err := syncFolderResilient(accCfg, db, folder)
		fmu.Unlock()
		if err != nil {
			logger.Error("folder sync failed", "folder", folder, "err", err)
			errs = append(errs, fmt.Errorf("folder %q: %w", folder, err))
		}
	}

	if len(errs) > 0 {
		return fmt.Errorf("%d folder(s) failed: %w", len(errs), errs[0])
	}
	return nil
}

// folderSyncAttempts is how often a folder sync is retried within a single run.
// Each attempt resumes from the persisted last_uid, so an interrupted folder
// costs at most one sync window of re-fetching.
const (
	folderSyncAttempts = 3
	folderRetryBackoff = 5 * time.Second
)

// syncFolderResilient syncs one folder on a dedicated connection and retries
// (resuming from the checkpoint) when the connection dies mid-folder.
func syncFolderResilient(accCfg config.AccountConfig, db *sql.DB, folder string) error {
	logger := slog.With("account", accCfg.ID, "folder", folder)

	var lastErr error
	for attempt := 1; attempt <= folderSyncAttempts; attempt++ {
		client, err := NewClient(accCfg)
		if err != nil {
			// Server unreachable — the next scheduled sync will try again.
			return fmt.Errorf("connect: %w", err)
		}
		lastErr = client.SyncFolder(db, folder)
		client.Close()
		if lastErr == nil {
			return nil
		}
		if attempt < folderSyncAttempts {
			logger.Warn("folder sync interrupted, resuming from the last checkpoint",
				"attempt", attempt, "err", lastErr)
			time.Sleep(time.Duration(attempt) * folderRetryBackoff)
		}
	}
	return lastErr
}

// resolveFolders returns the list of folders to sync for an account.
// If the account config specifies explicit folders those are used;
// otherwise all folders discovered via IMAP LIST are used.
func resolveFolders(client *Client, accCfg config.AccountConfig) ([]string, error) {
	if len(accCfg.Folders) > 0 {
		return accCfg.Folders, nil
	}
	return client.ListFolders()
}

// RunIDLE starts a persistent IMAP IDLE loop for each folder listed in
// idle_folders for every account that has at least one idle_folder configured.
// One goroutine per folder is launched, each holding its own dedicated IMAP
// connection. The function blocks until all goroutines exit (which normally
// never happens — they run forever).
//
// RunIDLE and RunAll are independent: poll-based full syncs continue on the
// configured interval regardless of IDLE. IDLE only provides faster detection
// of new messages in the explicitly listed folders.
func RunIDLE(cfg *config.Config, db *sql.DB) {
	var wg sync.WaitGroup
	for _, acc := range cfg.Accounts {
		if len(acc.IdleFolders) == 0 {
			continue
		}
		for _, folder := range acc.IdleFolders {
			wg.Add(1)
			go func(accCfg config.AccountConfig, f string) {
				defer wg.Done()
				idleLoop(accCfg, f, cfg, db)
			}(acc, folder)
		}
	}
	wg.Wait()
}

// idleLoop keeps restarting an IDLE session for one account+folder pair.
// On error it waits before reconnecting using exponential backoff.
func idleLoop(accCfg config.AccountConfig, folder string, cfg *config.Config, db *sql.DB) {
	logger := slog.With("account", accCfg.ID, "folder", folder)
	backoff := 30 * time.Second
	for {
		err := runIdleSession(accCfg, folder, cfg, db, logger)
		if err != nil {
			logger.Error("IDLE session ended with error, reconnecting", "err", err, "backoff", backoff)
			time.Sleep(backoff)
			if backoff < 5*time.Minute {
				backoff *= 2
			}
		} else {
			backoff = 30 * time.Second
		}
	}
}

// runIdleSession opens a dedicated IMAP connection for a single folder,
// waits for EXISTS notifications and triggers a targeted sync of that folder.
func runIdleSession(accCfg config.AccountConfig, folder string, cfg *config.Config, db *sql.DB, logger *slog.Logger) (retErr error) {
	defer func() {
		if r := recover(); r != nil {
			slog.Error("IDLE panic recovered", "account", accCfg.ID, "folder", folder, "panic", r)
			retErr = fmt.Errorf("panic: %v", r)
		}
	}()

	// existsCh is buffered so the unilateral handler never blocks.
	existsCh := make(chan struct{}, 1)

	idleClient, err := newIdleClient(accCfg, existsCh)
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer idleClient.Close()

	// Check IDLE capability; fall back to polling this folder if unsupported.
	caps := idleClient.Caps()
	if !caps.Has(imap.CapIdle) {
		logger.Warn("server does not support IDLE, polling instead")
		pollInterval := time.Duration(cfg.SyncIntervalMin) * time.Minute
		for {
			if err := syncOneFolder(accCfg, folder, db); err != nil {
				logger.Error("poll sync failed", "err", err)
			}
			time.Sleep(pollInterval)
		}
	}

	// SELECT the folder so we receive unilateral EXISTS messages for it.
	if _, err := idleClient.Select(folder, nil).Wait(); err != nil {
		return fmt.Errorf("select %q: %w", folder, err)
	}

	// Initial sync on connect.  A fresh instance syncs now; a restart with
	// existing data is spread across containers so 100 imports do not start at
	// once.
	logger.Info("IDLE: initial sync")
	time.Sleep(InitialDelay(db, cfg, time.Duration(cfg.SyncIntervalMin)*time.Minute))
	if err := syncOneFolder(accCfg, folder, db); err != nil {
		logger.Error("IDLE: initial sync failed", "err", err)
	}

	logger.Info("IDLE: entering idle loop")
	for {
		idleCmd, err := idleClient.Idle()
		if err != nil {
			return fmt.Errorf("idle: %w", err)
		}

		// Run Wait in a separate goroutine so we can also listen for other signals.
		waitDone := make(chan error, 1)
		go func() { waitDone <- idleCmd.Wait() }()

		select {
		case <-existsCh:
			logger.Info("IDLE: new mail detected, syncing")
			if err := idleCmd.Close(); err != nil {
				return fmt.Errorf("idle close: %w", err)
			}
			if err := <-waitDone; err != nil {
				logger.Warn("IDLE: wait after close returned error", "err", err)
			}
		case err := <-waitDone:
			// Server terminated IDLE unexpectedly.
			return fmt.Errorf("idle wait: %w", err)
		case <-idleClient.Closed():
			idleCmd.Close() //nolint:errcheck
			return fmt.Errorf("connection closed")
		}

		if err := syncOneFolder(accCfg, folder, db); err != nil {
			logger.Error("IDLE: sync failed", "err", err)
		}
	}
}

// syncOneFolder opens a fresh IMAP connection, syncs a single folder and
// closes the connection. Used by the IDLE path to avoid blocking the dedicated
// IDLE connection with heavy fetch operations.
// If the folder is already being synced (e.g. by RunAll during initial import)
// this call is a no-op to prevent double-importing.
func syncOneFolder(accCfg config.AccountConfig, folder string, db *sql.DB) error {
	fmu := folderMutex(accCfg.ID, folder)
	if !fmu.TryLock() {
		slog.Info("IDLE: folder sync already running, skipping", "account", accCfg.ID, "folder", folder)
		return nil
	}
	defer fmu.Unlock()

	return syncFolderResilient(accCfg, db, folder)
}

// newIdleClient creates a dedicated IMAP connection with a unilateral data
// handler that sends on existsCh whenever a new message count (EXISTS) is
// reported by the server.
func newIdleClient(accCfg config.AccountConfig, existsCh chan<- struct{}) (*imapclient.Client, error) {
	addr := fmt.Sprintf("%s:%d", accCfg.Host, accCfg.Port)

	var tlsCfg *tls.Config
	if accCfg.TLSSkipVerify {
		tlsCfg = &tls.Config{InsecureSkipVerify: true} //nolint:gosec
	}

	handler := &imapclient.UnilateralDataHandler{
		Mailbox: func(data *imapclient.UnilateralDataMailbox) {
			if data.NumMessages != nil {
				select {
				case existsCh <- struct{}{}:
				default: // already pending
				}
			}
		},
	}

	opts := &imapclient.Options{
		TLSConfig:             tlsCfg,
		UnilateralDataHandler: handler,
	}

	var c *imapclient.Client
	var err error

	switch accCfg.TLSMode {
	case "starttls":
		c, err = imapclient.DialStartTLS(addr, opts)
	case "none":
		c, err = imapclient.DialInsecure(addr, opts)
	default:
		c, err = imapclient.DialTLS(addr, opts)
	}
	if err != nil {
		return nil, fmt.Errorf("idle: dial %s: %w", addr, err)
	}

	if err := authenticate(c, accCfg.Username, accCfg.Password); err != nil {
		c.Close()
		return nil, fmt.Errorf("idle: authenticate %s: %w", accCfg.Username, err)
	}

	return c, nil
}
