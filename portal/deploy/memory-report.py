#!/usr/bin/env python3
"""Measure the real memory footprint of the hosted instances.

Run this on the Docker host *after* every instance has finished its first sync
(the peak is during import, not when the containers are idle)::

    python3 deploy/memory-report.py --budget-mb 4096

Only the USAGE column of ``docker stats`` counts against the host; LIMIT is the
per-container cgroup ceiling, and the sum of those ceilings is expected to be
larger than the host RAM (128m x 100 = 12.8 GB).  The script therefore compares
the summed USAGE with the budget and fails when it exceeds the budget minus the
requested safety margin.

The portal/Caddy container is excluded by the default ``msm-`` name prefix; add
``--prefix ''`` to include everything.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Docker prints e.g. "26.5MiB", "512KiB", "1.2GiB".  Docker Desktop can use
# decimal units ("MB"); both are treated as MiB/GiB for a conservative estimate.
_UNIT_MIB = {
    "B": 1 / (1024 * 1024),
    "KIB": 1 / 1024,
    "MIB": 1,
    "GIB": 1024,
    "KB": 1 / 1000,
    "MB": 1,
    "GB": 1000,
}
_MEM_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*$")


def parse_mib(value: str) -> float:
    match = _MEM_RE.match(value)
    if not match:
        raise ValueError(f"cannot parse memory value {value!r}")
    number, unit = match.groups()
    factor = _UNIT_MIB.get(unit.upper())
    if factor is None:
        raise ValueError(f"unknown memory unit {unit!r}")
    return float(number) * factor


def host_memory_mib() -> tuple[float, float] | None:
    """Return (MemTotal, MemAvailable) in MiB, or None outside Linux."""
    try:
        fields = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            fields[key] = rest.strip()
        total = float(fields["MemTotal"].split()[0]) / 1024
        available = float(fields["MemAvailable"].split()[0]) / 1024
        return total, available
    except (OSError, KeyError, IndexError, ValueError):
        return None


def docker_stats(prefix: str) -> list[tuple[str, float]]:
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("docker CLI not found in PATH")
    result = subprocess.run(
        [docker, "stats", "--no-stream", "--format", "{{json .}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"docker stats failed: {detail or result.returncode}")
    rows: list[tuple[str, float]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            name = str(item.get("Name") or "")
            usage = str(item.get("MemUsage") or "").split("/")[0]
            if prefix and not name.startswith(prefix):
                continue
            rows.append((name, parse_mib(usage)))
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget-mb", type=float, default=4096.0, help="host memory budget in MiB")
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.90,
        help="fail when instance usage exceeds this fraction of the budget (default 0.90)",
    )
    parser.add_argument("--prefix", default="msm-", help="container name prefix ('' = all)")
    args = parser.parse_args()

    try:
        rows = docker_stats(args.prefix)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not rows:
        print(
            f"no containers matching prefix {args.prefix!r}; "
            "is the stack running and are the names msm-<id>?",
            file=sys.stderr,
        )
        return 2

    usages = sorted(value for _, value in rows)
    total = sum(usages)
    average = total / len(usages)
    peak = usages[-1]
    budget = args.budget_mb
    limit = budget * args.threshold
    host = host_memory_mib()

    print(f"instances:     {len(rows)}")
    print(f"usage total:   {total:8.1f} MiB ({total / 1024:.2f} GiB)")
    print(f"per instance:  avg {average:6.1f} MiB / max {peak:6.1f} MiB")
    if host is not None:
        host_total, host_available = host
        print(f"host:          total {host_total:.0f} MiB / available {host_available:.0f} MiB")
    print(
        f"budget:        {budget:.0f} MiB, "
        f"instances use {total / budget * 100:.1f}% (fail above {args.threshold * 100:.0f}%)"
    )

    over = total > limit
    host_over = host is not None and host[1] < budget * 0.10
    if over:
        print("result:        OVER BUDGET — lower MSM_CONTAINER_GOMEMLIMIT / MSM_SYNC_INTERVAL_MIN")
    elif host_over:
        print("result:        OK for instances, but host MemAvailable is under 10% — add RAM or lower the cap")
    else:
        print("result:        OK")
    return 1 if over else 0


if __name__ == "__main__":
    raise SystemExit(main())
