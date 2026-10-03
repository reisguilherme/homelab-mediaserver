"""Fresh filesystem and qBittorrent progress used to admit exact torrent bytes."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx


@dataclass(frozen=True)
class CapacityEvidence:
    free_bytes: int
    remaining_by_hash: dict[str, int]
    other_pending_bytes: int = 0
    paused_hashes: frozenset[str] = frozenset()
    pools: tuple[PoolCapacityEvidence, ...] = ()

    def pool(self, pool_id: str = "ssd") -> CapacityEvidence:
        if not self.pools:
            if pool_id != "ssd":
                raise ValueError("pool_capacity_unavailable")
            return self
        for pool in self.pools:
            if pool.pool_id == pool_id:
                return CapacityEvidence(
                    pool.free_bytes,
                    pool.remaining_by_hash,
                    pool.other_pending_bytes,
                    pool.paused_hashes,
                    (pool,),
                )
        raise ValueError("pool_capacity_unavailable")

    def __post_init__(self) -> None:
        if (
            isinstance(self.free_bytes, bool)
            or not isinstance(self.free_bytes, int)
            or self.free_bytes < 0
        ):
            raise ValueError("invalid filesystem free bytes")
        if any(
            not isinstance(key, str)
            or len(key) != 40
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.remaining_by_hash.items()
        ):
            raise ValueError("invalid torrent progress")
        if (
            isinstance(self.other_pending_bytes, bool)
            or not isinstance(self.other_pending_bytes, int)
            or self.other_pending_bytes < 0
        ):
            raise ValueError("invalid unmanaged queue size")
        if any(not isinstance(item, str) or len(item) != 40 for item in self.paused_hashes):
            raise ValueError("invalid stopped torrent identity")
        if len({pool.pool_id for pool in self.pools}) != len(self.pools):
            raise ValueError("duplicate capacity pool")


@dataclass(frozen=True)
class PoolCapacityEvidence:
    pool_id: str
    filesystem_id: str
    free_bytes: int
    remaining_by_hash: dict[str, int] = field(default_factory=dict)
    other_pending_bytes: int = 0
    paused_hashes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.pool_id not in {"ssd", "hdd"} or not self.filesystem_id:
            raise ValueError("invalid capacity pool identity")
        CapacityEvidence(
            self.free_bytes, self.remaining_by_hash, self.other_pending_bytes, self.paused_hashes
        )


async def read_capacity_evidence(
    *,
    snapshot_path: Path,
    data_root: Path,
    gateway_url: str,
    arr_token: str,
    client: httpx.AsyncClient,
    expected_filesystem_id: str | None = None,
    max_age_seconds: float = 30,
    storage_registry=None,
) -> CapacityEvidence:
    """Read current free bytes and trusted remaining bytes of admitted torrents."""
    response = await client.get(
        f"{gateway_url.rstrip('/')}/internal/queue-capacity",
        headers={"X-Arr-Token": arr_token},
    )
    response.raise_for_status()
    payload = response.json()
    return capacity_from_queue(
        snapshot_path=snapshot_path,
        data_root=data_root,
        payload=payload,
        expected_filesystem_id=expected_filesystem_id,
        max_age_seconds=max_age_seconds,
        storage_registry=storage_registry,
    )


def capacity_from_queue(
    *,
    snapshot_path: Path,
    data_root: Path,
    payload,
    expected_filesystem_id: str | None = None,
    max_age_seconds: float = 30,
    storage_registry=None,
) -> CapacityEvidence:
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if storage_registry is not None:
        return _registered_capacity(snapshot, payload, storage_registry, max_age_seconds)
    measured = snapshot.get("measured_at")
    if (
        not snapshot.get("filesystem_id")
        or isinstance(measured, bool)
        or not isinstance(measured, (int, float))
        or not 0 <= time.time() - measured <= max_age_seconds
        or expected_filesystem_id is not None
        and snapshot.get("filesystem_id") != expected_filesystem_id
        or not os.path.ismount(data_root)
    ):
        raise ValueError("filesystem_snapshot_unavailable")
    if not isinstance(payload, list):
        raise ValueError("gateway_queue_unavailable")
    remaining: dict[str, int] = {}
    other_pending = 0
    paused: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("invalid gateway queue entry")
        infohash = item.get("hash")
        total = item.get("total_size")
        left = item.get("amount_left")
        admitted = item.get("admitted")
        stopped = item.get("state") in {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}
        if not isinstance(infohash, str) or len(infohash) != 40 or not isinstance(admitted, bool):
            raise ValueError("invalid gateway queue identity")
        # Historical superseded sources are not admitted for download, but
        # their stopped state is still required before another safe failover.
        if stopped:
            paused.add(infohash.lower())
        known = (
            isinstance(total, int)
            and not isinstance(total, bool)
            and total > 0
            and isinstance(left, int)
            and not isinstance(left, bool)
            and 0 <= left <= total
        )
        if not known:
            if not admitted:
                raise ValueError("unmanaged torrent size is unknown")
            continue
        if admitted:
            remaining[infohash.lower()] = left
        elif not stopped:
            other_pending += left
    stats = os.statvfs(data_root)
    free_bytes = stats.f_bavail * stats.f_frsize
    return CapacityEvidence(
        free_bytes=free_bytes,
        remaining_by_hash=remaining,
        other_pending_bytes=other_pending,
        paused_hashes=frozenset(paused),
    )


def _registered_capacity(snapshot, payload, registry, max_age_seconds):
    if not isinstance(payload, list) or not isinstance(snapshot.get("pools"), list):
        raise ValueError("filesystem_snapshot_unavailable")
    for entry in payload:
        if not isinstance(entry, dict):
            raise ValueError("invalid gateway queue entry")
        if entry.get("pool_id") not in registry.pool_ids:
            # Unattributed commitments could consume either disk.
            raise ValueError("unverified gateway queue placement")
    pools = []
    for pool_id in registry.pool_ids:
        candidates = [
            item
            for item in snapshot["pools"]
            if isinstance(item, dict) and item.get("pool_id") == pool_id
        ]
        if len(candidates) != 1:
            continue
        item = candidates[0]
        measured = item.get("measured_at")
        try:
            measured = (
                datetime.fromisoformat(measured.replace("Z", "+00:00")).timestamp()
                if isinstance(measured, str)
                else measured
            )
            if (
                isinstance(measured, bool)
                or not isinstance(measured, (int, float))
                or not 0 <= time.time() - measured <= max_age_seconds
                or item.get("state") != "ready"
            ):
                continue
            sample = registry.inspect(pool_id)
            if item.get("filesystem_id") != sample.filesystem_id:
                continue
        except (RuntimeError, ValueError, OSError):
            continue
        remaining = {}
        paused = set()
        other = {}
        queue_valid = True
        for entry in payload:
            if entry["pool_id"] != pool_id:
                continue
            infohash = entry.get("hash")
            if (
                not isinstance(infohash, str)
                or len(infohash) != 40
                or not isinstance(entry.get("admitted"), bool)
            ):
                queue_valid = False
                break
            if (
                entry.get("filesystem_id") != sample.filesystem_id
                or entry.get("placement_verified") is not True
            ):
                queue_valid = False
                break
            total, left = entry.get("total_size"), entry.get("amount_left")
            if entry.get("state") in {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}:
                paused.add(infohash.lower())
            known = (
                isinstance(total, int)
                and not isinstance(total, bool)
                and total > 0
                and isinstance(left, int)
                and not isinstance(left, bool)
                and 0 <= left <= total
            )
            if not known:
                if not entry["admitted"]:
                    queue_valid = False
                    break
                reserved = entry.get("reserved_bytes")
                if isinstance(reserved, bool) or not isinstance(reserved, int) or reserved <= 0:
                    queue_valid = False
                    break
                remaining[infohash.lower()] = reserved
                continue
            target = remaining if entry["admitted"] else other
            target[infohash.lower()] = max(target.get(infohash.lower(), 0), left)
        if not queue_valid:
            continue
        pools.append(
            PoolCapacityEvidence(
                pool_id,
                sample.filesystem_id,
                sample.free_bytes,
                remaining,
                sum(other.values()),
                frozenset(paused),
            )
        )
    if not pools:
        raise ValueError("filesystem_snapshot_unavailable")
    ssd = next((pool for pool in pools if pool.pool_id == "ssd"), None)
    return CapacityEvidence(
        ssd.free_bytes if ssd else 0,
        {key: value for pool in pools for key, value in pool.remaining_by_hash.items()},
        ssd.other_pending_bytes if ssd else 0,
        frozenset(key for pool in pools for key in pool.paused_hashes),
        tuple(pools),
    )
