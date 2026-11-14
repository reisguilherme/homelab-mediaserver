"""Private, read-only Prometheus exporter for two bounded SSD snapshots.

No live providers are imported: collecting a scrape must never inspect media,
open controller state, or need application credentials.
"""

from __future__ import annotations

import json
import math
import os
import stat
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Response

_MAX_BYTES = 1024 * 1024
_POOL_IDS = frozenset({"ssd", "hdd", "hdd2", "hdd3"})
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_SOURCES = (("host", "generated_at", 90), ("capacity", "measured_at", 30))


def _finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _open_directory(directory: Path) -> int:
    parts = directory.absolute().parts
    if ".." in parts:
        raise ValueError("parent traversal is not a snapshot directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(parts[0], flags)
    try:
        for part in parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_snapshot(directory: Path, filename: str) -> dict | None:
    """Open only a fixed basename, reject symlinks/special files, cap actual reads."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        return None  # Linux runtime guarantees are required; never silently follow links.
    directory_fd = file_fd = None
    try:
        directory_fd = _open_directory(directory)
        file_fd = os.open(
            filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd,
        )
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_BYTES:
            return None
        chunks = []
        remaining = _MAX_BYTES
        while remaining:
            chunk = os.read(file_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(file_fd)
        raw = b"".join(chunks)
        if (len(raw) != after.st_size or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            return None  # Publishers must replace snapshots atomically.
        payload = json.loads(
            raw.decode("utf-8"), parse_float=_json_float,
            parse_constant=_invalid_constant, object_pairs_hook=_json_object,
        )
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, RecursionError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _age(timestamp: object, now: float) -> float | None:
    if isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                return None
            timestamp = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if not _finite_number(timestamp) or not _finite_number(now):
        return None
    if timestamp < 0 or timestamp > now + 5:
        return None
    return max(0.0, now - timestamp)


def _pool_collection(snapshot: dict) -> list[dict] | None:
    pools = snapshot.get("pools")
    if not isinstance(pools, list) or len(pools) > len(_POOL_IDS):
        return None
    names, identities = set(), set()
    for pool in pools:
        if not isinstance(pool, dict):
            return None
        name, identity = pool.get("pool_id"), pool.get("filesystem_id")
        if not isinstance(name, str) or name not in _POOL_IDS or name in names:
            return None
        names.add(name)
        if isinstance(identity, str) and identity.strip():
            if identity in identities:
                return None
            identities.add(identity)
    return pools


def _emit(lines: list[str], name: str, value: int | float, label: str = "") -> None:
    lines.append(f"{name}{label} {value}")


def _host_metrics(lines: list[str], snapshot: dict) -> None:
    host = snapshot.get("host", {})
    network = snapshot.get("network", {})
    fields = (
        (host, "cpu_percent", "homeserver_host_cpu_percent", 0, 100),
        (host, "ram_percent", "homeserver_host_ram_percent", 0, 100),
        (host, "cpu_celsius", "homeserver_host_cpu_celsius", -273.15, None),
        (host, "uptime_seconds", "homeserver_host_uptime_seconds", 0, None),
        (network, "rx_bps", "homeserver_network_receive_bytes_per_second", 0, None),
        (network, "tx_bps", "homeserver_network_transmit_bytes_per_second", 0, None),
    )
    for section, key, metric, minimum, maximum in fields:
        value = section.get(key)
        if (_finite_number(value) and value >= minimum
                and (maximum is None or value <= maximum)):
            _emit(lines, metric, value)


def _pool_metrics(lines: list[str], pools: list[dict], now: float) -> None:
    for pool in pools:
        identity, state = pool.get("filesystem_id"), pool.get("state")
        age = _age(pool.get("measured_at"), now)
        if (not isinstance(identity, str) or not identity.strip()
                or state not in ("ready", "unavailable") or age is None or age > 30):
            continue
        label = f'{{pool="{pool["pool_id"]}"}}'
        if state == "unavailable":
            _emit(lines, "homeserver_pool_ready", 0, label)
            continue
        total, free, used = (pool.get(f"{key}_bytes") for key in ("total", "free", "used"))
        if not all(type(value) is int and _finite_number(value) for value in (total, free, used)):
            continue
        # Filesystem-reserved blocks need not be part of used or available bytes.
        if total <= 0 or free < 0 or used < 0 or used + free > total:
            continue
        _emit(lines, "homeserver_pool_ready", 1, label)
        for key, value in (("total", total), ("free", free), ("used", used)):
            _emit(lines, f"homeserver_pool_{key}_bytes", value, label)


def _render(directory: Path, now: float) -> str:
    lines = []
    for source, timestamp_key, max_age in _SOURCES:
        snapshot = _read_snapshot(directory, f"{source}.json")
        age = _age(snapshot.get(timestamp_key), now) if snapshot is not None else None
        pools = (
            _pool_collection(snapshot) if snapshot is not None and source == "capacity" else None
        )
        shape_valid = snapshot is not None
        if snapshot is not None and source == "host":
            shape_valid = all(
                isinstance(snapshot.get(key, {}), dict) for key in ("host", "network")
            )
        elif source == "capacity":
            shape_valid = pools is not None
        valid = shape_valid and age is not None
        fresh = valid and age <= max_age
        label = f'{{source="{source}"}}'
        _emit(lines, "homeserver_snapshot_valid", int(valid), label)
        _emit(lines, "homeserver_snapshot_fresh", int(fresh), label)
        if age is not None:
            _emit(lines, "homeserver_snapshot_age_seconds", age, label)
        if fresh:
            if source == "host":
                _host_metrics(lines, snapshot)
            else:
                _pool_metrics(lines, pools, now)
    # Prometheus requires each family to be contiguous, with metadata first.
    families = {}
    for line in lines:
        name = line.split(" ", 1)[0].split("{", 1)[0]
        families.setdefault(name, []).append(line)
    output = []
    for name, samples in families.items():
        output.append(f"# TYPE {name} gauge")
        output.extend(samples)
    return "\n".join(output) + "\n"


def create_metrics_app(
    *, snapshot_dir: Path | None = None, clock: Callable[[], float] = time.time,
) -> FastAPI:
    directory = snapshot_dir if snapshot_dir is not None else Path(
        os.environ.get("HOMESERVER_METRICS_SNAPSHOT_DIR", "/run/homeserver-metrics"),
    )
    metrics_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @metrics_app.get("/metrics")
    def metrics() -> Response:
        return Response(
            _render(directory, clock()),
            headers={"Content-Type": _CONTENT_TYPE, "Cache-Control": "no-store"},
        )

    @metrics_app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return metrics_app


app = create_metrics_app()
