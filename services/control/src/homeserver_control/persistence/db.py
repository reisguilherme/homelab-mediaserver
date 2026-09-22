from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from homeserver_control.domain.capacity import available_bytes, remaining_commitment


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ReservationResult:
    accepted: bool
    reservation_id: str | None = None
    operation_id: str | None = None
    reason: str | None = None


class ReservationRepository:
    def __init__(self, path: str | Path, *, timeout_seconds: float = 5.0) -> None:
        self.path = str(path)
        self.timeout_seconds = timeout_seconds

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        migrations = sorted((Path(__file__).parent / "migrations").glob("*.sql"))
        with self._connect() as connection:
            # The first migration creates schema_migrations itself; afterward
            # every file is applied once in lexical/version order.
            for migration_path in migrations:
                version = int(migration_path.stem.split("_", 1)[0])
                schema_table = connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'schema_migrations'"
                ).fetchone()
                applied = (
                    connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
                    ).fetchone()
                    if schema_table is not None
                    else None
                )
                if applied is not None:
                    continue
                connection.executescript(migration_path.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )

    def reserve(
        self,
        *,
        request_id: str,
        source_id: str,
        media_key: str,
        filesystem_id: str,
        budget_bytes: int,
        free_bytes: int,
        total_bytes: int,
    ) -> ReservationResult:
        if not filesystem_id:
            return ReservationResult(False, reason="filesystem_unknown")
        if budget_bytes < 0:
            return ReservationResult(False, reason="invalid_budget")
        now = _now()
        operation_key = f"reserve:{request_id}:{media_key}"
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "SELECT 1 FROM tombstones WHERE media_key = ?", (media_key,)
            ).fetchone()
            if deleted is not None:
                connection.commit()
                return ReservationResult(False, reason="media_deleted")
            connection.execute(
                """
                INSERT INTO requests(id, source_id, media_key, state, created_at, updated_at)
                VALUES (?, ?, ?, 'requested', ?, ?)
                ON CONFLICT(source_id) DO NOTHING
                """,
                (request_id, source_id, media_key, now, now),
            )
            existing = connection.execute(
                """
                SELECT r.id AS reservation_id, o.id AS operation_id
                FROM reservations r
                JOIN requests q ON q.id = r.request_id
                LEFT JOIN operations o ON o.idempotency_key = ?
                WHERE q.source_id = ? OR r.media_key = ?
                """,
                (operation_key, source_id, media_key),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return ReservationResult(
                    True,
                    reservation_id=existing["reservation_id"],
                    operation_id=existing["operation_id"],
                    reason="idempotent",
                )

            rows = connection.execute(
                """
                SELECT budget_bytes, allocated_bytes
                FROM reservations
                WHERE state IN ('reserved', 'downloading', 'waiting_episodes', 'validating')
                """
            ).fetchall()
            commitments = [
                remaining_commitment(row["budget_bytes"], row["allocated_bytes"]) for row in rows
            ]
            if budget_bytes > available_bytes(free_bytes, total_bytes, commitments):
                connection.execute(
                    "UPDATE requests SET state = 'waiting_space', updated_at = ? WHERE id = ?",
                    (now, request_id),
                )
                connection.commit()
                return ReservationResult(False, reason="waiting_space")

            reservation_id = str(uuid4())
            operation_id = str(uuid4())
            payload = {
                "request_id": request_id,
                "media_key": media_key,
                "filesystem_id": filesystem_id,
                "budget_bytes": budget_bytes,
            }
            connection.execute(
                """
                INSERT INTO reservations(
                  id, request_id, media_key, filesystem_id, budget_bytes, state
                )
                VALUES (?, ?, ?, ?, ?, 'reserved')
                """,
                (reservation_id, request_id, media_key, filesystem_id, budget_bytes),
            )
            connection.execute(
                """
                INSERT INTO operations(id, idempotency_key, kind, payload_json, state, updated_at)
                VALUES (?, ?, 'reserve', ?, 'authorized', ?)
                """,
                (operation_id, operation_key, json.dumps(payload, sort_keys=True), now),
            )
            connection.execute(
                "UPDATE requests SET state = 'reserved', updated_at = ? WHERE id = ?",
                (now, request_id),
            )
            connection.commit()
            return ReservationResult(True, reservation_id=reservation_id, operation_id=operation_id)
        except sqlite3.IntegrityError:
            connection.rollback()
            existing = connection.execute(
                "SELECT id FROM reservations WHERE media_key = ?", (media_key,)
            ).fetchone()
            if existing:
                return ReservationResult(True, reservation_id=existing["id"], reason="idempotent")
            raise
        finally:
            connection.close()

    def count_reservations(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0])

    def record_operation(
        self,
        *,
        operation_id: str,
        idempotency_key: str,
        kind: str,
        payload: dict[str, object],
        state: str,
    ) -> dict[str, object]:
        """Persist an idempotent operation and return its canonical row."""

        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO operations(
                  id, idempotency_key, kind, payload_json, state, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    idempotency_key,
                    kind,
                    json.dumps(payload, sort_keys=True),
                    state,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT id, kind, payload_json, state, updated_at FROM operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("operation was not persisted")
            connection.commit()
            decoded = json.loads(row["payload_json"])
            return {
                "operation_id": row["id"],
                "kind": row["kind"],
                "state": row["state"],
                "updated_at": row["updated_at"],
                **decoded,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_operation(self, operation_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, kind, payload_json, state, updated_at FROM operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return {
            "operation_id": row["id"],
            "kind": row["kind"],
            "state": row["state"],
            "updated_at": row["updated_at"],
            **payload,
        }
