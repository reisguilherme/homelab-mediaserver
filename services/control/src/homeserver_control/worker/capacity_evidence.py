"""Fresh filesystem and qBittorrent progress used to admit exact torrent bytes."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


@dataclass(frozen=True)
class CapacityEvidence:
    free_bytes: int
    remaining_by_hash: dict[str, int]
    other_pending_bytes: int = 0
    paused_hashes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if (
            isinstance(self.free_bytes, bool)
            or not isinstance(self.free_bytes, int)
            or self.free_bytes < 0
        ):
            raise ValueError("invalid filesystem free bytes")
        if any(
            not isinstance(key, str) or len(key) != 40
            or isinstance(value, bool) or not isinstance(value, int) or value < 0
            for key, value in self.remaining_by_hash.items()
        ):
            raise ValueError("invalid torrent progress")
        if (isinstance(self.other_pending_bytes, bool)
                or not isinstance(self.other_pending_bytes, int)
                or self.other_pending_bytes < 0):
            raise ValueError("invalid unmanaged queue size")
        if any(not isinstance(item, str) or len(item) != 40 for item in self.paused_hashes):
            raise ValueError("invalid stopped torrent identity")


async def read_capacity_evidence(
    *, snapshot_path: Path, data_root: Path, gateway_url: str,
    arr_token: str, client: httpx.AsyncClient,
) -> CapacityEvidence:
    """Read current free bytes and trusted remaining bytes of admitted torrents."""
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    measured = snapshot.get("measured_at")
    if (
        not snapshot.get("filesystem_id")
        or not isinstance(measured, (int, float))
        or not 0 <= time.time() - measured <= 30
        or not os.path.ismount(data_root)
    ):
        raise ValueError("filesystem_snapshot_unavailable")
    response = await client.get(
        f"{gateway_url.rstrip('/')}/internal/queue-capacity",
        headers={"X-Arr-Token": arr_token},
    )
    response.raise_for_status()
    payload = response.json()
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
        if admitted and stopped:
            paused.add(infohash.lower())
        known = (isinstance(total, int) and not isinstance(total, bool) and total > 0
                 and isinstance(left, int) and not isinstance(left, bool) and 0 <= left <= total)
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
    return CapacityEvidence(free_bytes=free_bytes, remaining_by_hash=remaining,
                            other_pending_bytes=other_pending,
                            paused_hashes=frozenset(paused))
