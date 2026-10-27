"""Entry point for the hosting portal (``python -m app``)."""

from __future__ import annotations

import logging
import signal
import sqlite3
import sys
import threading
from urllib.parse import urlsplit

from .config import Config, ConfigError
from .db import Database, now
from .docker_cli import DockerClient, DockerError
from .gateway import Gateway
from .provisioner import Provisioner
from .security import RateLimiter, TokenService
from .stats import StatsCollector
from .web import WebApp

log = logging.getLogger("app")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )
    logging.getLogger("http.server").setLevel(logging.WARNING)


def _bootstrap_invite(config: Config, db: Database, tokens: TokenService) -> None:
    code = config.bootstrap_invite
    if not code:
        return
    if len(code) < 16:
        log.warning("MSM_BOOTSTRAP_INVITE is shorter than 16 characters; use a high-entropy code")
    code_hash = tokens.hash_invite(code)
    expires_at = now() + config.bootstrap_invite_days * 86400
    try:
        invite_id = db.create_invite(
            code_hash,
            label="bootstrap",
            max_uses=config.bootstrap_invite_uses,
            expires_at=expires_at,
        )
    except sqlite3.IntegrityError:
        log.info("bootstrap invite already exists; leaving it unchanged")
        return
    log.info(
        "created bootstrap invite id=%s uses=%d expires_in_days=%d",
        invite_id,
        config.bootstrap_invite_uses,
        config.bootstrap_invite_days,
    )


def main() -> int:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(config.log_level)
    config.ensure_data_dir()

    public_scheme = urlsplit(config.public_url).scheme
    if public_scheme != "https" and not config.public_url.startswith(
        ("http://localhost", "http://127.0.0.1", "http://[::1]")
    ):
        log.warning(
            "MSM_PUBLIC_URL is not https; bearer tokens and app passwords would cross the network in clear text"
        )
    if config.backend_mode == "network":
        log.info(
            "backend_mode=network: portal must share Docker network %s with instance containers",
            config.docker_network,
        )
    elif config.backend_mode == "port":
        log.info("backend_mode=port: instances publish a random loopback-only port")

    db = Database(config.data_dir / "control.db")
    tokens = TokenService(config.master_key)
    docker = DockerClient(config.docker_bin)

    try:
        version = docker.version()
        log.info("docker daemon %s", version)
    except DockerError as exc:
        log.error("cannot talk to Docker: %s", exc)
        db.close()
        return 3

    try:
        if config.backend_mode in {"network", "ip"}:
            docker.ensure_network(config.docker_network)
        else:
            log.info("backend_mode=port; containers will use Docker's default bridge")
    except DockerError as exc:
        log.error("cannot prepare Docker network %s: %s", config.docker_network, exc)
        db.close()
        return 3

    if not docker.image_exists(config.docker_image):
        log.warning(
            "image %s is not present locally; it will be pulled on the first provisioning request",
            config.docker_image,
        )

    _bootstrap_invite(config, db, tokens)

    limiter = RateLimiter()
    provisioner = Provisioner(config, db, docker, tokens)
    gateway = Gateway(config, db, docker, tokens, limiter)
    stats = StatsCollector(config, db, docker)
    web = WebApp(
        config,
        db,
        tokens,
        provisioner,
        gateway,
        limiter,
        stats=stats,
        docker=docker,
    )
    server = web.create_server()
    if config.admin_password:
        stats.start()
        log.info("admin console enabled at /admin (stats every %ds)", config.stats_interval_seconds)
    else:
        log.info("admin console disabled: set MSM_ADMIN_PASSWORD to enable /admin")

    def _stop(signum: int, _frame: object) -> None:
        log.info("received signal %s, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    log.info(
        "portal listening on %s:%d public_url=%s data_dir=%s network=%s image=%s max_instances=%d",
        config.listen_host,
        config.listen_port,
        config.public_url,
        config.data_dir,
        config.docker_network,
        config.docker_image,
        config.max_instances,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        stats.stop()
        db.close()
        log.info("portal stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
