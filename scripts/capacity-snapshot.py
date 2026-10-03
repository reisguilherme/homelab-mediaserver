#!/usr/bin/env python3
"""Publish media capacity only after the expected writable mount is verified."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


def collect_pools(registry) -> dict[str, object]:
    from homeserver_common.storage import StorageUnavailable

    pools = []
    for pool_id in registry.pool_ids:
        pool = registry.pools[pool_id]
        try:
            sample = registry.inspect(pool_id, writable=False).as_dict()
        except (StorageUnavailable, OSError) as exc:
            sample = {
                "pool_id": pool_id,
                "label": pool.label,
                "filesystem_id": pool.filesystem_id,
                "state": "unavailable",
                "reason": str(exc),
                "measured_at": time.time(),
                "total_bytes": None,
                "used_bytes": None,
                "free_bytes": None,
            }
        pools.append(sample)
    ssd = next(pool for pool in pools if pool["pool_id"] == "ssd")
    return {**ssd, "pools": pools}


def collect(
    expected_uuid: str,
    *,
    guard: Callable[[], None],
    usage: Callable[[], tuple[int, int]],
    clock: Callable[[], float] = time.time,
) -> dict[str, object]:
    if not expected_uuid:
        raise ValueError("media UUID is required")
    guard()
    total, free = usage()
    guard()
    if total <= 0 or free < 0 or free > total:
        raise ValueError("invalid media capacity")
    return {
        "filesystem_id": expected_uuid,
        "total_bytes": total,
        "free_bytes": free,
        "measured_at": clock(),
    }


def refresh(
    output: Path,
    expected_uuid: str,
    *,
    guard: Callable[[], None],
    usage: Callable[[], tuple[int, int]],
) -> None:
    try:
        payload = collect(expected_uuid, guard=guard, usage=usage)
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
    except Exception:
        output.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/run/homeserver/capacity.json"))
    parser.add_argument("--media-path", type=Path, default=Path("/srv/data"))
    parser.add_argument("--media-uuid", default="")
    parser.add_argument(
        "--storage-registry", type=Path, default=Path("/run/homeserver/storage.json")
    )
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services/common/src"))
    from homeserver_common.storage import load_storage_registry

    registry = load_storage_registry(args.storage_registry)
    if registry is not None:
        payload = collect_pools(registry)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{args.output.name}.", dir=args.output.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, args.output)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return 0
    guard_script = Path(__file__).with_name("check-mount.sh")

    def guard() -> None:
        subprocess.run(
            ["bash", str(guard_script), str(args.media_path), args.media_uuid],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def usage() -> tuple[int, int]:
        result = shutil.disk_usage(args.media_path)
        return (result.total, result.free)

    refresh(args.output, args.media_uuid, guard=guard, usage=usage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
