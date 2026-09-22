#!/usr/bin/env python3
"""Write a restricted host metrics snapshot using atomic replace."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import os
import shutil
import tempfile
from glob import glob


def _memory_percent() -> float | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    values: dict[str, int] = {}
    for line in meminfo.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        if not value:
            continue
        try:
            values[key] = int(value.strip().split()[0])
        except (ValueError, IndexError):
            continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if not total or available is None:
        return None
    return round((total - available) * 100 / total, 2)


def _temperatures() -> list[float]:
    readings: list[float] = []
    for path in glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            readings.append(round(int(Path(path).read_text(encoding="utf-8")) / 1000, 1))
        except (OSError, ValueError):
            continue
    return readings


def collect(*, media_path: Path = Path("/srv/data")) -> dict[str, object]:
    uptime = None
    proc_uptime = Path("/proc/uptime")
    if proc_uptime.exists():
        uptime = int(float(proc_uptime.read_text(encoding="utf-8").split()[0]))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "host": {
            "cpu_percent": None,
            "ram_percent": _memory_percent(),
            "cpu_celsius": min(_temperatures(), default=None),
            "uptime_seconds": uptime,
        },
        "storage": (
            {
                "path": str(media_path),
                "total_bytes": shutil.disk_usage(media_path).total,
                "free_bytes": shutil.disk_usage(media_path).free,
            }
            if media_path.exists()
            else None
        ),
        "pid": os.getpid(),
    }


def write_atomic(output: Path, payload: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/run/homeserver/host.json"))
    args = parser.parse_args()
    write_atomic(args.output, collect())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
