"""Read-only statistics for the admin console.

Everything the admin console shows about a mailbox is derived from two places:

* the control database (email, status, folder *selection*, timestamps), and
* the instance's own shadow database, read straight from its Docker volume
  (``/var/lib/docker/volumes/msm-data-<id>/_data/mail.db``).

The portal deliberately does not ask the instance over HTTP: the MCP endpoint
only speaks JSON-RPC, and the portal has no copy of the instance's bearer
token.  Reading the volume is a read-only SQLite connection, which is safe in
WAL mode even while the instance is writing.

Two rules keep this cheap and safe:

* every query runs behind a progress handler with a wall-clock budget, so a
  25 GB mailbox can never hold a read transaction open or block the collector;
* collection happens in a background thread on a slow cadence, and pages always
  render the last snapshot.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .provisioner import folders_from_instance

log = logging.getLogger(__name__)

# How long a single SQLite query may run before SQLite interrupts it.
QUERY_BUDGET_SECONDS = 5.0
# Directory walks (attachment folders) are bounded by time *and* entry count.
WALK_BUDGET_SECONDS = 2.0
WALK_MAX_ENTRIES = 30000
# Pause between two instances so the collector never turns into a disk spike.
COLLECT_GAP_SECONDS = 0.2


class QueryTimeout(RuntimeError):
    """A bounded SQLite query hit its wall-clock budget."""


# ---------------------------------------------------------------------------
# formatting helpers (shared with the templates)
# ---------------------------------------------------------------------------
_UNITS = (("TB", 1024.0**4), ("GB", 1024.0**3), ("MB", 1024.0**2), ("KB", 1024.0))


def human_bytes(value: int | float | None) -> str:
    if value is None:
        return "—"
    amount = float(value)
    if amount < 1024:
        return f"{int(amount)} B"
    for suffix, size in _UNITS:
        if amount >= size:
            return f"{amount / size:.1f} {suffix}"
    return f"{int(amount)} B"


def human_count(value: int | float | None) -> str:
    if value is None:
        return "—"
    amount = int(value)
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.2f} 亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.1f} 万"
    return f"{amount:,}"


_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]*)\s*$")
_SIZE_FACTORS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def parse_docker_size(text: str) -> int:
    """Parse Docker's human sizes ("12.3MiB", "647kB", "0B") into bytes."""
    match = _SIZE_RE.match(text or "")
    if not match:
        return 0
    amount = float(match.group(1))
    suffix = (match.group(2) or "B").upper()
    factor = _SIZE_FACTORS.get(suffix)
    if factor is None:
        return 0
    return int(amount * factor)


def parse_pair(text: str) -> tuple[int, int]:
    """Parse Docker's "a / b" pairs (NetIO, BlockIO) into two byte counts."""
    left, _, right = (text or "").partition("/")
    return parse_docker_size(left), parse_docker_size(right)


def parse_percent(text: str) -> float:
    try:
        return float((text or "").strip().rstrip("%"))
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# shadow database
# ---------------------------------------------------------------------------
class _Deadline:
    def __init__(self, seconds: float) -> None:
        self.expires = time.monotonic() + seconds

    def __call__(self) -> int:
        return 1 if time.monotonic() > self.expires else 0


def open_shadow_db(path: Path, *, budget: float = QUERY_BUDGET_SECONDS) -> sqlite3.Connection:
    """Open the instance database read-only.

    ``mode=ro`` keeps the promise that the admin console cannot modify a
    mailbox, and WAL means a concurrent writer does not block us for long.
    """
    uri = Path(path).absolute().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=3.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=3000")
    connection.set_progress_handler(_Deadline(budget), 5000)
    return connection


def _query(
    connection: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    *,
    budget: float = QUERY_BUDGET_SECONDS,
) -> list[sqlite3.Row]:
    connection.set_progress_handler(_Deadline(budget), 5000)
    try:
        return connection.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc).lower():
            raise QueryTimeout(sql.split("\n", 1)[0][:80]) from exc
        raise


def read_shadow_stats(
    db_path: Path, *, budget: float = QUERY_BUDGET_SECONDS
) -> dict[str, Any]:
    """Count messages per folder and the sync bookkeeping of one instance."""
    result: dict[str, Any] = {
        "ok": False,
        "error": "",
        "messages": 0,
        "folders": [],
        "newest_mail": "",
        "oldest_mail": "",
        "skipped": 0,
        "attachments": 0,
        "attachment_bytes": 0,
        "partial": [],
        "read_at": int(time.time()),
    }
    if not db_path.exists():
        result["error"] = "影子数据库不存在"
        return result
    try:
        connection = open_shadow_db(db_path, budget=budget)
    except (sqlite3.Error, OSError) as exc:
        result["error"] = f"无法打开影子数据库：{exc}"
        return result

    folders: dict[str, dict[str, Any]] = {}
    try:
        rows = _query(
            connection,
            "SELECT imap_folder, COUNT(*), MAX(imap_uid) FROM mail_entries"
            " GROUP BY imap_folder",
            budget=budget,
        )
        for row in rows:
            name = str(row[0])
            folders[name] = {
                "name": name,
                "messages": int(row[1] or 0),
                "newest_uid": int(row[2] or 0),
                "synced_uid": 0,
                "pending": 0,
                "cjk_done": False,
            }
    except QueryTimeout:
        result["partial"].append("mail_entries")
    except sqlite3.Error as exc:
        result["error"] = f"读取邮件表失败：{exc}"

    try:
        rows = _query(
            connection,
            "SELECT imap_folder, last_uid, cjk_backfill_done FROM sync_state",
            budget=min(budget, 2.0),
        )
        for row in rows:
            name = str(row[0])
            entry = folders.setdefault(
                name,
                {
                    "name": name,
                    "messages": 0,
                    "newest_uid": 0,
                    "synced_uid": 0,
                    "pending": 0,
                    "cjk_done": False,
                },
            )
            entry["synced_uid"] = int(row[1] or 0)
            entry["cjk_done"] = bool(row[2])
    except QueryTimeout:
        result["partial"].append("sync_state")
    except sqlite3.Error:
        pass

    try:
        rows = _query(
            connection,
            "SELECT MIN(date_utc), MAX(date_utc) FROM mail_entries",
            budget=min(budget, 2.0),
        )
        if rows:
            result["oldest_mail"] = str(rows[0][0] or "")
            result["newest_mail"] = str(rows[0][1] or "")
    except (QueryTimeout, sqlite3.Error):
        result["partial"].append("dates")

    try:
        rows = _query(
            connection,
            "SELECT COUNT(*) FROM sync_skipped",
            budget=min(budget, 2.0),
        )
        result["skipped"] = int(rows[0][0] or 0) if rows else 0
    except (QueryTimeout, sqlite3.Error):
        result["partial"].append("skipped")

    try:
        rows = _query(
            connection,
            "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM mail_attachments",
            budget=min(budget, 3.0),
        )
        if rows:
            result["attachments"] = int(rows[0][0] or 0)
            result["attachment_bytes"] = int(rows[0][1] or 0)
    except (QueryTimeout, sqlite3.Error):
        result["partial"].append("attachments")
    finally:
        connection.close()

    for entry in folders.values():
        synced = int(entry["synced_uid"])
        newest = int(entry["newest_uid"])
        # A UID gap is a *heuristic*: it usually means "new mail not fetched
        # yet", but deletions and UIDVALIDITY resets also produce gaps.
        entry["pending"] = max(0, newest - synced) if synced else 0

    result["folders"] = sorted(
        folders.values(),
        key=lambda item: (
            str(item["name"]).lower() != "inbox",
            -int(item["messages"]),
            str(item["name"]),
        ),
    )
    result["messages"] = sum(int(item["messages"]) for item in folders.values())
    result["ok"] = not result["error"]
    return result


def _db_file_bytes(db_path: Path) -> tuple[int, int]:
    """Return (database bytes, WAL bytes) without opening the database."""
    size = 0
    wal = 0
    try:
        size = db_path.stat().st_size
    except OSError:
        pass
    try:
        wal = db_path.with_name(db_path.name + "-wal").stat().st_size
    except OSError:
        pass
    return size, wal


def dir_usage(
    path: Path,
    *,
    budget: float = WALK_BUDGET_SECONDS,
    max_entries: int = WALK_MAX_ENTRIES,
) -> dict[str, Any]:
    """Sum the size of a directory tree, bounded in time and entry count."""
    files = 0
    total = 0
    entries = 0
    truncated = False
    deadline = time.monotonic() + budget
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for item in iterator:
                entries += 1
                if entries > max_entries or time.monotonic() > deadline:
                    truncated = True
                    break
                try:
                    if item.is_dir(follow_symlinks=False):
                        stack.append(Path(item.path))
                    elif item.is_file(follow_symlinks=False):
                        files += 1
                        total += item.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        if truncated:
            break
    return {"files": files, "bytes": total, "truncated": truncated}


# ---------------------------------------------------------------------------
# host metrics
# ---------------------------------------------------------------------------
def host_metrics(volume_root: Path) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "load1": None,
        "load5": None,
        "load15": None,
        "mem_total": 0,
        "mem_available": 0,
        "disk_total": 0,
        "disk_used": 0,
        "disk_free": 0,
        "cpu_count": os.cpu_count() or 0,
    }
    try:
        load = os.getloadavg()
    except (AttributeError, OSError):
        pass
    else:
        metrics["load1"], metrics["load5"], metrics["load15"] = (round(x, 2) for x in load)

    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                if key == "MemTotal":
                    metrics["mem_total"] = int(rest.split()[0]) * 1024
                elif key == "MemAvailable":
                    metrics["mem_available"] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass

    target = volume_root if volume_root.exists() else Path("/")
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        pass
    else:
        metrics["disk_total"] = usage.total
        metrics["disk_used"] = usage.used
        metrics["disk_free"] = usage.free
    return metrics


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------
class StatsCollector:
    """Refresh a snapshot of every instance on a slow background cadence."""

    def __init__(self, config: Any, db: Any, docker: Any) -> None:
        self.config = config
        self.db = db
        self.docker = docker
        self._lock = threading.Lock()
        self._snapshot: dict[str, Any] = {
            "generated_at": 0,
            "instances": {},
            "host": {},
            "error": "",
        }
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_prune = 0.0
        self.volume_root = self._resolve_volume_root()

    def _resolve_volume_root(self) -> Path:
        """Find where the daemon keeps named volumes.

        ``/var/lib/docker/volumes`` is the common default, but a host can move
        its data root (this deployment keeps it under /mnt/data).  Ask the
        daemon instead of guessing, once, at start-up.
        """
        configured = Path(
            str(getattr(self.config, "docker_volume_root", "/var/lib/docker/volumes"))
        )
        if configured.is_dir():
            return configured
        daemon_root = ""
        if self.docker is not None:
            try:
                daemon_root = self.docker.docker_root()
            except Exception:  # pragma: no cover - best effort
                daemon_root = ""
        if daemon_root:
            candidate = Path(daemon_root) / "volumes"
            if candidate.is_dir():
                log.info(
                    "volume root %s missing; using the daemon's own root %s",
                    configured,
                    candidate,
                )
                return candidate
        log.warning(
            "Docker volume root %s does not exist; instance sizes will read as empty",
            configured,
        )
        return configured

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="msm-stats", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh(self) -> None:
        """Ask the collector to run again as soon as possible."""
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generated_at": self._snapshot["generated_at"],
                "instances": dict(self._snapshot["instances"]),
                "host": dict(self._snapshot["host"]),
                "error": self._snapshot["error"],
            }

    def mark(self, instance_id: str, **changes: Any) -> None:
        """Patch one cached entry so the UI reflects an admin action at once.

        A full re-collection takes seconds (seven databases, one after
        another); rendering the previous snapshot right after "stop" would
        still say 运行中.
        """
        with self._lock:
            entry = self._snapshot["instances"].get(str(instance_id))
            if entry is None:
                return
            updated = dict(entry)
            updated.update(changes)
            self._snapshot["instances"][str(instance_id)] = updated

    def forget(self, instance_id: str) -> None:
        """Drop a removed instance from the cached snapshot."""
        with self._lock:
            self._snapshot["instances"].pop(str(instance_id), None)

    def _run(self) -> None:
        interval = max(30, int(getattr(self.config, "stats_interval_seconds", 300)))
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.collect_once()
            except Exception:  # pragma: no cover - defensive
                log.exception("stats collection failed")
            elapsed = time.monotonic() - started
            self._wake.wait(max(10.0, interval - elapsed))
            self._wake.clear()

    # -- collection ------------------------------------------------------
    def collect_once(self) -> dict[str, Any]:
        instances = [row for row in self.db.list_instances() if row["status"] != "removed"]
        containers: dict[str, Any] = {}
        stats_by_name: dict[str, Any] = {}
        error = ""
        if self.docker is not None:
            try:
                for container in self.docker.list_managed_containers():
                    containers[str(container.get("Names") or "")] = container
            except Exception as exc:
                error = f"docker ps 失败：{_short(exc)}"
            try:
                stats_by_name = self.docker.container_stats()
            except Exception as exc:
                error = error or f"docker stats 失败：{_short(exc)}"

        entries: dict[str, Any] = {}
        for index, instance in enumerate(instances):
            name = str(instance["container_name"])
            entry: dict[str, Any]
            try:
                entry = self.collect_instance(
                    instance, containers.get(name), stats_by_name.get(name)
                )
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("stats collection failed for %s", instance.get("email"))
                entry = empty_entry(instance)
                entry["error"] = f"采集失败：{_short(exc)}"
            entries[str(instance["id"])] = entry
            self._record_sample(instance, entry)
            if index + 1 < len(instances):
                time.sleep(COLLECT_GAP_SECONDS)

        snapshot = {
            "generated_at": int(time.time()),
            "instances": entries,
            "host": host_metrics(self.volume_root),
            "error": error,
        }
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _recent_log_error(self, instance: dict[str, Any]) -> str:
        """Return the newest ERROR line from the container log, if any.

        A mailbox with a revoked app password keeps its container running and
        shows a healthy row on the dashboard; the only signal is this log line,
        so surface it instead of leaving the operator to run `docker logs`.
        """
        if self.docker is None:
            return ""
        try:
            text = self.docker.logs(str(instance["container_name"]), tail=80)
        except Exception:
            return ""
        for line in reversed((text or "").splitlines()):
            lowered = line.lower()
            if "level=error" not in lowered and '"level":"error"' not in lowered:
                continue
            cleaned = re.sub(r"^time=\S+\s+level=ERROR\s+", "", line).strip()
            cleaned = re.sub(r'"msg":\s*"', "", cleaned)
            return cleaned[:200]
        return ""

    def collect_instance(
        self,
        instance: dict[str, Any],
        container: dict[str, Any] | None,
        container_stats: dict[str, Any] | None,
    ) -> dict[str, Any]:
        entry = empty_entry(instance)
        if entry["configured"].get("source") == "deployment":
            # "Deployment default" is only useful with the actual value:
            # MSM_SYNC_FOLDERS can mean "every folder", which is a very
            # different (and much more expensive) scope.
            default = [
                str(name) for name in (getattr(self.config, "sync_folders", ()) or ())
            ]
            entry["configured"] = {**entry["configured"], "default": default}
        if container:
            entry["container_state"] = str(container.get("State") or "")
            entry["container_status"] = str(container.get("Status") or "")
            entry["image"] = str(container.get("Image") or "")
        if container_stats:
            entry["cpu_percent"] = parse_percent(str(container_stats.get("CPUPerc") or ""))
            memory, limit = _split_pair(str(container_stats.get("MemUsage") or ""))
            entry["mem_bytes"] = memory
            entry["mem_limit"] = limit
            entry["net_rx"], entry["net_tx"] = parse_pair(
                str(container_stats.get("NetIO") or "")
            )
            try:
                entry["pids"] = int(str(container_stats.get("PIDs") or "0").strip())
            except ValueError:
                entry["pids"] = 0
        entry["log_error"] = self._recent_log_error(instance)

        data_dir = self.volume_root / str(instance["data_volume"]) / "_data"
        db_path = data_dir / "mail.db"
        shadow: dict[str, Any]
        if not data_dir.is_dir():
            shadow = {
                "ok": False,
                "error": "数据卷不存在（容器可能还没创建成功）",
                "messages": 0,
                "folders": [],
                "pending": 0,
                "skipped": 0,
                "attachments": 0,
                "attachment_bytes": 0,
                "newest_mail": "",
                "oldest_mail": "",
            }
        else:
            shadow = read_shadow_stats(db_path)
            db_bytes, wal_bytes = _db_file_bytes(db_path)
            shadow["db_bytes"] = db_bytes
            shadow["wal_bytes"] = wal_bytes
            shadow["attachment_disk"] = dir_usage(data_dir / "attachments")
            try:
                entry["last_write_at"] = int(
                    max(db_path.stat().st_mtime, (data_dir / "mail.db-wal").stat().st_mtime)
                )
            except OSError:
                entry["last_write_at"] = 0

        entry["ok"] = bool(shadow.get("ok"))
        entry["error"] = str(shadow.get("error") or "")
        entry["messages"] = int(shadow.get("messages") or 0)
        entry["folders"] = list(shadow.get("folders") or [])
        entry["folder_count"] = len(entry["folders"])
        entry["pending"] = sum(int(item["pending"]) for item in entry["folders"])
        entry["cjk_pending"] = sum(1 for item in entry["folders"] if not item["cjk_done"])
        entry["skipped"] = int(shadow.get("skipped") or 0)
        entry["attachments"] = int(shadow.get("attachments") or 0)
        entry["attachment_bytes"] = int(shadow.get("attachment_bytes") or 0)
        entry["newest_mail"] = str(shadow.get("newest_mail") or "")
        entry["oldest_mail"] = str(shadow.get("oldest_mail") or "")
        entry["partial"] = list(shadow.get("partial") or [])
        entry["db_bytes"] = int(shadow.get("db_bytes") or 0)
        entry["wal_bytes"] = int(shadow.get("wal_bytes") or 0)
        disk = shadow.get("attachment_disk") or {}
        entry["attachment_disk_bytes"] = int(disk.get("bytes") or 0)
        entry["attachment_disk_files"] = int(disk.get("files") or 0)
        entry["total_bytes"] = (
            entry["db_bytes"]
            + entry["wal_bytes"]
            + entry["attachment_disk_bytes"]
        )
        entry["growth"] = self._growth(instance["id"])
        return entry

    # -- samples ---------------------------------------------------------
    def _record_sample(self, instance: dict[str, Any], entry: dict[str, Any]) -> None:
        if not entry.get("ok"):
            return
        try:
            self.db.record_instance_sample(
                str(instance["id"]),
                at=int(time.time()),
                messages=int(entry.get("messages") or 0),
                bytes_=int(entry.get("total_bytes") or 0),
            )
            if time.time() - self._last_prune > 3600:
                self._last_prune = time.time()
                self.db.prune_samples(before=int(time.time()) - 30 * 86400)
        except Exception:  # pragma: no cover - stats must never break the page
            log.exception("could not record stats sample")

    def _growth(self, instance_id: str) -> dict[str, Any] | None:
        try:
            return self.db.growth_since(str(instance_id), since=int(time.time()) - 86400)
        except Exception:  # pragma: no cover - defensive
            log.exception("could not read stats growth")
            return None


def _split_pair(text: str) -> tuple[int, int]:
    left, _, right = (text or "").partition("/")
    return parse_docker_size(left), parse_docker_size(right)


def _short(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:200] if text else exc.__class__.__name__


def empty_entry(instance: dict[str, Any]) -> dict[str, Any]:
    selected = folders_from_instance(instance)
    if selected is None:
        configured = {"source": "deployment", "all": False, "names": []}
    elif not selected:
        configured = {"source": "instance", "all": True, "names": []}
    else:
        configured = {"source": "instance", "all": False, "names": list(selected)}
    return {
        "id": str(instance["id"]),
        "email": str(instance["email"]),
        "status": str(instance["status"]),
        "container_name": str(instance["container_name"]),
        "imap_host": str(instance.get("imap_host") or ""),
        "imap_port": int(instance.get("imap_port") or 0),
        "provider_name": str(instance.get("provider_name") or ""),
        "created_at": int(instance.get("created_at") or 0),
        "configured": configured,
        "container_state": "missing",
        "container_status": "容器不存在",
        "image": "",
        "log_error": "",
        "ok": False,
        "error": "",
        "messages": 0,
        "folders": [],
        "folder_count": 0,
        "pending": 0,
        "cjk_pending": 0,
        "skipped": 0,
        "attachments": 0,
        "attachment_bytes": 0,
        "newest_mail": "",
        "oldest_mail": "",
        "partial": [],
        "db_bytes": 0,
        "wal_bytes": 0,
        "attachment_disk_bytes": 0,
        "attachment_disk_files": 0,
        "total_bytes": 0,
        "last_write_at": 0,
        "cpu_percent": 0.0,
        "mem_bytes": 0,
        "mem_limit": 0,
        "net_rx": 0,
        "net_tx": 0,
        "pids": 0,
        "growth": None,
    }
