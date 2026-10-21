from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.config import Config
from app.db import Database
from app.docker_cli import DockerError
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

    def write_config_volume(self, volume: str, image: str, content: str) -> None:
        self.configs[volume] = content

    def run_container(self, spec):
        self.runs.append(spec)
        self.containers[spec.name] = {
            "State": {"Running": True, "Status": "running"},
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
        self.assertIsNotNone(self.db.get_instance_by_token_hash(tokens.hash_mcp_token(first.token)))

        second = provisioner.provision("user@gmail.com", "abcd efgh")
        self.assertTrue(second.reused)
        self.assertFalse(second.rotated)
        self.assertEqual(len(docker.runs), 1)
        self.assertEqual(second.token, first.token)
        self.assertEqual(second.instance["id"], first.instance["id"])

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


if __name__ == "__main__":
    unittest.main()
