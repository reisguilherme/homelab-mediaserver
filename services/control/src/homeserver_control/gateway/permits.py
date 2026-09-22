from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import uuid4


@dataclass
class Permit:
    permit_id: str
    token: str
    infohash: str
    destination: str
    expires_at: datetime
    result: dict[str, Any] | None = None
    operation_id: str = field(default_factory=lambda: str(uuid4()))
    reservation_id: str | None = None
    metadata_sha256: str | None = None
    selected_files: tuple[str, ...] = ()
    budget_bytes: int | None = None
    state: str = "authorized"


class PermitRegistry:
    """Issue and consume permits with single-use effect transitions.

    The in-memory mode keeps contract tests lightweight. Production passes the
    controller database path so permits survive process restarts and the
    dispatching state prevents a second process from blindly repeating an
    uncertain upstream mutation.
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._permits: dict[str, Permit] = {}
        self._db_path = str(db_path) if db_path is not None else None
        self._lock = RLock()
        self._token_locks: dict[str, Lock] = {}
        if self._db_path is not None:
            self._initialize_db()

    def _connect(self) -> sqlite3.Connection:
        if self._db_path is None:
            raise RuntimeError("permit database is not configured")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _session(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize_db(self) -> None:
        with self._session() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_permits (
                  permit_id TEXT PRIMARY KEY,
                  token TEXT NOT NULL UNIQUE,
                  operation_id TEXT NOT NULL UNIQUE,
                  reservation_id TEXT,
                  infohash TEXT NOT NULL,
                  metadata_sha256 TEXT,
                  destination TEXT NOT NULL,
                  selected_files_json TEXT NOT NULL,
                  budget_bytes INTEGER,
                  expires_at TEXT NOT NULL,
                  state TEXT NOT NULL,
                  result_json TEXT
                )
                """
            )

    @staticmethod
    def _expires(value: str) -> datetime:
        return datetime.fromisoformat(value)

    @staticmethod
    def _validate_metadata_digest(value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError("invalid metadata digest")
        return value.lower()

    @staticmethod
    def _permit_from_row(row: sqlite3.Row) -> Permit:
        return Permit(
            permit_id=row["permit_id"],
            token=row["token"],
            operation_id=row["operation_id"],
            reservation_id=row["reservation_id"],
            infohash=row["infohash"],
            metadata_sha256=row["metadata_sha256"],
            destination=row["destination"],
            selected_files=tuple(json.loads(row["selected_files_json"])),
            budget_bytes=row["budget_bytes"],
            expires_at=PermitRegistry._expires(row["expires_at"]),
            state=row["state"],
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
        )

    def issue(
        self,
        *,
        infohash: str,
        destination: str,
        expires_at: datetime,
        reservation_id: str | None = None,
        metadata_sha256: str | None = None,
        selected_files: tuple[str, ...] = (),
        budget_bytes: int | None = None,
    ) -> Permit:
        permit = Permit(
            permit_id=str(uuid4()),
            token=secrets.token_urlsafe(32),
            infohash=infohash.lower(),
            destination=destination,
            expires_at=expires_at,
            reservation_id=reservation_id,
            metadata_sha256=self._validate_metadata_digest(metadata_sha256),
            selected_files=selected_files,
            budget_bytes=budget_bytes,
        )
        if self._db_path is None:
            with self._lock:
                self._permits[permit.token] = permit
        else:
            with self._session() as connection:
                connection.execute(
                    """
                    INSERT INTO gateway_permits(
                      permit_id, token, operation_id, reservation_id, infohash,
                      metadata_sha256, destination, selected_files_json,
                      budget_bytes, expires_at, state, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        permit.permit_id,
                        permit.token,
                        permit.operation_id,
                        permit.reservation_id,
                        permit.infohash,
                        permit.metadata_sha256,
                        permit.destination,
                        json.dumps(permit.selected_files),
                        permit.budget_bytes,
                        permit.expires_at.isoformat(),
                        permit.state,
                    ),
                )
        return permit

    def authorize(
        self,
        *,
        token: str,
        infohash: str,
        destination: str,
        metadata_sha256: str | None = None,
        effect: Callable[[Permit], dict[str, Any]],
    ) -> dict[str, Any]:
        expected_digest = self._validate_metadata_digest(metadata_sha256)
        if self._db_path is None:
            with self._lock:
                permit = self._permits.get(token)
                if permit is None:
                    raise PermissionError("permit_required")
                token_lock = self._token_locks.setdefault(token, Lock())
            with token_lock:
                permit = self._permits.get(token)
                self._validate_payload(permit, infohash, destination, expected_digest)
                if permit.result is not None:
                    return permit.result
                if permit.state != "authorized":
                    raise PermissionError(f"permit_{permit.state}")
                permit.state = "dispatching"
                try:
                    permit.result = effect(permit)
                except Exception:
                    permit.state = "unknown"
                    raise
                permit.state = "confirmed"
                return permit.result

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise PermissionError("permit_required")
            permit = self._permit_from_row(row)
            self._validate_payload(permit, infohash, destination, expected_digest)
            if permit.result is not None:
                connection.commit()
                return permit.result
            if permit.state != "authorized":
                connection.rollback()
                raise PermissionError(f"permit_{permit.state}")
            connection.execute(
                "UPDATE gateway_permits SET state = 'dispatching' WHERE token = ?", (token,)
            )
            connection.commit()
        finally:
            connection.close()

        try:
            result = effect(permit)
        except Exception:
            with self._session() as update:
                update.execute(
                    "UPDATE gateway_permits SET state = 'unknown' WHERE token = ?", (token,)
                )
            raise
        with self._session() as update:
            update.execute(
                "UPDATE gateway_permits SET state = 'confirmed', result_json = ? WHERE token = ?",
                (json.dumps(result, sort_keys=True), token),
            )
        return result

    @staticmethod
    def _validate_payload(
        permit: Permit | None,
        infohash: str,
        destination: str,
        metadata_sha256: str | None,
    ) -> None:
        if permit is None:
            raise PermissionError("permit_required")
        if datetime.now(UTC) >= permit.expires_at:
            raise PermissionError("permit_expired")
        if permit.infohash != infohash.lower() or destination != permit.destination:
            raise ValueError("permit_payload_mismatch")
        if metadata_sha256 is not None and permit.metadata_sha256 is None:
            raise ValueError("permit_metadata_missing")
        if permit.metadata_sha256 is not None and permit.metadata_sha256 != metadata_sha256:
            raise ValueError("permit_metadata_mismatch")

    def get(self, token: str) -> Permit | None:
        if self._db_path is None:
            with self._lock:
                return self._permits.get(token)
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token = ?", (token,)
            ).fetchone()
        return self._permit_from_row(row) if row is not None else None
