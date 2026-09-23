"""Read-only host status assembled from host snapshots and a cached disk scan."""

from __future__ import annotations

import json
import math
import os
import stat
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _number(value: object, *, maximum: float | None = None) -> int | float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _timestamp(value: object) -> float | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return _number(value)


def _state(timestamp: float | None, *, now: float, fresh_seconds: float) -> str:
    if timestamp is None or timestamp > now + 5:
        return "unavailable"
    return "ok" if now - timestamp <= fresh_seconds else "stale"


def _allocated_bytes(root: Path, *, device: int, seen: set[tuple[int, int]]) -> int:
    if not root.exists():
        return 0
    total = 0
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if info.st_dev != device:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        identity = (info.st_dev, info.st_ino)
                        if identity not in seen:
                            seen.add(identity)
                            total += info.st_blocks * 512
        except FileNotFoundError:
            continue
    return total


class StorageBreakdown:
    """Scan media paths off the request thread, at most once per refresh window."""

    def __init__(self, root: Path, *, refresh_seconds: float = 300) -> None:
        self.root = root
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._sample: dict[str, int] | None = None
        self._measured_at: float | None = None
        self._running = False
        self._error = False

    def _measure(self) -> dict[str, int]:
        device = self.root.stat().st_dev
        seen: set[tuple[int, int]] = set()
        paths = {
            "movies_bytes": self.root / "media" / "movies",
            "series_bytes": self.root / "media" / "tv",
            "torrents_bytes": self.root / "torrents",
        }
        return {
            name: _allocated_bytes(path, device=device, seen=seen) for name, path in paths.items()
        }

    def _refresh(self) -> None:
        try:
            sample = self._measure()
            measured_at = time.time()
        except OSError:
            with self._lock:
                self._error = True
                self._running = False
            return
        with self._lock:
            self._sample = sample
            self._measured_at = measured_at
            self._error = False
            self._running = False

    def snapshot(self, *, used_bytes: int | None) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            due = self._measured_at is None or now - self._measured_at > self.refresh_seconds
            if due and not self._running and self.root.is_dir():
                self._running = True
                threading.Thread(target=self._refresh, daemon=True, name="storage-scan").start()
            sample = self._sample
            measured_at = self._measured_at
            running = self._running
            error = self._error
        if used_bytes is None:
            return {
                "state": "calculating" if running and sample is None else "unavailable",
                "measured_at": None,
                "movies_bytes": None,
                "series_bytes": None,
                "torrents_bytes": None,
                "other_bytes": None,
            }
        if sample is None:
            state = "calculating" if running else "unavailable"
            return {
                "state": state,
                "measured_at": None,
                "movies_bytes": None,
                "series_bytes": None,
                "torrents_bytes": None,
                "other_bytes": None,
            }
        age = now - measured_at if measured_at is not None else float("inf")
        state = "unavailable" if error else "stale" if age > self.refresh_seconds else "ok"
        known = sum(sample.values())
        if used_bytes is not None and known > used_bytes:
            return {
                "state": "unavailable",
                "measured_at": datetime.fromtimestamp(measured_at, UTC).isoformat(),
                "movies_bytes": None,
                "series_bytes": None,
                "torrents_bytes": None,
                "other_bytes": None,
            }
        return {
            "state": state,
            "measured_at": datetime.fromtimestamp(measured_at, UTC).isoformat(),
            **sample,
            "other_bytes": max(used_bytes - known, 0) if used_bytes is not None else None,
        }


class StatusProvider:
    def __init__(self, *, host_path: Path, capacity_path: Path, media_root: Path) -> None:
        self.host_path = host_path
        self.capacity_path = capacity_path
        self.storage = StorageBreakdown(media_root)

    def __call__(self) -> dict[str, Any]:
        now = time.time()
        host_snapshot = _read_json(self.host_path) or {}
        host_timestamp = _timestamp(host_snapshot.get("generated_at"))
        host_state = _state(host_timestamp, now=now, fresh_seconds=45)
        host = host_snapshot.get("host") if host_state != "unavailable" else None
        network = host_snapshot.get("network") if host_state != "unavailable" else None
        host = host if isinstance(host, dict) else {}
        network = network if isinstance(network, dict) else {}

        capacity_snapshot = _read_json(self.capacity_path) or {}
        capacity_timestamp = _timestamp(capacity_snapshot.get("measured_at"))
        capacity_state = _state(capacity_timestamp, now=now, fresh_seconds=30)
        total = _number(capacity_snapshot.get("total_bytes"))
        free = _number(capacity_snapshot.get("free_bytes"))
        if (
            capacity_state == "unavailable"
            or not capacity_snapshot.get("filesystem_id")
            or type(total) is not int
            or type(free) is not int
            or total <= 0
            or free > total
        ):
            capacity_state = "unavailable"
            total = free = used = None
        else:
            used = None
            host_storage = host_snapshot.get("storage")
            if host_state == "ok" and capacity_state == "ok" and isinstance(host_storage, dict):
                host_total = _number(host_storage.get("total_bytes"))
                host_used = _number(host_storage.get("used_bytes"))
                if type(host_total) is int and host_total == total and type(host_used) is int:
                    if host_used <= total:
                        used = host_used

        interface = network.get("interface")
        return {
            "generated_at": datetime.fromtimestamp(now, UTC).isoformat(),
            "host": {
                "state": host_state,
                "measured_at": (
                    datetime.fromtimestamp(host_timestamp, UTC).isoformat()
                    if host_state != "unavailable"
                    else None
                ),
                "cpu_percent": _number(host.get("cpu_percent"), maximum=100),
                "ram_percent": _number(host.get("ram_percent"), maximum=100),
                "cpu_celsius": _number(host.get("cpu_celsius")),
                "uptime_seconds": _number(host.get("uptime_seconds")),
            },
            "network": {
                "interface": interface if isinstance(interface, str) else None,
                "rx_bps": _number(network.get("rx_bps")),
                "tx_bps": _number(network.get("tx_bps")),
            },
            "capacity": {
                "state": capacity_state,
                "measured_at": (
                    datetime.fromtimestamp(capacity_timestamp, UTC).isoformat()
                    if capacity_state != "unavailable"
                    else None
                ),
                "total_bytes": total,
                "free_bytes": free,
                "used_bytes": used,
            },
            "storage": self.storage.snapshot(used_bytes=used),
        }
