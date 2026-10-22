#!/usr/bin/env python3
"""Administrative CLI for the mail-shadow-mcp hosting portal.

Examples::

    python cli.py invite create --label "alice" --uses 1 --days 7
    python cli.py invite list
    python cli.py invite disable 1a2b3c4d5e6f7788
    python cli.py instance list
    python cli.py instance remove 1a2b3c4d5e6f7788 --purge-data
    python cli.py doctor
"""

from __future__ import annotations

import argparse
import sys
import time

from app.config import Config, ConfigError
from app.db import Database, now
from app.docker_cli import DockerClient, DockerError
from app.provisioner import Provisioner
from app.security import TokenService


def _fmt_time(timestamp: int | None) -> str:
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(timestamp)))


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    line = "  ".join(h.ljust(widths[index]) for index, h in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[index]) for index, cell in enumerate(row)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mail-shadow-mcp hosting portal admin")
    sub = parser.add_subparsers(dest="command", required=True)

    invite = sub.add_parser("invite", help="manage invite codes")
    invite_sub = invite.add_subparsers(dest="invite_command", required=True)
    create = invite_sub.add_parser("create", help="create an invite code")
    create.add_argument("--label", default="", help="human-readable label")
    create.add_argument("--uses", type=int, default=1, help="maximum number of uses (default 1)")
    create.add_argument("--days", type=int, default=7, help="validity in days, 0 = never expires")
    invite_sub.add_parser("list", help="list invite codes")
    disable = invite_sub.add_parser("disable", help="disable an invite code")
    disable.add_argument("id")

    instance = sub.add_parser("instance", help="manage provisioned instances")
    instance_sub = instance.add_subparsers(dest="instance_command", required=True)
    instance_sub.add_parser("list", help="list instances")
    remove = instance_sub.add_parser("remove", help="stop and remove an instance")
    remove.add_argument("id")
    remove.add_argument("--purge-data", action="store_true", help="also delete the mail database volume")
    reconcile = instance_sub.add_parser(
        "reconcile",
        help="recreate existing containers on the current image/config without user input",
    )
    reconcile.add_argument("--all", action="store_true", help="reconcile every non-removed instance")
    reconcile.add_argument("--id", help="reconcile one instance by ID")
    reconcile.add_argument("--force", action="store_true", help="recreate even if image and config match")
    reconcile.add_argument("--dry-run", action="store_true", help="show what would be recreated")
    reconcile.add_argument(
        "--update-config",
        action="store_true",
        help="also apply current MSM_SYNC_FOLDERS/MSM_TRASH_FOLDER (default: keep the existing config)",
    )

    sub.add_parser("doctor", help="check Docker, storage and configuration")

    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    config.ensure_data_dir()
    db = Database(config.data_dir / "control.db")
    tokens = TokenService(config.master_key)

    try:
        if args.command == "invite":
            return _invite_command(args, config, db, tokens)
        if args.command == "instance":
            return _instance_command(args, config, db, tokens)
        if args.command == "doctor":
            return _doctor(config, db)
        parser.print_help()
        return 1
    finally:
        db.close()


def _invite_command(args: argparse.Namespace, config: Config, db: Database, tokens: TokenService) -> int:
    if args.invite_command == "create":
        if args.uses < 1:
            print("--uses must be >= 1", file=sys.stderr)
            return 2
        if args.days < 0:
            print("--days must be >= 0", file=sys.stderr)
            return 2
        code = tokens.new_invite_code()
        expires_at = None if args.days == 0 else now() + args.days * 86400
        invite_id = db.create_invite(
            tokens.hash_invite(code),
            label=args.label,
            max_uses=args.uses,
            expires_at=expires_at,
        )
        print(f"invite_id: {invite_id}")
        print(f"code:      {code}")
        print(f"uses:      {args.uses}")
        print(f"expires:   {'never' if expires_at is None else _fmt_time(expires_at)}")
        print("\nThe code is shown only once. Store it in a password manager.")
        return 0

    if args.invite_command == "list":
        invites = db.list_invites()
        if not invites:
            print("no invites")
            return 0
        rows = [
            [
                item["id"],
                item["label"] or "-",
                f"{item['uses']}/{item['max_uses']}",
                str(item.get("mailbox_count", 0)),
                "never" if item["expires_at"] is None else _fmt_time(item["expires_at"]),
                "yes" if item["disabled"] else "no",
                _fmt_time(item["created_at"]),
            ]
            for item in invites
        ]
        _print_table(["id", "label", "sessions", "mailboxes", "expires", "disabled", "created"], rows)
        return 0

    if args.invite_command == "disable":
        if db.disable_invite(args.id):
            print(f"disabled {args.id}")
            return 0
        print(f"invite not found: {args.id}", file=sys.stderr)
        return 1

    return 1


def _instance_command(args: argparse.Namespace, config: Config, db: Database, tokens: TokenService) -> int:
    if args.instance_command == "list":
        instances = db.list_instances()
        if not instances:
            print("no instances")
            return 0
        rows = [
            [
                item["id"],
                item["email"],
                item["status"],
                item["container_name"],
                str(item.get("host_port") or "-"),
                _fmt_time(item["created_at"]),
                _fmt_time(item["last_seen_at"]),
            ]
            for item in instances
        ]
        _print_table(["id", "email", "status", "container", "loopback_port", "created", "last_seen"], rows)
        return 0

    if args.instance_command == "reconcile":
        if bool(args.all) == bool(args.id):
            print("specify exactly one of --all or --id", file=sys.stderr)
            return 2
        docker = DockerClient(config.docker_bin)
        provisioner = Provisioner(config, db, docker, tokens)
        if args.all:
            targets = [item for item in db.list_instances() if item["status"] != "removed"]
        else:
            target = db.get_instance(args.id)
            if not target:
                print(f"instance not found: {args.id}", file=sys.stderr)
                return 1
            targets = [target]
        if not targets:
            print("no instances to reconcile")
            return 0
        failures = 0
        print("id  email  action  message")
        for item in targets:
            try:
                result = provisioner.reconcile_instance(
                    item["id"],
                    force=args.force,
                    dry_run=args.dry_run,
                    update_config=args.update_config,
                )
            except Exception as exc:
                failures += 1
                print(f"{item['id']}  {item['email']}  failed  {exc}", file=sys.stderr)
                continue
            print(f"{result.instance_id}  {result.email}  {result.action}  {result.message}")
        return 1 if failures else 0

    if args.instance_command == "remove":
        docker = DockerClient(config.docker_bin)
        provisioner = Provisioner(config, db, docker, tokens)
        try:
            instance = provisioner.remove_instance(args.id, purge_data=args.purge_data)
        except Exception as exc:
            print(f"remove failed: {exc}", file=sys.stderr)
            return 1
        print(f"removed {instance['id']} ({instance['email']}); data_purged={args.purge_data}")
        return 0

    return 1


def _doctor(config: Config, db: Database) -> int:
    ok = True
    print(f"data_dir:       {config.data_dir}")
    print(f"public_url:     {config.public_url}")
    print(f"secure_cookies: {config.secure_cookies}")
    print(f"docker_network: {config.docker_network}")
    print(f"docker_image:   {config.docker_image}")
    print(f"backend_mode:   {config.backend_mode}")
    print(f"max_instances:  {config.max_instances or 'unlimited'}")
    print(f"master_key:     {len(config.master_key)} bytes")

    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        probe = config.data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print("storage:        writable")
    except OSError as exc:
        ok = False
        print(f"storage:        NOT WRITABLE ({exc})")

    try:
        print(f"invites:        {db.invite_count()}")
        print(f"instances:      {len(db.list_instances())}")
    except Exception as exc:
        ok = False
        print(f"database:       ERROR ({exc})")

    docker = DockerClient(config.docker_bin)
    try:
        print(f"docker:         {docker.version()}")
    except DockerError as exc:
        ok = False
        print(f"docker:         ERROR ({exc})")
        return 1

    if config.backend_mode in {"network", "ip"}:
        if docker.network_exists(config.docker_network):
            print("network:        exists")
        else:
            ok = False
            print("network:        missing (the portal will create it on startup)")
    else:
        print("network:        not required in port mode")

    if docker.image_exists(config.docker_image):
        print("image:          present")
    else:
        ok = False
        print("image:          missing (will be pulled on first use)")

    try:
        containers = docker.list_managed_containers()
        print(f"containers:     {len(containers)} managed")
    except DockerError as exc:
        ok = False
        print(f"containers:     ERROR ({exc})")

    print("status:         OK" if ok else "status:         ATTENTION NEEDED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
