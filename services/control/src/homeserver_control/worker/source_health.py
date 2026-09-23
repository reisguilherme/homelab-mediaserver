"""Persist torrent progress observations before considering a source change."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_ACTIVE_DOWNLOAD_STATES = frozenset({"downloading", "stalledDL", "forcedDL"})
_STALLED_SECONDS = 30 * 60
_SLOW_SECONDS = 60 * 60
_SLOW_BYTES_PER_SECOND = 1024 * 1024


@dataclass(frozen=True)
class TorrentHealth:
    infohash: str
    downloaded: int
    amount_left: int
    num_seeds: int
    dlspeed: int
    state: str
    progress: float

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> TorrentHealth:
        infohash = payload.get("hash")
        values = [payload.get(key) for key in (
            "downloaded", "amount_left", "num_seeds", "dlspeed"
        )]
        progress = payload.get("progress")
        state = payload.get("state")
        if (
            not isinstance(infohash, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", infohash) is None
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0
                   for value in values)
            or isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not math.isfinite(progress)
            or not 0 <= progress <= 1
            or not isinstance(state, str)
            or not state
        ):
            raise ValueError("invalid torrent health evidence")
        return cls(infohash.lower(), *values, state, float(progress))


class SourceHealthStore:
    """Measure sustained stall/slow periods across worker process restarts."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS source_health (
                    permit_id TEXT PRIMARY KEY,
                    infohash TEXT NOT NULL,
                    last_amount_left INTEGER NOT NULL,
                    last_progress_at REAL NOT NULL,
                    window_started_at REAL NOT NULL,
                    window_left INTEGER NOT NULL,
                    replacing INTEGER NOT NULL DEFAULT 0
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def observe(self, permit_id: str, health: TorrentHealth, *, now: float) -> str | None:
        if not permit_id or not math.isfinite(now) or now < 0:
            raise ValueError("invalid source observation")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_health WHERE permit_id = ?", (permit_id,)
            ).fetchone()
            if health.amount_left == 0 or health.progress >= 1:
                connection.execute("DELETE FROM source_health WHERE permit_id = ?", (permit_id,))
                return None
            if row is not None and row["replacing"] and (
                health.state in {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}
                or health.amount_left == row["last_amount_left"]
            ):
                return "replacing"
            if health.state not in _ACTIVE_DOWNLOAD_STATES:
                connection.execute("DELETE FROM source_health WHERE permit_id = ?", (permit_id,))
                return None
            if (
                row is None or row["infohash"] != health.infohash
                or health.amount_left > row["last_amount_left"]
                or now < row["window_started_at"]
            ):
                connection.execute("""
                    INSERT INTO source_health(
                        permit_id, infohash, last_amount_left, last_progress_at,
                        window_started_at, window_left, replacing
                    ) VALUES (?, ?, ?, ?, ?, ?, 0)
                    ON CONFLICT(permit_id) DO UPDATE SET
                        infohash = excluded.infohash,
                        last_amount_left = excluded.last_amount_left,
                        last_progress_at = excluded.last_progress_at,
                        window_started_at = excluded.window_started_at,
                        window_left = excluded.window_left,
                        replacing = 0
                """, (permit_id, health.infohash, health.amount_left, now, now,
                      health.amount_left))
                return None
            last_progress_at = (
                now if health.amount_left < row["last_amount_left"]
                else row["last_progress_at"]
            )
            elapsed = now - row["window_started_at"]
            window_started_at = row["window_started_at"]
            window_left = row["window_left"]
            average = (window_left - health.amount_left) / elapsed if elapsed > 0 else 0
            slow = elapsed >= _SLOW_SECONDS and average < _SLOW_BYTES_PER_SECOND
            if elapsed >= _SLOW_SECONDS and not slow:
                window_started_at, window_left = now, health.amount_left
            connection.execute("""
                UPDATE source_health SET last_amount_left = ?, last_progress_at = ?,
                    window_started_at = ?, window_left = ?, replacing = 0
                WHERE permit_id = ?
            """, (health.amount_left, last_progress_at, window_started_at,
                  window_left, permit_id))
            if (
                health.num_seeds == 0 and health.dlspeed == 0
                and now - last_progress_at >= _STALLED_SECONDS
            ):
                return "stalled"
            return "slow" if slow else None

    def mark_replacing(self, permit_id: str) -> None:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE source_health SET replacing = 1 WHERE permit_id = ?", (permit_id,)
            )
            if result.rowcount != 1:
                raise ValueError("source observation is unavailable")

    def clear_replacing(self, permit_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE source_health SET replacing = 0 WHERE permit_id = ?", (permit_id,)
            )
