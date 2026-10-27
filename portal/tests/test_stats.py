"""Tests for the read-only statistics used by the admin console."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.stats import (
    StatsCollector,
    dir_usage,
    empty_entry,
    human_bytes,
    human_count,
    parse_docker_size,
    parse_pair,
    read_shadow_stats,
)

SCHEMA = """
CREATE TABLE mail_entries (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, imap_uid INTEGER NOT NULL,
    imap_folder TEXT NOT NULL, subject TEXT, sender TEXT, date_utc TEXT
);
CREATE TABLE mail_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT, filename TEXT,
    content_type TEXT, size_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE sync_state (
    account_id TEXT NOT NULL, imap_folder TEXT NOT NULL,
    last_uid INTEGER NOT NULL DEFAULT 0, uid_validity INTEGER NOT NULL DEFAULT 0,
    cjk_backfill_done INTEGER NOT NULL DEFAULT 0, cjk_cursor_uid INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, imap_folder)
);
CREATE TABLE sync_skipped (
    account_id TEXT NOT NULL, imap_folder TEXT NOT NULL, imap_uid INTEGER NOT NULL,
    reason TEXT, attempts INTEGER, first_seen TEXT, last_seen TEXT,
    PRIMARY KEY (account_id, imap_folder, imap_uid)
);
"""


def make_shadow(path: Path, folders: dict[str, int], *, synced: dict[str, int] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.executescript(SCHEMA)
    for folder, count in folders.items():
        for uid in range(1, count + 1):
            connection.execute(
                "INSERT INTO mail_entries (id, account_id, imap_uid, imap_folder, date_utc)"
                " VALUES (?, 'acc', ?, ?, ?)",
                (f"{folder}-{uid}", uid, folder, f"2026-01-{uid % 28 + 1:02d}T10:00:00Z"),
            )
            if uid % 5 == 0:
                connection.execute(
                    "INSERT INTO mail_attachments (entry_id, filename, size_bytes)"
                    " VALUES (?, 'a.pdf', 1000)",
                    (f"{folder}-{uid}",),
                )
        last_uid = (synced or {}).get(folder, 0)
        connection.execute(
            "INSERT INTO sync_state (account_id, imap_folder, last_uid, cjk_backfill_done)"
            " VALUES ('acc', ?, ?, 1)",
            (folder, last_uid),
        )
    connection.commit()
    connection.close()


class FormatTest(unittest.TestCase):
    def test_human_bytes(self) -> None:
        self.assertEqual(human_bytes(None), "—")
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(1536), "1.5 KB")
        self.assertEqual(human_bytes(25 * 1024**3), "25.0 GB")

    def test_human_count(self) -> None:
        self.assertEqual(human_count(999), "999")
        self.assertEqual(human_count(12345), "1.2 万")
        self.assertEqual(human_count(250_000_000), "2.50 亿")

    def test_docker_sizes(self) -> None:
        self.assertEqual(parse_docker_size("12.5MiB"), int(12.5 * 1024**2))
        self.assertEqual(parse_docker_size("647kB"), 647000)
        self.assertEqual(parse_docker_size("0B"), 0)
        self.assertEqual(parse_docker_size("garbage"), 0)
        self.assertEqual(parse_pair("1.0MiB / 2.0MiB"), (1024**2, 2 * 1024**2))
        self.assertEqual(parse_pair(""), (0, 0))


class ShadowStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "mail.db"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_counts_folders_and_pending(self) -> None:
        make_shadow(self.db_path, {"INBOX": 6, "已发送": 3}, synced={"INBOX": 4, "已发送": 3})
        result = read_shadow_stats(self.db_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["messages"], 9)
        self.assertEqual([f["name"] for f in result["folders"]], ["INBOX", "已发送"])
        inbox = result["folders"][0]
        self.assertEqual(inbox["messages"], 6)
        self.assertEqual(inbox["newest_uid"], 6)
        self.assertEqual(inbox["synced_uid"], 4)
        self.assertEqual(inbox["pending"], 2)
        self.assertEqual(result["folders"][1]["pending"], 0)
        self.assertEqual(result["newest_mail"], "2026-01-07T10:00:00Z")
        self.assertEqual(result["attachments"], 1)

    def test_missing_database_is_reported(self) -> None:
        result = read_shadow_stats(self.root / "nope.db")
        self.assertFalse(result["ok"])
        self.assertIn("不存在", result["error"])
        self.assertEqual(result["folders"], [])

    def test_empty_database(self) -> None:
        make_shadow(self.db_path, {})
        result = read_shadow_stats(self.db_path)
        self.assertTrue(result["ok"])
        self.assertEqual(result["messages"], 0)
        self.assertEqual(result["newest_mail"], "")

    def test_dir_usage(self) -> None:
        target = self.root / "attachments" / "2026"
        target.mkdir(parents=True)
        (target / "a.bin").write_bytes(b"x" * 10)
        (target / "b.bin").write_bytes(b"y" * 5)
        usage = dir_usage(self.root / "attachments")
        self.assertEqual(usage["files"], 2)
        self.assertEqual(usage["bytes"], 15)
        self.assertFalse(usage["truncated"])

    def test_dir_usage_respects_entry_limit(self) -> None:
        target = self.root / "many"
        target.mkdir()
        for index in range(5):
            (target / f"{index}.bin").write_bytes(b"x")
        usage = dir_usage(target, max_entries=2)
        self.assertTrue(usage["truncated"])


class _FakeDB:
    def __init__(self, instances: list[dict]) -> None:
        self.instances = instances
        self.samples: list[tuple[str, int, int, int]] = []

    def list_instances(self) -> list[dict]:
        return list(self.instances)

    def record_instance_sample(self, instance_id, *, at, messages, bytes_) -> None:
        self.samples.append((instance_id, at, messages, bytes_))

    def growth_since(self, instance_id, *, since):
        return None

    def prune_samples(self, *, before) -> None:
        pass


class _FakeDocker:
    def __init__(self) -> None:
        self.containers = [
            {"Names": "msm-aaaa", "State": "running", "Status": "Up 2 hours", "Image": "msm:test"}
        ]
        self.stats = {
            "msm-aaaa": {
                "CPUPerc": "1.50%",
                "MemUsage": "32MiB / 128MiB",
                "NetIO": "1.0MB / 2.0MB",
                "PIDs": "7",
            }
        }

    def list_managed_containers(self):
        return self.containers

    def container_stats(self):
        return self.stats

    def logs(self, name, *, tail=60):
        return (
            'time=2026-10-27T10:00:00Z level=INFO msg="sync ok"\n'
            'time=2026-10-27T10:05:00Z level=ERROR msg="sync failed" err="AUTHENTICATIONFAILED"\n'
            'time=2026-10-27T10:06:00Z level=INFO msg="retry later"'
        )


def _instance(root: Path, instance_id: str, volume: str, folders: str | None = None) -> dict:
    return {
        "id": instance_id,
        "email": f"{instance_id}@example.com",
        "container_name": f"msm-{instance_id}",
        "status": "running",
        "data_volume": volume,
        "config_volume": f"cfg-{volume}",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "provider_name": "自定义 IMAP",
        "created_at": 1_700_000_000,
        "folders": folders,
    }


def _collector(root: Path, db, docker) -> StatsCollector:
    config = SimpleNamespace(
        docker_volume_root=str(root),
        stats_interval_seconds=300,
        sync_folders=("INBOX",),
    )
    return StatsCollector(config, db, docker)


class CollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_collect_reads_volume_and_container(self) -> None:
        volume = "msm-data-aaaa"
        data = self.root / volume / "_data"
        make_shadow(data / "mail.db", {"INBOX": 4}, synced={"INBOX": 4})
        (data / "attachments").mkdir()
        (data / "attachments" / "a.bin").write_bytes(b"z" * 2048)

        instance = _instance(self.root, "aaaa", volume, '["INBOX","已发送"]')
        db = _FakeDB([instance])
        collector = _collector(self.root, db, _FakeDocker())
        entry = collector.collect_instance(
            instance, {"State": "running", "Status": "Up 1 hour", "Image": "msm:test"},
            {"CPUPerc": "2.00%", "MemUsage": "16MiB / 128MiB", "NetIO": "1KB / 2KB", "PIDs": "4"},
        )
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["messages"], 4)
        self.assertEqual(entry["folder_count"], 1)
        self.assertEqual(entry["pending"], 0)
        self.assertGreaterEqual(entry["db_bytes"], 0)
        self.assertGreaterEqual(entry["attachment_disk_bytes"], 2048)
        self.assertGreaterEqual(entry["total_bytes"], entry["attachment_disk_bytes"])
        self.assertEqual(entry["container_state"], "running")
        self.assertAlmostEqual(entry["cpu_percent"], 2.0)
        self.assertEqual(entry["mem_bytes"], 16 * 1024**2)
        self.assertEqual(entry["net_rx"], 1000)
        self.assertEqual(entry["pids"], 4)
        self.assertEqual(entry["configured"], {"source": "instance", "all": False, "names": ["INBOX", "已发送"]})
        self.assertIn("AUTHENTICATIONFAILED", entry["log_error"])
        self.assertTrue(entry["log_error"].startswith("msg="))

    def test_missing_volume_is_not_fatal(self) -> None:
        instance = _instance(self.root, "bbbb", "msm-data-bbbb")
        collector = _collector(self.root, _FakeDB([instance]), _FakeDocker())
        entry = collector.collect_instance(instance, None, None)
        self.assertFalse(entry["ok"])
        self.assertIn("数据卷", entry["error"])
        self.assertEqual(entry["messages"], 0)
        self.assertEqual(entry["container_state"], "missing")

    def test_collect_once_records_samples_and_snapshot(self) -> None:
        volume = "msm-data-aaaa"
        make_shadow(self.root / volume / "_data" / "mail.db", {"INBOX": 3})
        instance = _instance(self.root, "aaaa", volume)
        db = _FakeDB([instance])
        collector = _collector(self.root, db, _FakeDocker())
        snapshot = collector.collect_once()
        self.assertIn("aaaa", snapshot["instances"])
        self.assertGreater(snapshot["generated_at"], 0)
        self.assertEqual(len(db.samples), 1)
        self.assertEqual(db.samples[0][2], 3)
        self.assertEqual(collector.snapshot()["instances"]["aaaa"]["messages"], 3)

    def test_removed_instances_are_skipped(self) -> None:
        instance = _instance(self.root, "cccc", "msm-data-cccc")
        instance["status"] = "removed"
        collector = _collector(self.root, _FakeDB([instance]), _FakeDocker())
        self.assertEqual(collector.collect_once()["instances"], {})

    def test_docker_failure_is_contained(self) -> None:
        class Boom:
            def list_managed_containers(self):
                raise RuntimeError("no docker")

            def container_stats(self):
                raise RuntimeError("no docker")

        instance = _instance(self.root, "aaaa", "msm-data-aaaa")
        collector = _collector(self.root, _FakeDB([instance]), Boom())
        snapshot = collector.collect_once()
        self.assertIn("docker", snapshot["error"].lower())
        self.assertFalse(snapshot["instances"]["aaaa"]["ok"])


class EmptyEntryTest(unittest.TestCase):
    def test_folder_selection_states(self) -> None:
        base = {
            "id": "d" * 16,
            "email": "a@b.c",
            "container_name": "msm-d",
            "status": "running",
            "folders": None,
        }
        self.assertEqual(empty_entry(base)["configured"]["source"], "deployment")
        self.assertEqual(empty_entry({**base, "folders": "[]"})["configured"], {
            "source": "instance", "all": True, "names": []
        })
        self.assertEqual(
            empty_entry({**base, "folders": '["INBOX"]'})["configured"]["names"], ["INBOX"]
        )


if __name__ == "__main__":
    unittest.main()
