from __future__ import annotations

import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from app.config import Config
from app.db import Database
from app.docker_cli import DockerError, container_spec_hash
from app.imap_check import ImapAuthError
from app.provisioner import Provisioner, ProvisionError
from app.security import TokenService


class FakeDocker:
    def __init__(self) -> None:
        self.containers: dict[str, dict] = {}
        self.configs: dict[str, str] = {}
        self.runs: list = []
        self.removed: list[str] = []
        self.volumes_removed: list[str] = []
        self.image_id_value = "sha256:test-image"

    def write_config_volume(self, volume: str, image: str, content: str) -> None:
        self.configs[volume] = content

    def read_config_volume(self, volume: str, image: str) -> str:
        return self.configs[volume]

    def image_id(self, image: str) -> str:
        return self.image_id_value

    def run_container(self, spec):
        self.runs.append(spec)
        self.containers[spec.name] = {
            "Id": f"cid-{len(self.runs)}",
            "Image": self.image_id_value,
            "State": {"Running": True, "Status": "running"},
            "Config": {"Labels": {"msm.instance": spec.instance_id, "msm.spec": container_spec_hash(spec)}},
            "NetworkSettings": {"Networks": {spec.network: {"IPAddress": "127.0.0.1"}}},
        }
        return f"cid-{len(self.runs)}"

    def inspect_container(self, name: str):
        return self.containers.get(name)

    def start_container(self, name: str) -> None:
        if name not in self.containers:
            raise DockerError("start", "missing")
        self.containers[name]["State"] = {"Running": True, "Status": "running"}

    def remove_container(self, name: str) -> None:
        self.removed.append(name)
        self.containers.pop(name, None)

    def container_ip(self, name: str, network: str) -> str | None:
        return "127.0.0.1"

    def container_host_port(self, name: str, container_port: int) -> int | None:
        return 18080

    def logs(self, name: str, tail: int = 60) -> str:
        return "fake container log"

    def remove_volume(self, name: str) -> None:
        self.volumes_removed.append(name)


class ProvisionerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "control.db")
        self.sleep_patch = mock.patch("app.provisioner.time.sleep", return_value=None)
        self.sleep_patch.start()
        self.checker_calls: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self.sleep_patch.stop()
        self.db.close()
        self.tmp.cleanup()

    def _checker(self, settings, password, timeout=15):
        self.checker_calls.append((settings.host, password))
        if password == "bad-password":
            raise ImapAuthError("IMAP 登录失败")

    def _provisioner(self, **overrides):
        env = {
            "MSM_DATA_DIR": self.tmp.name,
            "MSM_PUBLIC_URL": "http://localhost:8080",
            "MSM_SECURE_COOKIES": "0",
            "MSM_VERIFY_BACKEND": "0",
            "MSM_LISTEN_PORT": "0",
            "MSM_BACKEND_PORT": "1",
            "MSM_MAX_INSTANCES": "5",
            "MSM_DOCKER_NETWORK": "msm-test",
            "MSM_IMAGE": "test/image:latest",
        }
        env.update({key: str(value) for key, value in overrides.items()})
        env_patch = mock.patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        config = Config.from_env()
        docker = FakeDocker()
        tokens = TokenService(config.master_key)
        provisioner = Provisioner(config, self.db, docker, tokens, imap_checker=self._checker)
        return config, docker, tokens, provisioner

    def test_first_provision_and_idempotent_reuse(self) -> None:
        _, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision("User@Gmail.com", "abcd efgh", invite_id="inv")
        self.assertFalse(first.reused)
        self.assertEqual(first.instance["status"], "running")
        self.assertEqual(first.instance["host_port"], 18080)
        self.assertEqual(len(docker.runs), 1)
        self.assertIn(first.token, docker.configs[first.instance["config_volume"]])
        self.assertIn('password: "abcdefgh"', docker.configs[first.instance["config_volume"]])
        self.assertIn("sync_interval_min: 30", docker.configs[first.instance["config_volume"]])
        self.assertIn("sync_jitter_sec: 1800", docker.configs[first.instance["config_volume"]])
        self.assertIsNotNone(self.db.get_instance_by_token_hash(tokens.hash_mcp_token(first.token)))
        self.assertEqual(first.instance["owner_invite"], "inv")

        second = provisioner.provision("user@gmail.com", "abcd efgh")
        self.assertTrue(second.reused)
        self.assertFalse(second.rotated)
        self.assertEqual(len(docker.runs), 1)
        self.assertEqual(second.token, first.token)
        self.assertEqual(second.instance["id"], first.instance["id"])

    def test_folder_selection_is_rendered_and_reused(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision(
            "user@gmail.com",
            "abcd efgh",
            folders=["INBOX", "已发送", "主送王星"],
            invite_id="inv",
        )
        cfg = docker.configs[first.instance["config_volume"]]
        self.assertIn('folders: ["INBOX", "已发送", "主送王星"]', cfg)
        self.assertIn('idle_folders: ["INBOX"]', cfg)
        self.assertEqual(
            first.instance["folders"], '["INBOX", "已发送", "主送王星"]'
        )

        second = provisioner.provision(
            "user@gmail.com",
            "abcd efgh",
            folders=["INBOX", "已发送", "主送王星"],
        )
        self.assertEqual(len(docker.runs), 1)
        self.assertEqual(second.token, first.token)

    def test_changing_folders_keeps_token_and_recreates(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision(
            "user@gmail.com", "abcd efgh", folders=["INBOX"], invite_id="inv"
        )
        before = docker.configs[first.instance["config_volume"]]
        self.assertIn('folders: ["INBOX"]', before)
        self.assertEqual(len(docker.runs), 1)

        second = provisioner.provision(
            "user@gmail.com", "abcd efgh", folders=["INBOX", "Archive"]
        )
        self.assertTrue(second.reused)
        self.assertFalse(second.rotated)
        self.assertEqual(second.token, first.token)
        self.assertEqual(len(docker.runs), 2)
        after = docker.configs[first.instance["config_volume"]]
        self.assertIn('folders: ["INBOX", "Archive"]', after)
        self.assertNotEqual(before, after)
        self.assertEqual(
            self.db.get_instance(first.instance["id"])["folders"],
            '["INBOX", "Archive"]',
        )

    def test_same_folders_in_different_order_do_not_recreate(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision(
            "user@gmail.com", "abcd efgh", folders=["INBOX", "Archive"], invite_id="inv"
        )
        self.assertEqual(len(docker.runs), 1)

        second = provisioner.provision(
            "user@gmail.com", "abcd efgh", folders=["Archive", "INBOX"]
        )
        self.assertEqual(second.token, first.token)
        # Same set, different order: no container rebuild, no token change.
        self.assertEqual(len(docker.runs), 1)

    def test_folder_selection_all_folders_removes_restriction(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        result = provisioner.provision(
            "user@gmail.com", "abcd efgh", folders=[], invite_id="inv"
        )
        cfg = docker.configs[result.instance["config_volume"]]
        self.assertNotIn("folders:", cfg)
        self.assertEqual(result.instance["folders"], "[]")

    def test_no_folder_picker_keeps_deployment_default(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        result = provisioner.provision("user@gmail.com", "abcd efgh", invite_id="inv")
        cfg = docker.configs[result.instance["config_volume"]]
        self.assertIn('folders: ["INBOX"]', cfg)
        self.assertIsNone(result.instance["folders"])

    def test_folder_selection_rejects_oversized_name(self) -> None:
        _, _, _, provisioner = self._provisioner()
        with self.assertRaises(ProvisionError) as ctx:
            provisioner.provision(
                "user@gmail.com", "abcd efgh", folders=["x" * 300]
            )
        self.assertEqual(ctx.exception.field, "folders")

    def test_list_mailbox_folders_returns_imap_list(self) -> None:
        _, _, _, provisioner = self._provisioner()
        provisioner._list_folders = lambda settings, password: ["INBOX", "已发送"]
        self.assertEqual(
            provisioner.list_mailbox_folders("user@gmail.com", "abcd efgh"),
            ["INBOX", "已发送"],
        )

    def test_foreign_invite_with_bad_password_does_not_transfer_owner(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "old-password", invite_id="inv-a")
        with self.assertRaises(ProvisionError) as ctx:
            provisioner.provision("user@gmail.com", "bad-password", invite_id="inv-b")
        self.assertEqual(ctx.exception.code, "imap_auth")
        # The failed attempt must not move the status page to the attacker.
        self.assertEqual(
            self.db.get_instance(first.instance["id"])["owner_invite"], "inv-a"
        )
        self.assertEqual(len(docker.runs), 1)

    def test_foreign_invite_with_valid_credentials_transfers_owner(self) -> None:
        _, _, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "old-password", invite_id="inv-a")
        second = provisioner.provision(
            "user@gmail.com", "new-password", invite_id="inv-b"
        )
        self.assertEqual(second.instance["id"], first.instance["id"])
        self.assertEqual(second.instance["owner_invite"], "inv-b")
        self.assertTrue(second.rotated)

    def test_same_credentials_from_another_invite_keeps_token_and_transfers(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password", invite_id="inv-a")
        second = provisioner.provision("user@gmail.com", "password", invite_id="inv-b")
        self.assertEqual(second.token, first.token)
        self.assertEqual(second.instance["owner_invite"], "inv-b")
        self.assertEqual(len(docker.runs), 1)

    def test_password_rotation_invalidates_old_token(self) -> None:
        _, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "old-password")
        old_hash = tokens.hash_mcp_token(first.token)
        self.assertIsNotNone(self.db.get_instance_by_token_hash(old_hash))

        second = provisioner.provision("user@gmail.com", "new-password")
        self.assertFalse(second.reused)
        self.assertTrue(second.rotated)
        self.assertNotEqual(second.token, first.token)
        self.assertEqual(len(docker.runs), 2)
        self.assertIsNone(self.db.get_instance_by_token_hash(old_hash))
        self.assertIsNotNone(
            self.db.get_instance_by_token_hash(tokens.hash_mcp_token(second.token))
        )
        self.assertIn('password: "new-password"', docker.configs[second.instance["config_volume"]])

    def test_bad_new_password_does_not_touch_existing(self) -> None:
        _, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "old-password")
        with self.assertRaises(ProvisionError) as ctx:
            provisioner.provision("user@gmail.com", "bad-password")
        self.assertEqual(ctx.exception.code, "imap_auth")
        self.assertEqual(len(docker.runs), 1)
        self.assertEqual(self.db.get_instance(first.instance["id"])["status"], "running")
        self.assertIsNotNone(
            self.db.get_instance_by_token_hash(tokens.hash_mcp_token(first.token))
        )

    def test_bad_password_creates_no_instance(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        with self.assertRaises(ProvisionError):
            provisioner.provision("user@gmail.com", "bad-password")
        self.assertEqual(self.db.count_active_instances(), 0)
        self.assertEqual(len(docker.runs), 0)

    def test_one_invite_can_create_multiple_mailboxes(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        invite_id = self.db.create_invite("hash-invite", max_uses=1)
        first = provisioner.provision("one@gmail.com", "password", invite_id=invite_id)
        second = provisioner.provision("two@gmail.com", "password", invite_id=invite_id)
        self.assertNotEqual(first.instance["id"], second.instance["id"])
        self.assertEqual(first.instance["owner_invite"], invite_id)
        self.assertEqual(second.instance["owner_invite"], invite_id)
        self.assertEqual(len(docker.runs), 2)

    def test_capacity_limit(self) -> None:
        _, _, _, provisioner = self._provisioner(MSM_MAX_INSTANCES=1)
        provisioner.provision("one@gmail.com", "password")
        with self.assertRaises(ProvisionError) as ctx:
            provisioner.provision("two@gmail.com", "password")
        self.assertEqual(ctx.exception.code, "capacity")

    def test_removed_instance_can_be_reactivated(self) -> None:
        _, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        old_hash = tokens.hash_mcp_token(first.token)
        provisioner.remove_instance(first.instance["id"], purge_data=False)
        self.assertEqual(self.db.get_instance(first.instance["id"])["status"], "removed")

        again = provisioner.provision("user@gmail.com", "password")
        self.assertTrue(again.reused)
        self.assertEqual(again.instance["status"], "running")
        self.assertEqual(again.instance["id"], first.instance["id"])
        self.assertEqual(len(docker.runs), 2)
        # Recreating from the removed state rotates the salt, so the old token
        # cannot be resurrected by re-submitting the same credentials.
        self.assertNotEqual(again.token, first.token)
        self.assertIsNone(self.db.get_instance_by_token_hash(old_hash))
        self.assertIsNotNone(
            self.db.get_instance_by_token_hash(tokens.hash_mcp_token(again.token))
        )

    def test_removed_instance_with_new_folders_still_rotates_token(self) -> None:
        # The folder-change branch keeps the token on purpose.  Recovery from
        # removed/failed must run first, otherwise re-submitting the old
        # credentials with a different folder selection resurrects the token
        # that the delete was supposed to revoke.
        _, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision(
            "user@gmail.com", "password", folders=["INBOX"]
        )
        old_hash = tokens.hash_mcp_token(first.token)
        provisioner.remove_instance(first.instance["id"], purge_data=False)

        again = provisioner.provision(
            "user@gmail.com", "password", folders=["Archive"]
        )
        self.assertEqual(again.instance["status"], "running")
        self.assertNotEqual(again.token, first.token)
        self.assertIsNone(self.db.get_instance_by_token_hash(old_hash))
        self.assertEqual(len(docker.runs), 2)
        self.assertEqual(again.instance["folders"], '["Archive"]')
        self.assertIn(
            'folders: ["Archive"]',
            docker.configs[first.instance["config_volume"]],
        )

    def test_stale_provisioning_is_retried(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        with self.db._lock:  # noqa: SLF001 - simulate a portal crash mid-provision
            self.db._conn.execute(  # noqa: SLF001
                "UPDATE instances SET status = 'provisioning', updated_at = ? WHERE id = ?",
                (int(time.time()) - 1000, first.instance["id"]),
            )
        again = provisioner.provision("user@gmail.com", "password")
        self.assertTrue(again.reused)
        self.assertEqual(again.instance["status"], "running")
        self.assertEqual(len(docker.runs), 2)

    def test_private_imap_target_rejected(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        with self.assertRaises(ProvisionError) as ctx:
            provisioner.provision(
                "user@corp.example",
                "password",
                host="127.0.0.1",
                port="143",
                tls_mode="starttls",
            )
        self.assertEqual(ctx.exception.code, "imap_target")
        self.assertEqual(len(docker.runs), 0)
        self.assertEqual(self.checker_calls, [])

    def test_nonstandard_imap_port_rejected_by_default(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        with self.assertRaises(ProvisionError) as ctx:
            provisioner.provision(
                "user@corp.example",
                "password",
                host="mail.corp.example",
                port="1993",
                tls_mode="starttls",
            )
        self.assertEqual(ctx.exception.code, "imap_target")
        self.assertEqual(len(docker.runs), 0)

    def test_insecure_imap_rejected(self) -> None:
        _, _, _, provisioner = self._provisioner()
        with self.assertRaises(ProvisionError) as ctx:
            provisioner.provision(
                "user@corp.example",
                "password",
                host="mail.corp.example",
                tls_mode="none",
            )
        self.assertEqual(ctx.exception.code, "insecure_imap")


    def test_running_container_on_wrong_network_is_recreated(self) -> None:
        _, docker, _, provisioner = self._provisioner(MSM_BACKEND_MODE="network")
        first = provisioner.provision("user@gmail.com", "password")
        name = first.instance["container_name"]
        # Simulate a container that was created before the operator switched
        # from port mode to network mode: it is running but not attached to
        # msm-net, so the gateway's DNS lookup would fail forever.
        docker.containers[name]["NetworkSettings"] = {"Networks": {}}

        again = provisioner.provision("user@gmail.com", "password")
        self.assertTrue(again.reused)
        self.assertEqual(len(docker.runs), 2)
        self.assertIn("msm-test", docker.containers[name]["NetworkSettings"]["Networks"])

    def test_running_container_with_foreign_instance_label_is_recreated(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        name = first.instance["container_name"]
        docker.containers[name]["Config"]["Labels"]["msm.instance"] = "f" * 16

        again = provisioner.provision("user@gmail.com", "password")
        self.assertTrue(again.reused)
        self.assertEqual(len(docker.runs), 2)
        self.assertEqual(
            docker.containers[name]["Config"]["Labels"]["msm.instance"],
            first.instance["id"],
        )


    def test_reconcile_dry_run_does_not_mutate(self) -> None:
        _, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        volume = first.instance["config_volume"]
        docker.configs[volume] = docker.configs[volume].replace(
            'http_bearer_token: "' + first.token + '"',
            'http_bearer_token: "other-token"',
        )
        docker.image_id_value = "sha256:new-image"
        docker.containers[first.instance["container_name"]]["Image"] = "sha256:old-image"

        result = provisioner.reconcile_instance(first.instance["id"], dry_run=True)
        self.assertEqual(result.action, "would_recreate")
        self.assertEqual(len(docker.runs), 1)
        self.assertIsNotNone(
            self.db.get_instance_by_token_hash(tokens.hash_mcp_token(first.token))
        )

    def test_reconcile_recreates_on_new_image_and_preserves_token(self) -> None:
        _, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        volume = first.instance["config_volume"]
        name = first.instance["container_name"]
        # Simulate a config written by the previous portal version: no folders.
        docker.configs[volume] = "\n".join(
            line for line in docker.configs[volume].splitlines()
            if not line.strip().startswith(("folders:", "idle_folders:"))
        ) + "\n"
        docker.image_id_value = "sha256:new-image"
        docker.containers[name]["Image"] = "sha256:old-image"

        result = provisioner.reconcile_instance(first.instance["id"], update_config=True)
        self.assertEqual(result.action, "recreated")
        self.assertEqual(len(docker.runs), 2)
        self.assertIn('folders: ["INBOX"]', docker.configs[volume])
        self.assertIn(first.token, docker.configs[volume])
        refreshed = self.db.get_instance(first.instance["id"])
        self.assertEqual(refreshed["status"], "running")
        self.assertEqual(refreshed["token_hash"], tokens.hash_mcp_token(first.token))

    def test_reconcile_image_only_keeps_existing_config(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        volume = first.instance["config_volume"]
        name = first.instance["container_name"]
        docker.configs[volume] = "\n".join(
            line for line in docker.configs[volume].splitlines()
            if not line.strip().startswith(("folders:", "idle_folders:"))
        ) + "\n"
        old_config = docker.configs[volume]
        docker.image_id_value = "sha256:new-image"
        docker.containers[name]["Image"] = "sha256:old-image"

        result = provisioner.reconcile_instance(first.instance["id"])
        self.assertEqual(result.action, "recreated")
        self.assertEqual(docker.configs[volume], old_config)
        self.assertIn(first.token, docker.configs[volume])

    def test_reconcile_recreates_when_container_memory_changes(self) -> None:
        config, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        self.assertEqual(first.instance["status"], "running")

        smaller = replace(config, container_memory="96m")
        provisioner2 = Provisioner(smaller, self.db, docker, tokens, imap_checker=self._checker)
        result = provisioner2.reconcile_instance(first.instance["id"])
        self.assertEqual(result.action, "recreated")
        self.assertEqual(len(docker.runs), 2)
        self.assertEqual(docker.runs[-1].memory, "96m")

    def test_reconcile_skips_when_image_and_config_match(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        self.assertEqual(first.instance["status"], "running")
        result = provisioner.reconcile_instance(first.instance["id"])
        self.assertEqual(result.action, "skipped")
        self.assertEqual(len(docker.runs), 1)

    def test_reconcile_recreates_missing_container(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        name = first.instance["container_name"]
        docker.containers.pop(name)
        docker.image_id_value = "sha256:new-image"

        result = provisioner.reconcile_instance(first.instance["id"])
        self.assertEqual(result.action, "recreated")
        self.assertEqual(len(docker.runs), 2)
        self.assertIn(name, docker.containers)

    def test_reconcile_aborts_without_password_and_keeps_container(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        volume = first.instance["config_volume"]
        name = first.instance["container_name"]
        docker.configs[volume] = "\n".join(
            line for line in docker.configs[volume].splitlines()
            if not line.strip().startswith("password:")
        ) + "\n"
        docker.image_id_value = "sha256:new-image"

        with self.assertRaises(ProvisionError) as ctx:
            provisioner.reconcile_instance(first.instance["id"])
        self.assertEqual(ctx.exception.code, "config_unreadable")
        self.assertEqual(len(docker.runs), 1)
        self.assertIn(name, docker.containers)
        self.assertEqual(self.db.get_instance(first.instance["id"])["status"], "running")

    def test_reconcile_write_failure_keeps_old_container_running(self) -> None:
        _, docker, _, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        name = first.instance["container_name"]
        docker.image_id_value = "sha256:new-image"
        docker.containers[name]["Image"] = "sha256:old-image"

        def fail_write(volume, image, content):
            raise DockerError("write config volume", "disk full")

        docker.write_config_volume = fail_write
        with self.assertRaises(ProvisionError):
            provisioner.reconcile_instance(first.instance["id"], update_config=True)
        self.assertEqual(len(docker.runs), 1)
        self.assertIn(name, docker.containers)
        self.assertEqual(self.db.get_instance(first.instance["id"])["status"], "running")

    def test_reconcile_repairs_db_token_hash_from_config(self) -> None:
        _, docker, tokens, provisioner = self._provisioner()
        first = provisioner.provision("user@gmail.com", "password")
        volume = first.instance["config_volume"]
        docker.configs[volume] = docker.configs[volume].replace(
            'http_bearer_token: "' + first.token + '"',
            'http_bearer_token: "some-other-token"',
        )
        docker.image_id_value = "sha256:new-image"
        docker.containers[first.instance["container_name"]]["Image"] = "sha256:old-image"

        result = provisioner.reconcile_instance(first.instance["id"])
        self.assertEqual(result.action, "recreated")
        self.assertEqual(len(docker.runs), 2)
        self.assertIsNotNone(
            self.db.get_instance_by_token_hash(tokens.hash_mcp_token("some-other-token"))
        )
        self.assertIsNone(self.db.get_instance_by_token_hash(tokens.hash_mcp_token(first.token)))


if __name__ == "__main__":
    unittest.main()
