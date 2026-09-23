#!/usr/bin/env python3
"""Write a restricted host metrics snapshot using atomic replace."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from glob import glob
from pathlib import Path


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


def _cpu_temperatures() -> list[float]:
    readings: list[float] = []
    for path in glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            sensor_type = (Path(path).parent / "type").read_text(encoding="utf-8").strip().lower()
            if sensor_type not in {"x86_pkg_temp", "cpu-thermal", "cpu_thermal", "cpu", "coretemp"}:
                continue
            readings.append(round(int(Path(path).read_text(encoding="utf-8")) / 1000, 1))
        except (OSError, ValueError):
            continue
    return readings


def _cpu_counters(proc_root: Path) -> tuple[int, int] | None:
    try:
        line = (proc_root / "stat").read_text(encoding="utf-8").splitlines()[0]
        fields = line.split()
        if fields[0] != "cpu" or len(fields) < 6:
            return None
        values = [int(value) for value in fields[1:]]
        # guest and guest_nice are already included in user and nice.
        return sum(values[:8]), values[3] + values[4]
    except (OSError, IndexError, ValueError):
        return None


def _default_interface(proc_root: Path) -> str | None:
    try:
        routes = (proc_root / "net" / "route").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return None
    candidates: list[tuple[int, str]] = []
    for line in routes:
        fields = line.split()
        try:
            if len(fields) >= 8 and fields[1] == "00000000" and int(fields[3], 16) & 1:
                candidates.append((int(fields[6]), fields[0]))
        except ValueError:
            continue
    return min(candidates)[1] if candidates else None


def _interface_counters(network_root: Path, interface: str | None) -> tuple[int, int] | None:
    if interface is None:
        return None
    try:
        statistics = network_root / interface / "statistics"
        return (
            int((statistics / "rx_bytes").read_text(encoding="utf-8")),
            int((statistics / "tx_bytes").read_text(encoding="utf-8")),
        )
    except (OSError, ValueError):
        return None


def collect(
    *,
    media_path: Path = Path("/srv/data"),
    proc_root: Path = Path("/proc"),
    network_root: Path = Path("/sys/class/net"),
    sampled_at: float | None = None,
    previous: dict[str, object] | None = None,
) -> dict[str, object]:
    sampled_at = time.time() if sampled_at is None else sampled_at
    cpu = _cpu_counters(proc_root)
    interface = _default_interface(proc_root)
    network = _interface_counters(network_root, interface)
    old = previous.get("counters", {}) if isinstance(previous, dict) else {}
    if not isinstance(old, dict):
        old = {}
    old_sampled_at = old.get("sampled_at")
    age = sampled_at - old_sampled_at if type(old_sampled_at) in (int, float) else -1
    sample_valid = 0 < age <= 90
    cpu_percent = None
    if sample_valid and cpu is not None:
        old_total, old_idle = old.get("cpu_total"), old.get("cpu_idle")
        if type(old_total) is int and type(old_idle) is int:
            elapsed = cpu[0] - old_total
            idle = cpu[1] - old_idle
            if elapsed > 0 and 0 <= idle <= elapsed:
                cpu_percent = round(100 * (elapsed - idle) / elapsed, 2)
    rx_bps = tx_bps = None
    if sample_valid and network is not None and interface == old.get("network_interface"):
        old_rx, old_tx = old.get("rx_bytes"), old.get("tx_bytes")
        if type(old_rx) is int and type(old_tx) is int:
            rx_delta, tx_delta = network[0] - old_rx, network[1] - old_tx
            if rx_delta >= 0 and tx_delta >= 0:
                rx_bps, tx_bps = round(rx_delta / age), round(tx_delta / age)
    uptime = None
    proc_uptime = proc_root / "uptime"
    if proc_uptime.exists():
        uptime = int(float(proc_uptime.read_text(encoding="utf-8").split()[0]))
    usage = shutil.disk_usage(media_path) if media_path.exists() else None
    return {
        "schema_version": 1,
        "generated_at": datetime.fromtimestamp(sampled_at, UTC).isoformat().replace("+00:00", "Z"),
        "host": {
            "cpu_percent": cpu_percent,
            "ram_percent": _memory_percent(),
            "cpu_celsius": max(_cpu_temperatures(), default=None),
            "uptime_seconds": uptime,
        },
        "network": {"interface": interface, "rx_bps": rx_bps, "tx_bps": tx_bps},
        "storage": (
            {
                "path": str(media_path),
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            }
            if usage is not None
            else None
        ),
        "counters": {
            "sampled_at": sampled_at,
            "cpu_total": cpu[0] if cpu else None,
            "cpu_idle": cpu[1] if cpu else None,
            "network_interface": interface,
            "rx_bytes": network[0] if network else None,
            "tx_bytes": network[1] if network else None,
        },
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
    try:
        previous = json.loads(args.output.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    write_atomic(args.output, collect(previous=previous))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
