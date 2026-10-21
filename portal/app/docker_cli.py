"""Thin wrapper around the Docker CLI.

The portal intentionally uses the ``docker`` binary instead of a Python Docker
SDK: the deployment image only needs ``docker-cli``, and every operation stays
visible in the host's shell history/audit tooling.  All arguments are passed as
an argv list (never through a shell), and the mailbox password is streamed to a
helper container on stdin rather than appearing in the process list.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


class DockerError(RuntimeError):
    def __init__(self, operation: str, message: str) -> None:
        super().__init__(f"{operation}: {message}")
        self.operation = operation
        self.message = message


@dataclass(frozen=True)
class ContainerSpec:
    name: str
    instance_id: str
    network: str
    image: str
    config_volume: str
    data_volume: str
    memory: str
    cpus: str
    pids: int
    container_port: int = 8080
    publish_loopback: bool = False


class DockerClient:
    def __init__(self, binary: str = "docker") -> None:
        self.binary = binary

    # -- low level -------------------------------------------------------
    def _run(
        self,
        operation: str,
        args: list[str],
        *,
        timeout: float = 60,
        input_data: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.binary, *args]
        log.debug("docker %s", " ".join(args))
        try:
            result = subprocess.run(
                command,
                input=input_data,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DockerError(
                operation,
                f"找不到 docker 命令（{self.binary}）。请安装 Docker CLI 或设置 MSM_DOCKER_BIN。",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError(operation, f"docker 命令超时（{timeout:.0f}s）") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            detail = " ".join(detail.split())[-600:]
            raise DockerError(operation, detail or f"docker 退出码 {result.returncode}")
        return result

    # -- daemon / images / networks -------------------------------------
    def version(self) -> str:
        result = self._run("docker version", ["version", "--format", "{{.Server.Version}}"], timeout=15)
        return result.stdout.strip()

    def network_exists(self, name: str) -> bool:
        result = self._run(
            "inspect network",
            ["network", "inspect", name],
            timeout=15,
            check=False,
        )
        return result.returncode == 0

    def ensure_network(self, name: str) -> None:
        if self.network_exists(name):
            return
        log.info("creating docker network %s", name)
        result = self._run(
            "create network",
            ["network", "create", "--driver", "bridge", name],
            timeout=60,
            check=False,
        )
        if result.returncode != 0 and not self.network_exists(name):
            detail = (result.stderr or result.stdout or "").strip()[-600:]
            raise DockerError("create network", detail or "docker network create failed")

    def image_exists(self, image: str) -> bool:
        result = self._run(
            "inspect image",
            ["image", "inspect", image],
            timeout=15,
            check=False,
        )
        return result.returncode == 0

    def pull_image(self, image: str) -> None:
        log.info("pulling image %s (this can take a while)", image)
        self._run("pull image", ["pull", image], timeout=900)

    def ensure_image(self, image: str) -> None:
        if not self.image_exists(image):
            self.pull_image(image)

    # -- config volume ---------------------------------------------------
    def write_config_volume(self, volume: str, image: str, content: str) -> None:
        """Write config.yaml into a named volume without a host bind mount.

        A throwaway container of the same image receives the file on stdin.  It
        is not attached to any network and exits immediately.
        """
        script = "umask 077 && mkdir -p /config && cat > /config/config.yaml && chmod 600 /config/config.yaml"
        self._run(
            "write config volume",
            [
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--entrypoint",
                "/bin/sh",
                "-v",
                f"{volume}:/config",
                image,
                "-c",
                script,
            ],
            timeout=900,
            input_data=content,
        )

    # -- containers ------------------------------------------------------
    def remove_container(self, name: str) -> None:
        result = self._run(
            "remove container",
            ["rm", "-f", name],
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").lower()
            if "no such container" not in message and "no such object" not in message:
                raise DockerError("remove container", (result.stderr or result.stdout).strip()[-600:])

    def run_container(self, spec: ContainerSpec) -> str:
        args = [
            "run",
            "-d",
            "--name",
            spec.name,
            "--hostname",
            spec.name,
            "--restart",
            "unless-stopped",
        ]
        if spec.network:
            args.extend(["--network", spec.network, "--network-alias", spec.name])
        args.extend(
            [
                "--label",
                "msm.managed=true",
                "--label",
                f"msm.instance={spec.instance_id}",
                "--memory",
                spec.memory,
                "--memory-swap",
                spec.memory,
                "--cpus",
                spec.cpus,
                "--pids-limit",
                str(spec.pids),
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=64m",
                "--log-driver",
                "json-file",
                "--log-opt",
                "max-size=10m",
                "--log-opt",
                "max-file=2",
                "--stop-timeout",
                "10",
            ]
        )
        if spec.publish_loopback:
            # Random host port bound to loopback only.  The portal reads the
            # assigned port from docker inspect and routes to it.
            args.extend(["-p", f"127.0.0.1::{spec.container_port}"])
        args.extend(
            [
                "-v",
                f"{spec.config_volume}:/config:ro",
                "-v",
                f"{spec.data_volume}:/data",
                spec.image,
            ]
        )
        result = self._run("run container", args, timeout=300)
        container_id = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if not container_id:
            raise DockerError("run container", "docker run 未返回容器 ID")
        return container_id

    def start_container(self, name: str) -> None:
        self._run("start container", ["start", name], timeout=60)

    def inspect_container(self, name: str) -> dict[str, Any] | None:
        result = self._run(
            "inspect container",
            ["inspect", "--type", "container", name],
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, list) or not payload:
            return None
        first = payload[0]
        return first if isinstance(first, dict) else None

    def container_running(self, name: str) -> bool:
        info = self.inspect_container(name)
        if not info:
            return False
        state = info.get("State") or {}
        return bool(state.get("Running"))

    def container_ip(self, name: str, network: str) -> str | None:
        info = self.inspect_container(name)
        if not info:
            return None
        networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
        entry = networks.get(network)
        if isinstance(entry, dict) and entry.get("IPAddress"):
            return str(entry["IPAddress"])
        for value in networks.values():
            if isinstance(value, dict) and value.get("IPAddress"):
                return str(value["IPAddress"])
        return None

    def container_host_port(self, name: str, container_port: int) -> int | None:
        info = self.inspect_container(name)
        if not info:
            return None
        ports = (info.get("NetworkSettings") or {}).get("Ports") or {}
        bindings = ports.get(f"{int(container_port)}/tcp")
        if isinstance(bindings, list):
            for binding in bindings:
                if isinstance(binding, dict) and binding.get("HostPort"):
                    try:
                        return int(binding["HostPort"])
                    except (TypeError, ValueError):
                        return None
        return None

    def logs(self, name: str, *, tail: int = 60) -> str:
        result = self._run(
            "container logs",
            ["logs", "--tail", str(tail), name],
            timeout=20,
            check=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return output.strip()[-4000:]

    def remove_volume(self, name: str) -> None:
        result = self._run("remove volume", ["volume", "rm", name], timeout=30, check=False)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").lower()
            if "no such volume" not in message and "no such object" not in message:
                raise DockerError("remove volume", (result.stderr or result.stdout).strip()[-600:])

    def list_managed_containers(self) -> list[dict[str, Any]]:
        result = self._run(
            "list containers",
            ["ps", "-a", "--filter", "label=msm.managed=true", "--format", "{{json .}}"],
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise DockerError("list containers", (result.stderr or "").strip()[-600:])
        containers: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return containers
