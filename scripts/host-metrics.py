#!/usr/bin/env python3
"""Write a restricted host metrics snapshot using atomic replace."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
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
    network_interface: str = "auto",
    registry=None,
    filesystem_id: str | None = None,
    storage_available: bool = True,
) -> dict[str, object]:
    sampled_at = time.time() if sampled_at is None else sampled_at
    cpu = _cpu_counters(proc_root)
    interface = _default_interface(proc_root) if network_interface == "auto" else network_interface
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
    try:
        usage = (
            shutil.disk_usage(media_path)
            if storage_available and registry is None and media_path.exists()
            else None
        )
    except OSError:
        usage = None
    result = {
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
                "filesystem_id": filesystem_id or "device:" + str(media_path.stat().st_dev),
                "measured_at": sampled_at,
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
    if registry is not None:
        from homeserver_common.storage import StorageUnavailable

        pools = []
        for pool_id in registry.pool_ids:
            pool = registry.pools[pool_id]
            try:
                sample = registry.inspect(pool_id, writable=False).as_dict()
                categories = _storage_categories(pool.root, sample["used_bytes"])
                # Revalidate after walking; unplugged/replaced pools cannot publish categories.
                latest = registry.inspect(pool_id, writable=False)
                if latest.filesystem_id != sample["filesystem_id"]:
                    raise StorageUnavailable("filesystem_changed")
                sample["storage"] = {
                    "state": "ok",
                    "measured_at": sample["measured_at"],
                    **categories,
                }
            except (StorageUnavailable, OSError) as exc:
                sample = {
                    "pool_id": pool_id,
                    "label": pool.label,
                    "filesystem_id": pool.filesystem_id,
                    "state": "unavailable",
                    "reason": str(exc),
                    "measured_at": sampled_at,
                    "total_bytes": None,
                    "used_bytes": None,
                    "free_bytes": None,
                    "storage": {
                        "state": "unavailable",
                        "measured_at": None,
                        **dict.fromkeys(
                            ("movies_bytes", "series_bytes", "torrents_bytes", "other_bytes")
                        ),
                    },
                }
            pools.append(sample)
        result["pools"] = pools
        result["storage"] = next(pool for pool in pools if pool["pool_id"] == "ssd")
    return result


def _storage_categories(root: Path, used_bytes: int) -> dict[str, int | None]:
    device = root.stat().st_dev
    seen = set()
    categories = {}
    for name, relative in (
        ("movies", "media/movies"),
        ("series", "media/tv"),
        ("torrents", "torrents"),
    ):
        total = 0
        stack = [root / relative] if (root / relative).is_dir() else []
        while stack:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if info.st_dev != device:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) not in seen:
                        seen.add((info.st_dev, info.st_ino))
                        total += info.st_blocks * 512
        categories[name + "_bytes"] = total
    known = sum(categories.values())
    if known > used_bytes:
        return dict.fromkeys((*categories, "other_bytes"))
    return {**categories, "other_bytes": used_bytes - known}


def write_atomic(output: Path, payload: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/run/homeserver/host.json"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--loop", action="store_true")
    parser.add_argument(
        "--storage-registry", type=Path, default=Path("/run/homeserver/storage.json")
    )
    args = parser.parse_args()
    settings = None
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "services/common/src"))
    from homeserver_common.storage import StorageRegistryError, load_storage_registry

    if args.env_file:
        from homeserver_common.env import load_settings

        settings = load_settings(args.env_file, mode="prod")
        args.output = Path(settings.run_root) / "host.json"
    interval = (
        settings.metrics_interval_seconds
        if settings
        else float(os.environ.get("HOMESERVER_METRICS_INTERVAL_SECONDS", "5"))
    )
    if not math.isfinite(interval) or interval <= 0:
        parser.error("collection interval must be positive")
    running = True

    def stop(_signal, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        registry_error = None
        try:
            registry = load_storage_registry(args.storage_registry)
        except StorageRegistryError as exc:
            registry = None
            registry_error = str(exc)
        try:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
        media = (
            Path(settings.media_root)
            if settings
            else Path(os.environ.get("HOMESERVER_MEDIA_ROOT", "/srv/data"))
        )
        capacity_ok = registry_error is None
        if registry_error is not None:
            unavailable_pools = [
                {
                    "pool_id": pool_id,
                    "label": label,
                    "filesystem_id": None,
                    "state": "unavailable",
                    "reason": "invalid_storage_registry",
                    "measured_at": time.time(),
                    "total_bytes": None,
                    "used_bytes": None,
                    "free_bytes": None,
                }
                for pool_id, label in (("ssd", "SSD"), ("hdd", "HD USB"))
            ]
            write_atomic(
                args.output.parent / "capacity.json",
                {**unavailable_pools[0], "pools": unavailable_pools},
            )
        if capacity_ok and registry is None and settings and settings.media_uuid:
            guard = subprocess.run(
                ["bash", str(root / "scripts/check-mount.sh"), str(media), settings.media_uuid],
                capture_output=True,
                timeout=15,
            )
            if guard.returncode:
                # Never report a root-filesystem reading as mounted media capacity.
                write_atomic(
                    Path(settings.run_root) / "capacity.json",
                    {"filesystem_id": None, "state": "missing", "measured_at": time.time()},
                )
                capacity_ok = False
        filesystem_id = settings.media_uuid if settings and settings.media_uuid else None
        if registry is None and capacity_ok:
            try:
                volume = os.statvfs(media)
                filesystem_id = filesystem_id or "device:" + str(os.stat(media).st_dev)
                write_atomic(
                    args.output.parent / "capacity.json",
                    {
                        "filesystem_id": filesystem_id,
                        "measured_at": time.time(),
                        "free_bytes": volume.f_bavail * volume.f_frsize,
                        "used_bytes": (volume.f_blocks - volume.f_bfree) * volume.f_frsize,
                        "total_bytes": volume.f_blocks * volume.f_frsize,
                    },
                )
            except OSError:
                write_atomic(
                    args.output.parent / "capacity.json",
                    {"filesystem_id": None, "state": "missing", "measured_at": time.time()},
                )
                capacity_ok = False
        snapshot = collect(
            previous=previous,
            media_path=media,
            network_interface=settings.network_interface if settings else "auto",
            registry=registry,
            filesystem_id=filesystem_id,
            storage_available=capacity_ok,
        )
        if not capacity_ok:
            snapshot["storage"] = None
        if registry_error is not None:
            snapshot["pools"] = unavailable_pools
        if registry is not None:
            ssd = next(pool for pool in snapshot["pools"] if pool["pool_id"] == "ssd")
            write_atomic(
                args.output.parent / "capacity.json",
                {
                    **{key: value for key, value in ssd.items() if key != "storage"},
                    "pools": [
                        {key: value for key, value in pool.items() if key != "storage"}
                        for pool in snapshot["pools"]
                    ],
                },
            )
        write_atomic(args.output, snapshot)
        if not args.loop:
            return 0 if capacity_ok else 4
        time.sleep(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
