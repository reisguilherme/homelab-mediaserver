"""Persist torrent progress observations before considering a source change."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_ACTIVE_DOWNLOAD_STATES = frozenset({"downloading", "stalledDL", "forcedDL"})
_STALLED_SECONDS = 5 * 60
_SLOW_SECONDS = 5 * 60
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
        values = [payload.get(key) for key in ("downloaded", "amount_left", "num_seeds", "dlspeed")]
        progress = payload.get("progress")
        state = payload.get("state")
        if (
            not isinstance(infohash, str)
            or re.fullmatch(r"[0-9a-fA-F]{40}", infohash) is None
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in values
            )
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

    def __init__(
        self,
        db_path: str | Path,
        *,
        slow_seconds: int = _SLOW_SECONDS,
        slow_bytes_per_second: int = _SLOW_BYTES_PER_SECOND,
        stalled_seconds: int = _STALLED_SECONDS,
        probe_seconds: int = 60,
        min_eta_gain: float = 0.2,
        slow_replacement_enabled: bool = True,
    ) -> None:
        if (
            not isinstance(slow_replacement_enabled, bool)
            or
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
                for value in (slow_seconds, stalled_seconds, probe_seconds)
            )
            or isinstance(slow_bytes_per_second, bool)
            or not isinstance(slow_bytes_per_second, (int, float))
            or not math.isfinite(slow_bytes_per_second)
            or slow_bytes_per_second < 0
            or isinstance(min_eta_gain, bool)
            or not isinstance(min_eta_gain, (int, float))
            or not math.isfinite(min_eta_gain)
            or not 0 <= min_eta_gain < 1
        ):
            raise ValueError("invalid source health policy")
        self.slow_seconds = slow_seconds
        self.slow_bytes_per_second = slow_bytes_per_second
        self.stalled_seconds = stalled_seconds
        self.probe_seconds = probe_seconds
        self.min_eta_gain = min_eta_gain
        self.slow_replacement_enabled = slow_replacement_enabled
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
            connection.execute("""CREATE TABLE IF NOT EXISTS protected_sources (
                infohash TEXT PRIMARY KEY, reason TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS source_probes (
                candidate_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                started_at REAL NOT NULL, old_left INTEGER NOT NULL,
                new_left INTEGER NOT NULL, decision TEXT NOT NULL)""")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(source_probes)")
            }
            if "last_observed_at" not in columns:
                connection.execute("ALTER TABLE source_probes ADD COLUMN last_observed_at REAL")

    def protect_source(self, infohash: str, reason: str = "operator exception") -> None:
        if re.fullmatch(r"[0-9a-fA-F]{40}", infohash) is None:
            raise ValueError("invalid protected source")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO protected_sources VALUES (?, ?)", (infohash.lower(), reason)
            )

    def is_protected(self, infohash: str) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM protected_sources WHERE infohash = ?", (infohash.lower(),)
                ).fetchone()
                is not None
            )

    def probe_decision(
        self,
        parent_id: str,
        candidate_id: str,
        old: TorrentHealth,
        new: TorrentHealth,
        *,
        now: float,
    ) -> str:
        """Measure both transfers over the same durable window, including start cost.

        A seed count is never evidence of faster transfer. A larger candidate must
        finish sooner as well as transfer faster. Completion of the original wins.
        """
        if not parent_id or not candidate_id or not math.isfinite(now) or now < 0:
            raise ValueError("invalid probe observation")
        if old.amount_left <= 0 or old.progress >= 1 or self.is_protected(old.infohash):
            return "reject"
        if not self.slow_replacement_enabled and (old.num_seeds > 0 or old.dlspeed > 0):
            return "reject"
        if old.state not in _ACTIVE_DOWNLOAD_STATES:
            return "reject"
        if old.dlspeed >= self.slow_bytes_per_second and self.slow_bytes_per_second > 0:
            return "reject"
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_probes WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            fetching_metadata = new.state in {"metaDL", "forcedMetaDL"}
            starting = fetching_metadata or (
                new.amount_left > 0 and new.dlspeed == 0 and new.downloaded == 0
            )
            if (
                new.state not in _ACTIVE_DOWNLOAD_STATES
                and new.amount_left > 0
                and not fetching_metadata
            ):
                # Waiting for a queue slot or an operator resume is not swarm failure.
                connection.execute(
                    "DELETE FROM source_probes WHERE candidate_id=?", (candidate_id,)
                )
                return "observing"
            if row is not None:
                if row["parent_id"] != parent_id or now < row["started_at"]:
                    raise ValueError("probe identity or clock changed")
                if row["decision"] not in {"starting", "observing"}:
                    return row["decision"]
            if (
                row is not None
                and row["decision"] != "starting"
                and row["last_observed_at"] is not None
                and (now - row["last_observed_at"] > self.probe_seconds * 2)
            ):
                connection.execute(
                    "DELETE FROM source_probes WHERE candidate_id=?", (candidate_id,)
                )
                row = None
            if row is None:
                connection.execute(
                    "INSERT INTO source_probes(candidate_id,parent_id,started_at,"
                    "old_left,new_left,decision,last_observed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate_id,
                        parent_id,
                        now,
                        old.amount_left,
                        new.amount_left,
                        "starting" if starting else "observing",
                        now,
                    ),
                )
                return "observing"
            elapsed = now - row["started_at"]
            connection.execute(
                "UPDATE source_probes SET last_observed_at=? WHERE candidate_id=?",
                (now, candidate_id),
            )
            if row["decision"] == "starting":
                if starting:
                    decision = "reject" if elapsed >= self.stalled_seconds else "starting"
                    connection.execute(
                        "UPDATE source_probes SET decision=? WHERE candidate_id=?",
                        (decision, candidate_id),
                    )
                    return "reject" if decision == "reject" else "observing"
                # The same window includes startup cost once payload starts moving.
                connection.execute(
                    "UPDATE source_probes SET decision='observing' WHERE candidate_id=?",
                    (candidate_id,),
                )
            if elapsed < self.probe_seconds:
                return "observing"
            old_rate = max(0, (row["old_left"] - old.amount_left) / elapsed)
            new_rate = max(0, (row["new_left"] - new.amount_left) / elapsed)
            if old_rate >= self.slow_bytes_per_second and self.slow_bytes_per_second > 0:
                connection.execute(
                    "UPDATE source_probes SET decision='reject' WHERE candidate_id=?",
                    (candidate_id,),
                )
                return "reject"
            # Stalled sources have no finite ETA; verified candidate progress is enough.
            old_eta = old.amount_left / old_rate if old_rate > 0 else math.inf
            new_eta = new.amount_left / new_rate if new_rate > 0 else math.inf
            decision = (
                "promote"
                if new_rate > old_rate
                and new_rate > 0
                and new_eta <= old_eta * (1 - self.min_eta_gain)
                else "reject"
            )
            connection.execute(
                "UPDATE source_probes SET decision = ? WHERE candidate_id = ?",
                (decision, candidate_id),
            )
            return decision

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
            if (
                row is not None
                and row["replacing"]
                and (
                    health.state in {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}
                    or health.amount_left == row["last_amount_left"]
                )
            ):
                return "replacing"
            if health.state not in _ACTIVE_DOWNLOAD_STATES:
                connection.execute("DELETE FROM source_health WHERE permit_id = ?", (permit_id,))
                return None
            if (
                row is None
                or row["infohash"] != health.infohash
                or health.amount_left > row["last_amount_left"]
                or now < row["window_started_at"]
            ):
                connection.execute(
                    """
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
                """,
                    (permit_id, health.infohash, health.amount_left, now, now, health.amount_left),
                )
                return None
            last_progress_at = (
                now if health.amount_left < row["last_amount_left"] else row["last_progress_at"]
            )
            elapsed = now - row["window_started_at"]
            window_started_at = row["window_started_at"]
            window_left = row["window_left"]
            average = (window_left - health.amount_left) / elapsed if elapsed > 0 else 0
            slow = (
                self.slow_replacement_enabled
                and elapsed >= self.slow_seconds and average < self.slow_bytes_per_second
            )
            if elapsed >= self.slow_seconds and not slow:
                window_started_at, window_left = now, health.amount_left
            connection.execute(
                """
                UPDATE source_health SET last_amount_left = ?, last_progress_at = ?,
                    window_started_at = ?, window_left = ?, replacing = 0
                WHERE permit_id = ?
            """,
                (health.amount_left, last_progress_at, window_started_at, window_left, permit_id),
            )
            if (
                health.num_seeds == 0
                and health.dlspeed == 0
                and now - last_progress_at >= self.stalled_seconds
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
