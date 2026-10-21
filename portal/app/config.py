"""Runtime configuration for the mail-shadow-mcp hosting portal.

Everything is read from the environment so the same image can be deployed
without editing files.  The portal deliberately has no third-party Python
dependencies; see portal/README.md for the full reference.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(RuntimeError):
    """Raised when the environment cannot produce a usable configuration."""


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _load_master_key(data_dir: Path) -> bytes:
    """Return the portal master key, creating a persistent one on first run.

    The key protects invite hashes, session cookies, flash cookies and the
    deterministic MCP bearer tokens.  Losing it invalidates every MCP token
    and every active browser session, so it must live on persistent storage.
    """
    env_key = os.environ.get("MSM_MASTER_KEY")
    if env_key:
        key = env_key.encode("utf-8")
        if len(key) < 32:
            raise ConfigError("MSM_MASTER_KEY must be at least 32 bytes long")
        return key

    key_path = data_dir / "master.key"
    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        data_dir.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        # O_EXCL prevents two racing processes from silently overwriting a key.
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = key_path.read_bytes()
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key)
    if len(key) < 32:
        raise ConfigError(f"{key_path} is too short; expected at least 32 bytes")
    return key


@dataclass(frozen=True)
class Config:
    data_dir: Path
    public_url: str
    public_origin: str
    secure_cookies: bool
    listen_host: str
    listen_port: int
    master_key: bytes = field(repr=False)

    docker_bin: str = "docker"
    docker_network: str = "msm-net"
    docker_image: str = "ghcr.io/dryas/mail-shadow-mcp:latest"
    backend_mode: str = "port"
    backend_port: int = 8080
    verify_backend: bool = True
    allow_insecure_imap: bool = False
    allow_private_imap: bool = False
    allow_any_imap_port: bool = False
    trust_proxy: bool = False

    max_instances: int = 25
    session_ttl_seconds: int = 7200
    flash_ttl_seconds: int = 900
    container_memory: str = "512m"
    container_cpus: str = "1.0"
    container_pids: int = 256
    log_level: str = "INFO"

    invite_rate_limit: int = 12
    invite_rate_window: int = 900
    provision_rate_limit: int = 8
    provision_rate_window: int = 3600
    gateway_rate_limit: int = 120
    gateway_rate_window: int = 60

    bootstrap_invite: str = field(default="", repr=False)
    bootstrap_invite_uses: int = 1
    bootstrap_invite_days: int = 7

    @classmethod
    def from_env(cls) -> "Config":
        data_dir = Path(_env("MSM_DATA_DIR", "data")).expanduser().resolve()
        public_url = _env("MSM_PUBLIC_URL", "http://localhost:8080").rstrip("/")
        parts = urlsplit(public_url)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"} or not parts.netloc:
            raise ConfigError("MSM_PUBLIC_URL must be an absolute http(s) URL")
        if parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ConfigError("MSM_PUBLIC_URL must be an origin without a path")

        secure_default = scheme == "https"
        backend_mode = _env("MSM_BACKEND_MODE", "port").lower()
        if backend_mode not in {"network", "ip", "port"}:
            raise ConfigError("MSM_BACKEND_MODE must be 'network', 'ip' or 'port'")

        try:
            master_key = _load_master_key(data_dir)
        except OSError as exc:
            raise ConfigError(f"cannot prepare master key in {data_dir}: {exc}") from exc

        return cls(
            data_dir=data_dir,
            public_url=f"{scheme}://{parts.netloc}",
            public_origin=f"{scheme}://{parts.netloc}",
            secure_cookies=_env_bool("MSM_SECURE_COOKIES", secure_default),
            listen_host=_env("MSM_LISTEN_HOST", "0.0.0.0"),
            listen_port=_env_int("MSM_LISTEN_PORT", 8080),
            master_key=master_key,
            docker_bin=_env("MSM_DOCKER_BIN", "docker"),
            docker_network=_env("MSM_DOCKER_NETWORK", "msm-net"),
            docker_image=_env("MSM_IMAGE", "ghcr.io/dryas/mail-shadow-mcp:latest"),
            backend_mode=backend_mode,
            backend_port=_env_int("MSM_BACKEND_PORT", 8080, 1),
            verify_backend=_env_bool("MSM_VERIFY_BACKEND", True),
            allow_insecure_imap=_env_bool("MSM_ALLOW_INSECURE_IMAP", False),
            allow_private_imap=_env_bool("MSM_ALLOW_PRIVATE_IMAP", False),
            allow_any_imap_port=_env_bool("MSM_ALLOW_ANY_IMAP_PORT", False),
            trust_proxy=_env_bool("MSM_TRUST_PROXY", False),
            max_instances=_env_int("MSM_MAX_INSTANCES", 25, 1),
            session_ttl_seconds=_env_int("MSM_SESSION_TTL", 7200, 60),
            flash_ttl_seconds=_env_int("MSM_FLASH_TTL", 900, 60),
            container_memory=_env("MSM_CONTAINER_MEMORY", "512m"),
            container_cpus=_env("MSM_CONTAINER_CPUS", "1.0"),
            container_pids=_env_int("MSM_CONTAINER_PIDS", 256, 32),
            log_level=_env("MSM_LOG_LEVEL", "INFO").upper(),
            invite_rate_limit=_env_int("MSM_INVITE_RATE_LIMIT", 12, 1),
            invite_rate_window=_env_int("MSM_INVITE_RATE_WINDOW", 900, 1),
            provision_rate_limit=_env_int("MSM_PROVISION_RATE_LIMIT", 8, 1),
            provision_rate_window=_env_int("MSM_PROVISION_RATE_WINDOW", 3600, 1),
            gateway_rate_limit=_env_int("MSM_GATEWAY_RATE_LIMIT", 120, 1),
            gateway_rate_window=_env_int("MSM_GATEWAY_RATE_WINDOW", 60, 1),
            bootstrap_invite=os.environ.get("MSM_BOOTSTRAP_INVITE", "").strip(),
            bootstrap_invite_uses=_env_int("MSM_BOOTSTRAP_INVITE_USES", 1, 1),
            bootstrap_invite_days=_env_int("MSM_BOOTSTRAP_INVITE_DAYS", 7, 1),
        )

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            mode = os.stat(self.data_dir).st_mode & 0o777
            if mode & 0o077:
                os.chmod(self.data_dir, 0o700)
        except OSError:
            pass
