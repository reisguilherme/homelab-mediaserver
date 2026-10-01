"""Durable, idempotent Jellyfin deletion jobs and acquisition tombstones."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from homeserver_control.persistence.db import ReservationRepository

_STAGES = {
    "queued",
    "validated",
    "tombstoned",
    "arr_removed",
    "torrents_removed",
    "seerr_removed",
    "jellyfin_removed",
    "complete",
    "blocked",
}
_TERMINAL_STAGES = {"complete", "blocked"}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class DeletionJobStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def initialize(self) -> None:
        ReservationRepository(self.path).initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, object]:
        return {
            "item_id": row["item_id"],
            "item_type": row["item_type"],
            "payload": json.loads(row["payload_json"]),
            "stage": row["stage"],
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def enqueue(self, item_id: str, item_type: str, payload: dict) -> dict[str, object]:
        """Keep the first preflight capture when a Jellyfin delete is retried."""
        now = _now()
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT OR IGNORE INTO deletion_jobs
                (item_id, item_type, payload_json, stage, created_at, updated_at)
                VALUES (?, ?, ?, 'queued', ?, ?)""",
                (item_id, item_type, payload_json, now, now),
            )
            row = connection.execute(
                "SELECT * FROM deletion_jobs WHERE item_id = ?", (item_id,)
            ).fetchone()
            assert row is not None
            return self._decode(row)

    def enqueue_season(self, item_id: str, payload: dict) -> dict[str, object]:
        """Persist a captured season and its children together, or admit none."""
        now = _now()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM deletion_jobs WHERE item_id = ?", (item_id,)
            ).fetchone()
            if existing is not None:
                if existing["item_type"] != "Season":
                    raise ValueError("season item already belongs to another deletion")
                return self._decode(existing)
            episodes = payload.get("episodes")
            if not isinstance(episodes, list) or not episodes:
                raise ValueError("season has no captured episodes")
            children: list[tuple[str, str]] = []
            seen = {item_id}
            for child in episodes:
                if not isinstance(child, dict) or set(child) != {"item_id", "payload"}:
                    raise ValueError("season child capture is incomplete")
                child_id, child_payload = child["item_id"], child["payload"]
                if (
                    not isinstance(child_id, str) or not child_id or child_id in seen
                    or not isinstance(child_payload, dict)
                    or child_payload.get("parent_item_id") != item_id
                ):
                    raise ValueError("season child identity is invalid")
                seen.add(child_id)
                encoded = json.dumps(child_payload, sort_keys=True, separators=(",", ":"))
                existing_child = connection.execute(
                    "SELECT * FROM deletion_jobs WHERE item_id = ?", (child_id,)
                ).fetchone()
                if existing_child is not None and (
                    existing_child["item_type"] != "Episode"
                    or existing_child["payload_json"] != encoded
                    or existing_child["stage"] in _TERMINAL_STAGES
                ):
                    raise ValueError("captured episode already has a different deletion")
                children.append((child_id, encoded))
            connection.execute(
                """INSERT INTO deletion_jobs
                (item_id, item_type, payload_json, stage, created_at, updated_at)
                VALUES (?, 'Season', ?, 'queued', ?, ?)""",
                (item_id, json.dumps(payload, sort_keys=True, separators=(",", ":")), now, now),
            )
            for child_id, encoded in children:
                connection.execute(
                    """INSERT OR IGNORE INTO deletion_jobs
                    (item_id, item_type, payload_json, stage, created_at, updated_at)
                    VALUES (?, 'Episode', ?, 'queued', ?, ?)""",
                    (child_id, encoded, now, now),
                )
            row = connection.execute(
                "SELECT * FROM deletion_jobs WHERE item_id = ?", (item_id,)
            ).fetchone()
            assert row is not None
            return self._decode(row)

    def get(self, item_id: str) -> dict[str, object] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM deletion_jobs WHERE item_id = ?", (item_id,)
            ).fetchone()
        return self._decode(row) if row is not None else None

    def list(self) -> list[dict[str, object]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM deletion_jobs ORDER BY created_at, rowid"
            ).fetchall()
        return [self._decode(row) for row in rows]

    def next_queued(self) -> dict[str, object] | None:
        """Return the least recently attempted resumable job."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT * FROM deletion_jobs WHERE stage NOT IN (?, ?)
                ORDER BY updated_at, created_at, rowid LIMIT 1""",
                tuple(_TERMINAL_STAGES),
            ).fetchone()
        return self._decode(row) if row is not None else None

    def touch(self, item_id: str) -> None:
        """Rotate a retrying job behind other pending deletions."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE deletion_jobs SET updated_at = ? WHERE item_id = ?",
                (_now(), item_id),
            )

    def set_stage(
        self, item_id: str, stage: str, error: str | None = None
    ) -> dict[str, object]:
        if stage not in _STAGES:
            raise ValueError(f"invalid deletion stage: {stage}")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM deletion_jobs WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(item_id)
            if row["stage"] == stage and row["error"] == error:
                return self._decode(row)
            connection.execute(
                """UPDATE deletion_jobs SET stage = ?, error = ?, updated_at = ?
                WHERE item_id = ?""",
                (stage, error, _now(), item_id),
            )
            updated = connection.execute(
                "SELECT * FROM deletion_jobs WHERE item_id = ?", (item_id,)
            ).fetchone()
            assert updated is not None
            return self._decode(updated)

    def tombstone(self, media_key: str, source_generation: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO tombstones(media_key, deleted_at, source_generation)
                VALUES (?, ?, ?)""",
                (media_key, _now(), source_generation),
            )

    def is_tombstoned(self, media_key: str) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT 1 FROM tombstones WHERE media_key = ?", (media_key,)
            ).fetchone()
        return row is not None
