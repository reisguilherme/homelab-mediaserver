"""Publish a small, sanitized projection of measurements already held in memory."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import sys
from datetime import datetime
from pathlib import Path

_POOL_IDS = ("ssd", "hdd", "hdd2", "hdd3")
_MAX_BYTES = 1024 * 1024


def _number(value: object, *, integer: bool = False) -> int | float | None:
    if type(value) not in ((int,) if integer else (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _timestamp(value: object) -> str | int | float | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return value if parsed.tzinfo is not None and parsed.timestamp() >= 0 else None
        except (ValueError, OverflowError, OSError):
            return None
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _section(value: object, fields: tuple[str, ...]) -> dict | None:
    if not isinstance(value, dict):
        return None
    return {key: _number(value.get(key)) for key in fields}


def _pool(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    identity = value.get("filesystem_id")
    digest = None
    if isinstance(identity, str) and identity.strip():
        digest = "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    name, state = value.get("pool_id"), value.get("state")
    return {
        "pool_id": name if name in _POOL_IDS else None,
        "filesystem_id": digest,
        "state": state if state in ("ready", "unavailable") else None,
        "measured_at": _timestamp(value.get("measured_at")),
        **{key: _number(value.get(key), integer=True)
           for key in ("total_bytes", "free_bytes", "used_bytes")},
    }


def _project(host: dict, capacity: dict) -> tuple[dict, dict]:
    pools = capacity.get("pools")
    return {
        "generated_at": _timestamp(host.get("generated_at")),
        "host": _section(host.get("host", {}), (
            "cpu_percent", "ram_percent", "cpu_celsius", "uptime_seconds",
        )),
        "network": _section(host.get("network", {}), ("rx_bps", "tx_bps")),
    }, {
        "measured_at": _timestamp(capacity.get("measured_at")),
        "pools": [_pool(pool) for pool in pools]
        if isinstance(pools, list) and len(pools) <= len(_POOL_IDS) else None,
    }


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


def _write_atomic(directory_fd: int, name: str, content: bytes) -> None:
    try:
        existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(existing.st_mode):
            raise ValueError("snapshot target must be a regular file")
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600, dir_fd=directory_fd,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fchmod(handle.fileno(), 0o640)
            os.fsync(handle.fileno())
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def publish_metrics_snapshots(directory: Path, *, host: dict, capacity: dict) -> bool:
    """Publish fixed files without creating destinations or reading any source files.

    Files inherit their group from the precreated setgid directory. Failure leaves
    the prior evidence to expire naturally and never interrupts the host collector.
    """
    directory_fd = None
    try:
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise ValueError("Linux filesystem guarantees are required")
        payloads = _project(host, capacity)
        encoded = [
            (json.dumps(payload, allow_nan=False, sort_keys=True) + "\n").encode("utf-8")
            for payload in payloads
        ]
        if any(len(payload) > _MAX_BYTES for payload in encoded):
            raise ValueError("snapshot exceeds size limit")
        directory_fd = _open_directory(directory)
        for name, content in zip(("host.json", "capacity.json"), encoded, strict=True):
            _write_atomic(directory_fd, name, content)
        return True
    except (OSError, ValueError, TypeError):
        print("metrics snapshot publication failed", file=sys.stderr)
        return False
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
