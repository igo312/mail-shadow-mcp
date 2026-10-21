"""Provisioning orchestration: validate IMAP, create volumes, start one container.

Idempotency contract
--------------------
* One instance per mailbox (``email`` is UNIQUE).
* ``HMAC(master, email + NUL + password)`` is the idempotency key.  Submitting
  the same pair returns the same MCP token and reuses the same container/data.
* Submitting the same mailbox with a *different* app password validates the new
  password, rewrites the config, recreates the container and rotates the token.
  The old MCP configuration stops working, which is the desired behaviour after
  a credential rotation.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import logging
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .db import Database, now
from .docker_cli import ContainerSpec, DockerClient, DockerError
from .imap_check import ImapError
from .instance_config import render_instance_config
from .providers import (
    ImapSettings,
    InputError,
    normalize_email,
    normalize_password,
    prepare_password,
    resolve_settings,
)
from .security import TokenService, constant_time_equal

log = logging.getLogger(__name__)


class ProvisionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        field: str = "",
        hint: str = "",
        code: str = "provision_failed",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.hint = hint
        self.code = code


@dataclass(frozen=True)
class ProvisionResult:
    instance: dict[str, Any]
    token: str
    reused: bool
    rotated: bool
    warning: str = ""


class Provisioner:
    def __init__(
        self,
        config: Config,
        db: Database,
        docker: DockerClient,
        tokens: TokenService,
        *,
        imap_checker: Callable[..., None] | None = None,
    ) -> None:
        self.config = config
        self.db = db
        self.docker = docker
        self.tokens = tokens
        self._imap_check = imap_checker
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # -- public API ------------------------------------------------------
    def provision(
        self,
        email_raw: str,
        password_raw: str,
        *,
        host: str = "",
        port: str | int | None = None,
        tls_mode: str = "",
        provider_key: str = "",
        invite_id: str = "",
        ip: str = "",
    ) -> ProvisionResult:
        try:
            email = normalize_email(email_raw)
            password = normalize_password(password_raw)
            settings = resolve_settings(
                email,
                host=host,
                port=port,
                tls_mode=tls_mode,
                provider_key=provider_key,
            )
            password = prepare_password(email, password)
        except InputError as exc:
            raise ProvisionError(str(exc), field=exc.field, code="invalid_input") from exc

        if settings.tls_mode == "none" and not self.config.allow_insecure_imap:
            raise ProvisionError(
                "为保护邮箱授权码，当前部署不允许使用未加密的 IMAP 连接。",
                field="tls_mode",
                code="insecure_imap",
            )
        self._validate_target(settings)

        idem_hash = self.tokens.derive_idempotency_key(email, password)
        # A fresh salt is used for new instances and for credential rotation.
        new_salt = secrets.token_hex(16)

        with self._lock_for(email):
            existing = self.db.get_instance_by_email(email)
            if existing:
                return self._provision_existing(
                    existing,
                    email=email,
                    password=password,
                    settings=settings,
                    idem_hash=idem_hash,
                    new_salt=new_salt,
                    invite_id=invite_id,
                    ip=ip,
                )
            return self._provision_new(
                email=email,
                password=password,
                settings=settings,
                idem_hash=idem_hash,
                token_salt=new_salt,
                invite_id=invite_id,
                ip=ip,
            )

    def remove_instance(self, instance_id: str, *, purge_data: bool = False) -> dict[str, Any]:
        instance = self.db.get_instance(instance_id)
        if not instance:
            raise ProvisionError("实例不存在", code="not_found")
        try:
            self.docker.remove_container(instance["container_name"])
        except DockerError as exc:
            log.warning("remove container failed: %s", exc)
        try:
            self.docker.remove_volume(instance["config_volume"])
        except DockerError as exc:
            log.warning("remove config volume failed: %s", exc)
        if purge_data:
            try:
                self.docker.remove_volume(instance["data_volume"])
            except DockerError as exc:
                log.warning("remove data volume failed: %s", exc)
        self.db.mark_instance_removed(instance_id)
        self.db.audit(
            "instance_removed",
            email=instance["email"],
            detail={"instance_id": instance_id, "purge_data": purge_data},
        )
        refreshed = self.db.get_instance(instance_id)
        assert refreshed is not None
        return refreshed

    # -- branches --------------------------------------------------------
    def _provision_existing(
        self,
        existing: dict[str, Any],
        *,
        email: str,
        password: str,
        settings: ImapSettings,
        idem_hash: str,
        new_salt: str,
        invite_id: str,
        ip: str,
    ) -> ProvisionResult:
        same_credentials = constant_time_equal(existing["idem_hash"], idem_hash)

        stale_provisioning = (
            existing["status"] == "provisioning"
            and now() - int(existing["updated_at"]) > 300
        )
        if existing["status"] == "provisioning" and not stale_provisioning:
            raise ProvisionError(
                "这个邮箱的实例正在创建中，请稍后刷新重试。",
                code="in_progress",
            )

        if same_credentials:
            # Reuse the instance's salt so the same (email, password) pair keeps
            # returning the same token.  Only rotate the salt when credentials
            # change or when an instance is recreated from a failed/removed state.
            salt = str(existing.get("token_salt") or new_salt)
            token = self.tokens.derive_mcp_token(email, password, salt)
            token_hash = self.tokens.hash_mcp_token(token)
            same_token = constant_time_equal(existing["token_hash"], token_hash)

            # Master-key rotation (or an interrupted previous run) can leave the
            # stored token hash stale.  Rewriting the config and recreating the
            # container repairs it without asking the user for anything new.
            if not same_token or existing["status"] in {"removed", "failed"} or stale_provisioning:
                if existing["status"] in {"removed", "failed"} or stale_provisioning:
                    salt = new_salt
                    token = self.tokens.derive_mcp_token(email, password, salt)
                    token_hash = self.tokens.hash_mcp_token(token)
                self.db.update_instance_credentials(
                    existing["id"],
                    idem_hash=idem_hash,
                    token_hash=token_hash,
                    token_salt=salt,
                    imap_host=settings.host,
                    imap_port=settings.port,
                    tls_mode=settings.tls_mode,
                    provider_key=settings.provider_key,
                    provider_name=settings.provider_name,
                )
                instance = self.db.get_instance(existing["id"])
                assert instance is not None
                self._recreate(instance, password=password, settings=settings, token=token)
                return ProvisionResult(
                    instance=self.db.get_instance(existing["id"]),
                    token=token,
                    reused=True,
                    rotated=False,
                    warning="实例已重建，MCP Token 已轮换，请使用下面的新配置。",
                )

            try:
                instance = self._ensure_running(
                    existing,
                    password=password,
                    settings=settings,
                    token=token,
                )
            except ProvisionError as exc:
                self.db.set_instance_status(existing["id"], "failed", error=exc.message)
                raise
            self.db.touch_instance(instance["id"])
            return ProvisionResult(instance=instance, token=token, reused=True, rotated=False)

        # Same mailbox, different app password: verify the new password before
        # touching the running instance, then rotate token and salt.
        self._validate_imap(settings, password)
        salt = new_salt
        token = self.tokens.derive_mcp_token(email, password, salt)
        token_hash = self.tokens.hash_mcp_token(token)
        self.db.update_instance_credentials(
            existing["id"],
            idem_hash=idem_hash,
            token_hash=token_hash,
            token_salt=salt,
            imap_host=settings.host,
            imap_port=settings.port,
            tls_mode=settings.tls_mode,
            provider_key=settings.provider_key,
            provider_name=settings.provider_name,
        )
        instance = self.db.get_instance(existing["id"])
        assert instance is not None
        self._recreate(instance, password=password, settings=settings, token=token)
        self.db.audit(
            "credentials_rotated",
            ip=ip,
            email=email,
            detail={"instance_id": existing["id"], "invite_id": invite_id},
        )
        return ProvisionResult(
            instance=self.db.get_instance(existing["id"]),
            token=token,
            reused=False,
            rotated=True,
        )

    def _provision_new(
        self,
        *,
        email: str,
        password: str,
        settings: ImapSettings,
        idem_hash: str,
        token_salt: str,
        invite_id: str,
        ip: str,
    ) -> ProvisionResult:
        if self.db.count_active_instances() >= self.config.max_instances:
            raise ProvisionError(
                "当前实例数量已达到部署上限，请联系管理员扩容或清理旧实例。",
                code="capacity",
            )
        self._validate_imap(settings, password)
        token = self.tokens.derive_mcp_token(email, password, token_salt)
        token_hash = self.tokens.hash_mcp_token(token)

        instance_id = self.tokens.new_instance_id()
        record = {
            "id": instance_id,
            "email": email,
            "idem_hash": idem_hash,
            "token_hash": token_hash,
            "token_salt": token_salt,
            "container_name": f"msm-{instance_id}",
            "config_volume": f"msm-config-{instance_id}",
            "data_volume": f"msm-data-{instance_id}",
            "host_port": 0,
            "imap_host": settings.host,
            "imap_port": settings.port,
            "tls_mode": settings.tls_mode,
            "provider_key": settings.provider_key,
            "provider_name": settings.provider_name,
            "status": "provisioning",
            "last_error": None,
            "created_at": now(),
            "updated_at": now(),
        }
        self.db.insert_instance(record)
        instance = self.db.get_instance(instance_id)
        assert instance is not None

        try:
            self._start(instance, password=password, settings=settings, token=token)
        except ProvisionError as exc:
            self.db.set_instance_status(instance_id, "failed", error=exc.message)
            self._cleanup_failed_container(instance["container_name"])
            self._cleanup_failed_config(instance["config_volume"])
            self.db.audit(
                "provision_failed",
                ip=ip,
                email=email,
                detail={"instance_id": instance_id, "error": exc.message},
            )
            raise
        except Exception as exc:  # defensive: never leave a half-created row silent
            self.db.set_instance_status(instance_id, "failed", error=str(exc)[:500])
            self._cleanup_failed_container(instance["container_name"])
            self._cleanup_failed_config(instance["config_volume"])
            log.exception("unexpected provisioning failure for %s", email)
            raise ProvisionError("创建实例时发生内部错误，请稍后重试。", code="internal") from exc

        self.db.audit(
            "instance_created",
            ip=ip,
            email=email,
            detail={"instance_id": instance_id, "invite_id": invite_id},
        )
        return ProvisionResult(
            instance=self.db.get_instance(instance_id),
            token=token,
            reused=False,
            rotated=False,
        )

    # -- container lifecycle --------------------------------------------
    def _validate_target(self, settings: ImapSettings) -> None:
        """Reduce the portal's SSRF surface for user-supplied IMAP targets.

        Known provider presets are trusted, but the advanced form accepts an
        arbitrary host.  For a public deployment we refuse loopback/private
        addresses and non-standard ports unless the operator explicitly opts
        in.  DNS rebinding is still possible in theory; the resolved addresses
        are checked here and the connection is made immediately afterwards.
        """
        if settings.port not in {993, 143} and not self.config.allow_any_imap_port:
            raise ProvisionError(
                "为降低 SSRF 风险，仅允许使用 IMAP 标准端口 993 / 143。"
                "如确需其他端口，请让管理员设置 MSM_ALLOW_ANY_IMAP_PORT=1。",
                field="port",
                code="imap_target",
            )
        # Provider presets point at a fixed, trusted host.  Only the advanced
        # custom-host path needs the address checks (and DNS resolution).
        if settings.provider_key != "custom":
            return
        if self.config.allow_private_imap:
            return

        host = settings.host.strip().lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(
            (".localhost", ".local", ".internal", ".home.arpa")
        ):
            raise ProvisionError(
                "不允许连接本地/内网 IMAP 地址。",
                field="host",
                code="imap_target",
            )

        candidates: list[str] = []
        literal = _parse_ip(host)
        if literal is not None:
            candidates.append(str(literal))
        else:
            try:
                infos = socket.getaddrinfo(host, settings.port, type=socket.SOCK_STREAM)
            except socket.gaierror:
                # Let the IMAP checker produce a precise DNS error.
                return
            for info in infos:
                sockaddr = info[4]
                if sockaddr:
                    candidates.append(str(sockaddr[0]))

        for candidate in candidates:
            address = _parse_ip(candidate)
            if address is None:
                continue
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise ProvisionError(
                    "不允许连接本地/内网 IMAP 地址。",
                    field="host",
                    code="imap_target",
                )

    def _validate_imap(self, settings: ImapSettings, password: str) -> None:
        checker = self._imap_check
        if checker is None:
            from .imap_check import check_login as checker  # local import keeps tests simple
        try:
            checker(settings, password, timeout=15)
        except ImapError as exc:
            raise ProvisionError(
                str(exc),
                field="password",
                hint="请确认已开启 IMAP 服务，并使用邮箱生成的授权码/应用专用密码，而不是登录密码。",
                code="imap_auth",
            ) from exc

    def _start(
        self,
        instance: dict[str, Any],
        *,
        password: str,
        settings: ImapSettings,
        token: str,
    ) -> None:
        self._write_config(instance, password=password, settings=settings, token=token)
        self._remove_container_quietly(instance["container_name"])
        spec = ContainerSpec(
            name=instance["container_name"],
            instance_id=instance["id"],
            network=self.config.docker_network
            if self.config.backend_mode in {"network", "ip"}
            else "",
            image=self.config.docker_image,
            config_volume=instance["config_volume"],
            data_volume=instance["data_volume"],
            memory=self.config.container_memory,
            cpus=self.config.container_cpus,
            pids=self.config.container_pids,
            container_port=self.config.backend_port,
            publish_loopback=self.config.backend_mode == "port",
        )
        try:
            container_id = self.docker.run_container(spec)
        except DockerError as exc:
            raise ProvisionError(
                "启动 Docker 容器失败：" + exc.message,
                hint="请检查 Docker 守护进程、镜像名称和网络配置。",
                code="docker",
            ) from exc
        self.db.set_instance_status(instance["id"], "provisioning", container_id=container_id)

        host_port = 0
        if self.config.backend_mode == "port":
            for _ in range(100):
                host_port = self.docker.container_host_port(
                    instance["container_name"], self.config.backend_port
                ) or 0
                if host_port:
                    break
                time.sleep(0.1)
            if not host_port:
                raise ProvisionError(
                    "容器已启动，但无法读取 Docker 分配的 loopback 端口。",
                    hint="请确认 Docker 的端口映射正常，或改用 MSM_BACKEND_MODE=network。",
                    code="docker",
                )
            self.db.set_instance_host_port(instance["id"], host_port)

        warning = self._wait_ready(instance["container_name"], host_port=host_port)
        self.db.set_instance_status(instance["id"], "running", error=None)
        if warning:
            log.warning("instance %s started with warning: %s", instance["id"], warning)

    def _recreate(
        self,
        instance: dict[str, Any],
        *,
        password: str,
        settings: ImapSettings,
        token: str,
    ) -> None:
        self.db.set_instance_status(instance["id"], "updating", error=None)
        try:
            self._start(instance, password=password, settings=settings, token=token)
        except ProvisionError as exc:
            self.db.set_instance_status(instance["id"], "failed", error=exc.message)
            self._cleanup_failed_container(instance["container_name"])
            self._cleanup_failed_config(instance["config_volume"])
            raise

    def _ensure_running(
        self,
        instance: dict[str, Any],
        *,
        password: str,
        settings: ImapSettings,
        token: str,
    ) -> dict[str, Any]:
        name = instance["container_name"]
        info = self.docker.inspect_container(name)
        if info:
            state = info.get("State") or {}
            if state.get("Running"):
                if self.config.backend_mode == "port":
                    host_port = self.docker.container_host_port(
                        name, self.config.backend_port
                    ) or 0
                    if not host_port:
                        # The container was created in network/ip mode (or its
                        # mapping was removed): recreate it with a loopback
                        # publish so the gateway can reach it.
                        self._start(instance, password=password, settings=settings, token=token)
                        return self.db.get_instance(instance["id"]) or instance
                    if int(instance.get("host_port") or 0) != host_port:
                        self.db.set_instance_host_port(instance["id"], host_port)
                    instance = self.db.get_instance(instance["id"]) or instance
                return instance
            if self.config.backend_mode == "port" and not (
                self.docker.container_host_port(name, self.config.backend_port) or 0
            ):
                self._start(instance, password=password, settings=settings, token=token)
                return self.db.get_instance(instance["id"]) or instance
            try:
                self.docker.start_container(name)
            except DockerError as exc:
                raise ProvisionError(
                    "无法启动已有容器：" + exc.message,
                    code="docker",
                ) from exc
            host_port = 0
            if self.config.backend_mode == "port":
                host_port = self.docker.container_host_port(
                    name, self.config.backend_port
                ) or 0
                if not host_port:
                    raise ProvisionError(
                        "已有容器缺少 loopback 端口映射，无法转发请求。",
                        code="docker",
                    )
                self.db.set_instance_host_port(instance["id"], host_port)
            self._wait_ready(name, host_port=host_port)
            self.db.set_instance_status(instance["id"], "running", error=None)
            return self.db.get_instance(instance["id"]) or instance

        # Container disappeared (host cleanup, manual docker rm, ...).  The
        # config and data volumes may still exist, so recreate from the
        # credentials supplied by the user right now.
        self._start(instance, password=password, settings=settings, token=token)
        return self.db.get_instance(instance["id"]) or instance

    def _write_config(
        self,
        instance: dict[str, Any],
        *,
        password: str,
        settings: ImapSettings,
        token: str,
    ) -> None:
        content = render_instance_config(
            email=instance["email"],
            settings=settings,
            password=password,
            bearer_token=token,
            http_port=self.config.backend_port,
        )
        try:
            self.docker.write_config_volume(
                instance["config_volume"],
                self.config.docker_image,
                content,
            )
        except DockerError as exc:
            raise ProvisionError(
                "写入实例配置失败：" + exc.message,
                hint="请确认 Docker 镜像可以正常启动。",
                code="docker",
            ) from exc

    def _wait_ready(
        self, container_name: str, timeout: float = 45.0, *, host_port: int = 0
    ) -> str:
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            info = self.docker.inspect_container(container_name)
            if info is None:
                raise ProvisionError("容器启动后立即消失，请查看 Docker 日志。", code="docker")
            state = info.get("State") or {}
            status = str(state.get("Status", ""))
            if status in {"exited", "dead"}:
                logs = self.docker.logs(container_name)
                log.warning("container %s exited: %s", container_name, logs)
                raise ProvisionError(
                    "容器启动后退出。Docker 日志：" + (logs[-800:] or "（无日志）"),
                    code="docker",
                )
            if state.get("Running"):
                if not self.config.verify_backend:
                    time.sleep(0.8)
                    return ""
                ok, detail = self._probe_mcp(container_name, host_port=host_port)
                if ok:
                    return ""
                last_error = detail
            time.sleep(0.6)
        if self.config.verify_backend:
            logs = self.docker.logs(container_name)
            raise ProvisionError(
                "容器已启动，但 MCP 端口未在限定时间内就绪"
                + (f"（{last_error}）" if last_error else "")
                + "。Docker 日志：" + (logs[-800:] or "（无日志）"),
                hint="宿主机直接运行 portal 时请使用 MSM_BACKEND_MODE=port；容器化部署使用 network。",
                code="healthcheck",
            )
        return ""

    def _probe_mcp(self, container_name: str, *, host_port: int = 0) -> tuple[bool, str]:
        if self.config.backend_mode == "port":
            if not host_port:
                return False, "缺少 loopback 端口"
            host, port = "127.0.0.1", host_port
        elif self.config.backend_mode == "ip":
            host = self.docker.container_ip(container_name, self.config.docker_network) or ""
            port = self.config.backend_port
            if not host:
                return False, "无法获取容器 IP"
        else:
            host, port = container_name, self.config.backend_port
        try:
            connection = http.client.HTTPConnection(host, port, timeout=2.0)
            try:
                connection.request("GET", "/mcp")
                response = connection.getresponse()
                response.read(256)
                # The instance always has a bearer token, so an unauthenticated
                # request must come back as 401.  Any HTTP response proves the
                # listener is up, but 401 specifically proves the MCP handler ran.
                if response.status == 401:
                    return True, ""
                return False, f"HTTP {response.status}"
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as exc:
            return False, type(exc).__name__

    def _remove_container_quietly(self, name: str) -> None:
        try:
            self.docker.remove_container(name)
        except DockerError as exc:
            log.warning("could not remove container %s before start: %s", name, exc)

    def _cleanup_failed_container(self, name: str) -> None:
        try:
            self.docker.remove_container(name)
        except DockerError:
            pass

    def _cleanup_failed_config(self, volume: str) -> None:
        # The config volume contains the mailbox app password.  Do not leave it
        # behind after a failed start; a retry writes it again.
        try:
            self.docker.remove_volume(volume)
        except DockerError:
            pass

    # -- locking ---------------------------------------------------------
    @contextlib.contextmanager
    def _lock_for(self, email: str):
        with self._locks_guard:
            lock = self._locks.setdefault(email, threading.Lock())
        with lock:
            yield


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None
