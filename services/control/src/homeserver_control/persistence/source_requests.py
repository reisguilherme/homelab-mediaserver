"""Durable operator requests bound to the precise source shown in the dashboard."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


class SourceReplacementRequests:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS source_replacement_requests (
                request_id TEXT PRIMARY KEY, permit_id TEXT NOT NULL, infohash TEXT NOT NULL,
                reservation_id TEXT NOT NULL, media_key TEXT NOT NULL, scope_key TEXT,
                state TEXT NOT NULL, result TEXT, created_at REAL NOT NULL,
                updated_at REAL NOT NULL)""")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_source_replacement
                ON source_replacement_requests(permit_id) WHERE state IN ('queued', 'running')""")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def enqueue(self, source: dict) -> dict:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM source_replacement_requests WHERE permit_id=? "
                "AND state IN ('queued', 'running')",
                (source["permit_id"],),
            ).fetchone()
            if row is None:
                request_id = str(uuid4())
                connection.execute(
                    """INSERT INTO source_replacement_requests
                    VALUES (?, ?, ?, ?, ?, ?, 'queued', NULL, ?, ?)""",
                    (
                        request_id,
                        source["permit_id"],
                        source["infohash"],
                        source["reservation_id"],
                        source["media_key"],
                        source["scope_key"],
                        now,
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM source_replacement_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            connection.commit()
            return dict(row)

    def latest(self, permit_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_replacement_requests WHERE permit_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (permit_id,),
            ).fetchone()
        return dict(row) if row else None

    def recent(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_replacement_requests ORDER BY created_at DESC LIMIT 10",
            ).fetchall()
        return [dict(row) for row in rows]

    def claim(self, *, lease_seconds: float = 600) -> dict | None:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM source_replacement_requests WHERE state='queued' "
                "OR (state='running' AND updated_at<?) ORDER BY created_at LIMIT 1",
                (now - lease_seconds,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                "UPDATE source_replacement_requests SET state='running', updated_at=? "
                "WHERE request_id=?",
                (now, row["request_id"]),
            )
            connection.commit()
            return {**dict(row), "state": "running", "updated_at": now}

    def finish(self, request_id: str, *, state: str, result: str) -> None:
        if state not in {"succeeded", "failed", "no_candidate", "stale", "completed"}:
            raise ValueError("invalid manual source request result")
        with self._connect() as connection:
            connection.execute(
                "UPDATE source_replacement_requests SET state=?, result=?, updated_at=? "
                "WHERE request_id=? AND state='running'",
                (state, result, time.time(), request_id),
            )

    def source(self, infohash: str) -> dict | None:
        """A stopped discarded source is never a target for a new operator command."""
        with self._connect() as connection:
            row = connection.execute(
                """SELECT p.permit_id, p.infohash, p.reservation_id,
                p.scope_key, p.category, p.pool_id, r.media_key,
                r.state AS reservation_state, q.state AS request_state,
                (EXISTS(SELECT 1 FROM movie_imports m WHERE m.reservation_id=r.id)
                 OR EXISTS(SELECT 1 FROM episode_imports e WHERE e.permit_id=p.permit_id
                   OR e.permit_id LIKE p.permit_id || ':S%')) AS import_started,
                EXISTS(SELECT 1 FROM tombstones t WHERE t.media_key=r.media_key) AS tombstoned
                FROM gateway_permits p JOIN reservations r ON r.id=p.reservation_id
                JOIN requests q ON q.id=r.request_id
                WHERE p.infohash=? AND p.state='confirmed' AND p.probe_parent_id IS NULL
                ORDER BY p.rowid DESC LIMIT 1""",
                (infohash,),
            ).fetchone()
            if row is None:
                return None
            source = dict(row)
            source['protected'] = False
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='protected_sources'",
            ).fetchone():
                source['protected'] = bool(connection.execute(
                    'SELECT 1 FROM protected_sources WHERE infohash=?', (infohash,),
                ).fetchone())
            season = re.fullmatch(r'season:tmdb:([0-9]+):[0-9]+', source['media_key'])
            if season and re.fullmatch(r'S[0-9]+E[0-9]+', source['scope_key'] or ''):
                episode_key = f"episode:tmdb:{season[1]}:{source['scope_key']}"
                source['tombstoned'] |= bool(connection.execute(
                    'SELECT 1 FROM tombstones WHERE media_key=?', (episode_key,),
                ).fetchone())
            elif season and re.fullmatch(r'S([0-9]+)PACK', source['scope_key'] or ''):
                pack_season = int(re.fullmatch(r'S([0-9]+)PACK', source['scope_key'])[1])
                prefix = f'episode:tmdb:{season[1]}:S{pack_season:02d}E%'
                source['tombstoned'] |= bool(connection.execute(
                    'SELECT 1 FROM tombstones WHERE media_key LIKE ?', (prefix,),
                ).fetchone())
            return source

    @staticmethod
    def source_allowed(source: dict) -> bool:
        return source["reservation_state"] in {
            "reserved",
            "downloading",
            "waiting_episodes",
        } and (
            source["request_state"] not in {"cancel_requested", "cancelled"}
            and not source['import_started'] and not source['tombstoned']
            and not source['protected']
        )
