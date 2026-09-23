from __future__ import annotations

import json
import re
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

from homeserver_control.worker.capacity_evidence import CapacityEvidence


@dataclass
class Permit:
    permit_id: str
    token: str
    infohash: str
    destination: str
    expires_at: datetime
    category: str = ""
    result: dict[str, Any] | None = None
    operation_id: str = field(default_factory=lambda: str(uuid4()))
    reservation_id: str | None = None
    scope_key: str | None = None
    metadata_sha256: str | None = None
    selected_files: tuple[str, ...] = ()
    budget_bytes: int | None = None
    state: str = "authorized"
    reported_seeders: int | None = None


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
            # Serialize schema inspection and ALTER across worker/gateway processes.
            # A second initializer must observe the committed schema, not a stale
            # PRAGMA result followed by a duplicate-column ALTER.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS gateway_permits (
                  permit_id TEXT PRIMARY KEY,
                  token TEXT NOT NULL UNIQUE,
                  operation_id TEXT NOT NULL UNIQUE,
                  reservation_id TEXT,
                  scope_key TEXT,
                  infohash TEXT NOT NULL,
                  metadata_sha256 TEXT,
                  destination TEXT NOT NULL,
                  category TEXT NOT NULL DEFAULT '',
                  selected_files_json TEXT NOT NULL,
                  budget_bytes INTEGER,
                  reported_seeders INTEGER,
                  expires_at TEXT NOT NULL,
                  state TEXT NOT NULL,
                  result_json TEXT
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(gateway_permits)")
            }
            if "category" not in columns:
                connection.execute(
                    "ALTER TABLE gateway_permits ADD COLUMN category TEXT NOT NULL DEFAULT ''"
                )
            if "scope_key" not in columns:
                connection.execute("ALTER TABLE gateway_permits ADD COLUMN scope_key TEXT")
            if "reported_seeders" not in columns:
                connection.execute(
                    "ALTER TABLE gateway_permits ADD COLUMN reported_seeders INTEGER"
                )
            connection.execute("DROP INDEX IF EXISTS idx_gateway_permit_reservation")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_permit_reservation "
                "ON gateway_permits(reservation_id) "
                "WHERE reservation_id IS NOT NULL AND scope_key IS NULL "
                "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed')"
            )
            connection.execute("DROP INDEX IF EXISTS idx_gateway_permit_scope")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_permit_scope "
                "ON gateway_permits(reservation_id, scope_key) "
                "WHERE reservation_id IS NOT NULL AND scope_key IS NOT NULL "
                "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed')"
            )
            connection.commit()

    @staticmethod
    def _pending_bytes(
        connection: sqlite3.Connection, capacity: CapacityEvidence,
        *, include_infohash: str | None = None,
    ) -> int:
        rows = connection.execute(
            """SELECT p.infohash, p.budget_bytes, p.state FROM gateway_permits p
            JOIN reservations r ON r.id = p.reservation_id
            WHERE r.state IN ('reserved', 'downloading', 'waiting_episodes')
              AND p.state IN ('authorized', 'dispatching', 'unknown', 'confirmed')
              AND (p.state != 'authorized' OR p.expires_at > ?)
              AND p.budget_bytes > 0
              AND NOT EXISTS (
                SELECT 1 FROM episode_imports e
                WHERE e.permit_id = p.permit_id AND e.state = 'complete'
              )""",
            (datetime.now(UTC).isoformat(),),
        ).fetchall()
        return capacity.other_pending_bytes + sum(
            min(row["budget_bytes"], capacity.remaining_by_hash.get(
                row["infohash"], row["budget_bytes"]
            )) for row in rows
            if row["state"] != "confirmed"
            or row["infohash"] not in capacity.paused_hashes
            or row["infohash"] == include_infohash
        )

    def pending_bytes(
        self, capacity: CapacityEvidence, *, include_infohash: str | None = None,
    ) -> int:
        if self._db_path is None:
            raise ValueError("persistent permits required for queue capacity")
        with self._session() as connection:
            return self._pending_bytes(
                connection, capacity, include_infohash=include_infohash
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
    def _validate_reported_seeders(value: int | None) -> int | None:
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            raise ValueError("invalid reported seeders")
        return value

    @staticmethod
    def _permit_from_row(row: sqlite3.Row) -> Permit:
        return Permit(
            permit_id=row["permit_id"],
            token=row["token"],
            operation_id=row["operation_id"],
            reservation_id=row["reservation_id"],
            scope_key=row["scope_key"],
            infohash=row["infohash"],
            metadata_sha256=row["metadata_sha256"],
            destination=row["destination"],
            category=row["category"],
            selected_files=tuple(json.loads(row["selected_files_json"])),
            budget_bytes=row["budget_bytes"],
            reported_seeders=row["reported_seeders"],
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
        category: str = "",
        reservation_id: str | None = None,
        scope_key: str | None = None,
        metadata_sha256: str | None = None,
        selected_files: tuple[str, ...] = (),
        budget_bytes: int | None = None,
        capacity: CapacityEvidence | None = None,
        reported_seeders: int | None = None,
    ) -> Permit:
        permit = Permit(
            permit_id=str(uuid4()),
            token=secrets.token_urlsafe(32),
            infohash=infohash.lower(),
            destination=destination,
            expires_at=expires_at,
            category=category,
            reservation_id=reservation_id,
            scope_key=scope_key,
            metadata_sha256=self._validate_metadata_digest(metadata_sha256),
            selected_files=selected_files,
            budget_bytes=budget_bytes,
            reported_seeders=self._validate_reported_seeders(reported_seeders),
        )
        if capacity is not None and (self._db_path is None or reservation_id is None or
                                     isinstance(budget_bytes, bool) or
                                     not isinstance(budget_bytes, int) or budget_bytes <= 0):
            raise ValueError("exact-byte permit requires a persistent reservation and size")
        if self._db_path is None:
            with self._lock:
                if reservation_id is not None and any(
                    item.reservation_id == reservation_id
                    and item.scope_key == scope_key and item.state == "superseded"
                    for item in self._permits.values()
                ):
                    raise PermissionError("replacement_limit")
                self._permits[permit.token] = permit
        else:
            with self._session() as connection:
                if reservation_id is not None and capacity is None and scope_key is None:
                    connection.execute("BEGIN IMMEDIATE")
                if capacity is not None:
                    connection.execute("BEGIN IMMEDIATE")
                    reservation = connection.execute(
                        "SELECT media_key FROM reservations WHERE id = ? "
                        "AND state IN ('reserved', 'downloading', 'waiting_episodes')",
                        (reservation_id,),
                    ).fetchone()
                    if reservation is None:
                        raise PermissionError("reservation_required")
                    if scope_key is not None and (
                        not scope_key or category != "sonarr"
                        or not reservation["media_key"].startswith("season:tmdb:")
                    ):
                        raise ValueError("invalid episode permit")
                    pending = self._pending_bytes(connection, capacity)
                    if budget_bytes > max(0, capacity.free_bytes - pending):
                        raise PermissionError("waiting_space")
                    connection.execute(
                        """UPDATE reservations SET budget_bytes = ? + COALESCE((
                            SELECT SUM(p.budget_bytes) FROM gateway_permits p
                            WHERE p.reservation_id = ? AND p.state IN
                            ('authorized', 'dispatching', 'unknown', 'confirmed')
                        ), 0) WHERE id = ?""",
                        (budget_bytes, reservation_id, reservation_id),
                    )
                elif scope_key is not None:
                    if (
                        not scope_key or category != "sonarr" or reservation_id is None
                        or budget_bytes is None or budget_bytes <= 0
                    ):
                        raise ValueError("invalid episode permit")
                    connection.execute("BEGIN IMMEDIATE")
                    reservation = connection.execute(
                        "SELECT media_key, budget_bytes FROM reservations "
                        "WHERE id = ? AND state IN ('reserved', 'downloading', 'waiting_episodes')",
                        (reservation_id,),
                    ).fetchone()
                    if reservation is None or not reservation["media_key"].startswith(
                        "season:tmdb:"
                    ):
                        raise PermissionError("season_reservation_required")
                    committed = connection.execute(
                        "SELECT COALESCE(SUM(budget_bytes), 0) FROM gateway_permits "
                    "WHERE reservation_id = ? AND scope_key IS NOT NULL "
                    "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed')",
                        (reservation_id,),
                    ).fetchone()[0]
                    if committed + budget_bytes > reservation["budget_bytes"]:
                        raise PermissionError("season_budget_exceeded")
                elif reservation_id is not None:
                    reservation = connection.execute(
                        "SELECT budget_bytes FROM reservations WHERE id = ?", (reservation_id,)
                    ).fetchone()
                    if reservation is not None and reservation["budget_bytes"] == 0:
                        raise PermissionError("capacity_evidence_required")
                if reservation_id is not None and connection.execute(
                    "SELECT 1 FROM gateway_permits WHERE reservation_id = ? "
                    "AND scope_key IS ? AND state = 'superseded' LIMIT 1",
                    (reservation_id, scope_key),
                ).fetchone():
                    raise PermissionError("replacement_limit")
                connection.execute(
                    """
                    INSERT INTO gateway_permits(
                      permit_id, token, operation_id, reservation_id, scope_key, infohash,
                      metadata_sha256, destination, category, selected_files_json,
                      budget_bytes, reported_seeders, expires_at, state, result_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        permit.permit_id,
                        permit.token,
                        permit.operation_id,
                        permit.reservation_id,
                        permit.scope_key,
                        permit.infohash,
                        permit.metadata_sha256,
                        permit.destination,
                        permit.category,
                        json.dumps(permit.selected_files),
                        permit.budget_bytes,
                        permit.reported_seeders,
                        permit.expires_at.isoformat(),
                        permit.state,
                    ),
                )
                if reservation_id is not None or capacity is not None:
                    connection.commit()
        return permit

    def find_for_metadata(
        self, *, infohash: str, metadata_sha256: str, destination: str, category: str
    ) -> Permit:
        """Resolve Arr's headerless add to one pre-authorized metadata identity.

        Persistent permits are usable only while the matching reservation still
        exists. This is deliberately stricter than the legacy token API.
        """
        digest = self._validate_metadata_digest(metadata_sha256)
        if self._db_path is None:
            with self._lock:
                matches = [
                    item
                    for item in self._permits.values()
                    if item.infohash == infohash.lower()
                    and item.metadata_sha256 == digest
                    and item.destination == destination
                    and item.category == category
                    and item.state in {"authorized", "confirmed"}
                    and datetime.now(UTC) < item.expires_at
                ]
        else:
            with self._session() as connection:
                rows = connection.execute(
                    """
                    SELECT p.* FROM gateway_permits p
                    JOIN reservations r ON r.id = p.reservation_id
                    WHERE p.infohash = ? AND p.metadata_sha256 = ?
                      AND p.destination = ? AND p.category = ?
                      AND p.state IN ('authorized', 'confirmed')
                      AND r.state IN ('reserved', 'downloading', 'waiting_episodes')
                      AND p.budget_bytes IS NOT NULL AND p.budget_bytes <= r.budget_bytes
                    """,
                    (infohash.lower(), digest, destination, category),
                ).fetchall()
            matches = [
                self._permit_from_row(row)
                for row in rows
                if datetime.now(UTC) < self._expires(row["expires_at"])
            ]
        if len(matches) != 1:
            raise PermissionError("permit_required_or_ambiguous")
        return matches[0]

    def find_for_magnet(
        self, *, infohash: str, destination: str, category: str
    ) -> Permit:
        """Resolve a magnet only after metadata and reservation were verified.

        The v1 infohash identifies the inspected torrent info dictionary. A
        headerless Arr request cannot choose a permit token or bypass its
        reserved budget.
        """
        if self._db_path is None:
            with self._lock:
                matches = [
                    item for item in self._permits.values()
                    if item.infohash == infohash.lower()
                    and item.destination == destination
                    and item.category == category
                    and item.metadata_sha256 is not None
                    and item.budget_bytes is not None and item.budget_bytes > 0
                    and item.selected_files
                    and item.state in {"authorized", "confirmed"}
                    and datetime.now(UTC) < item.expires_at
                ]
        else:
            with self._session() as connection:
                rows = connection.execute(
                    """
                    SELECT p.* FROM gateway_permits p
                    JOIN reservations r ON r.id = p.reservation_id
                    WHERE p.infohash = ? AND p.destination = ? AND p.category = ?
                      AND p.state IN ('authorized', 'confirmed')
                      AND p.metadata_sha256 IS NOT NULL
                      AND p.budget_bytes > 0 AND p.budget_bytes <= r.budget_bytes
                      AND p.selected_files_json != '[]'
                      AND r.state IN ('reserved', 'downloading', 'waiting_episodes')
                    """,
                    (infohash.lower(), destination, category),
                ).fetchall()
            matches = [
                self._permit_from_row(row) for row in rows
                if datetime.now(UTC) < self._expires(row["expires_at"])
            ]
        if len(matches) != 1:
            raise PermissionError("permit_required_or_ambiguous")
        return matches[0]

    def is_admitted(self, infohash: str) -> bool:
        if self._db_path is None:
            with self._lock:
                return any(
                    item.infohash == infohash.lower() and item.state == "confirmed"
                    for item in self._permits.values()
                )
        with self._session() as connection:
            row = connection.execute(
                "SELECT 1 FROM gateway_permits WHERE infohash = ? AND state = 'confirmed' LIMIT 1",
                (infohash.lower(),),
            ).fetchone()
        return row is not None

    def get_for_reservation(
        self, reservation_id: str, *, scope_key: str | None = None
    ) -> Permit | None:
        if self._db_path is None:
            with self._lock:
                return next(
                    (
                        item for item in self._permits.values()
                        if item.reservation_id == reservation_id and item.scope_key == scope_key
                        and item.state in {"authorized", "dispatching", "unknown", "confirmed"}
                    ),
                    None,
                )
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? AND state IN "
                "('authorized', 'dispatching', 'unknown', 'confirmed')",
                (reservation_id, scope_key),
            ).fetchone()
        return self._permit_from_row(row) if row is not None else None

    def list_active_confirmed_movies(self) -> list[Permit]:
        """Return admitted movie sources whose reservations are still active."""
        if self._db_path is None:
            with self._lock:
                return [
                    permit for permit in self._permits.values()
                    if permit.state == "confirmed" and permit.category == "radarr"
                    and permit.scope_key is None and permit.reservation_id is not None
                ]
        with self._session() as connection:
            rows = connection.execute(
                "SELECT p.* FROM gateway_permits p "
                "JOIN reservations r ON r.id = p.reservation_id "
                "WHERE p.state = 'confirmed' AND p.category = 'radarr' "
                "AND p.scope_key IS NULL AND r.media_key LIKE 'movie:tmdb:%' "
                "AND r.state IN ('reserved', 'downloading', 'waiting_episodes') "
                "ORDER BY p.permit_id"
            ).fetchall()
        return [self._permit_from_row(row) for row in rows]

    def list_active_confirmed_episodes(self) -> list[tuple[str, Permit]]:
        """Return pending Sonarr episodes grouped across seasons of each series."""
        if self._db_path is None:
            # In-memory permits have no reservation media key; grouping by the
            # reservation is conservative for contract tests.
            with self._lock:
                return [
                    (permit.reservation_id, permit)
                    for permit in self._permits.values()
                    if permit.state == "confirmed" and permit.category == "sonarr"
                    and permit.scope_key is not None and permit.reservation_id is not None
                ]
        with self._session() as connection:
            rows = connection.execute(
                "SELECT p.*, r.media_key FROM gateway_permits p "
                "JOIN reservations r ON r.id = p.reservation_id "
                "WHERE p.state = 'confirmed' AND p.category = 'sonarr' "
                "AND p.scope_key IS NOT NULL "
                "AND r.state IN ('reserved', 'downloading', 'waiting_episodes') "
                "AND r.media_key LIKE 'season:tmdb:%' "
                "AND NOT EXISTS (SELECT 1 FROM episode_imports e "
                "WHERE e.permit_id = p.permit_id AND e.state = 'complete') "
                "ORDER BY p.permit_id"
            ).fetchall()
        episodes: list[tuple[str, Permit]] = []
        for row in rows:
            match = re.fullmatch(r"season:tmdb:([1-9][0-9]*):[0-9]+", row["media_key"])
            if match is not None:
                episodes.append((f"season:tmdb:{match.group(1)}", self._permit_from_row(row)))
        return episodes

    def had_superseded(self, reservation_id: str, *, scope_key: str | None = None) -> bool:
        """Report whether this exact movie or episode slot has used its one failover."""
        if self._db_path is None:
            with self._lock:
                return any(
                    item.reservation_id == reservation_id and item.scope_key == scope_key
                    and item.state == "superseded" for item in self._permits.values()
                )
        with self._session() as connection:
            row = connection.execute(
                "SELECT 1 FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? AND state = 'superseded' LIMIT 1",
                (reservation_id, scope_key),
            ).fetchone()
        return row is not None

    def list_uncertain(
        self, limit: int = 100, *, after_id: str | None = None
    ) -> list[Permit]:
        """Enumerate uncertain sources with a cursor across movie and episode slots."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if after_id is not None and (not isinstance(after_id, str) or not after_id):
            raise ValueError("after_id must be a nonempty permit ID")
        capped = min(limit, 100)
        if self._db_path is None:
            with self._lock:
                return sorted(
                    (
                        item for item in self._permits.values()
                        if item.reservation_id is not None
                        and item.state in {"unknown", "dispatching"}
                        and (after_id is None or item.permit_id > after_id)
                    ),
                    key=lambda item: item.permit_id,
                )[:capped]
        with self._session() as connection:
            rows = connection.execute(
                "SELECT p.* FROM gateway_permits p "
                "JOIN reservations r ON r.id = p.reservation_id "
                "WHERE p.state IN ('unknown', 'dispatching') "
                "AND r.state IN ('reserved', 'downloading', 'waiting_episodes') "
                "AND p.permit_id > ? "
                "ORDER BY p.permit_id LIMIT ?",
                (after_id or "", capped),
            ).fetchall()
        return [self._permit_from_row(row) for row in rows]

    def confirm_reconciled_source(self, token: str) -> Permit:
        """Confirm a source only after the gateway verifies persisted metadata and qBittorrent."""
        if self._db_path is None:
            with self._lock:
                permit = self._permits.get(token)
                if (
                    permit is None or permit.state not in {
                        "authorized", "dispatching", "unknown", "confirmed"
                    }
                    or permit.reservation_id is None
                    or (permit.state == "authorized" and not self.had_superseded(
                        permit.reservation_id, scope_key=permit.scope_key
                    ))
                ):
                    raise PermissionError("reconcilable_source_required")
                if permit.state == "confirmed":
                    if permit.result != {"accepted": True, "infohash": permit.infohash}:
                        raise PermissionError("confirmed_result_mismatch")
                    return permit
                permit.state = "confirmed"
                permit.result = {"accepted": True, "infohash": permit.infohash}
                return permit

        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token = ?", (token,)
            ).fetchone()
            permit = self._permit_from_row(row) if row is not None else None
            if (
                permit is None or permit.state not in {
                    "authorized", "dispatching", "unknown", "confirmed"
                }
                or permit.reservation_id is None
            ):
                raise PermissionError("reconcilable_source_required")
            if permit.state == "authorized" and not connection.execute(
                "SELECT 1 FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? AND state = 'superseded' LIMIT 1",
                (permit.reservation_id, permit.scope_key),
            ).fetchone():
                raise PermissionError("replacement_permit_required")
            reservation = connection.execute(
                "SELECT media_key FROM reservations WHERE id = ? "
                "AND state IN ('reserved', 'downloading', 'waiting_episodes')",
                (permit.reservation_id,),
            ).fetchone()
            if reservation is None or (
                permit.scope_key is None and (
                    permit.category != "radarr"
                    or not reservation["media_key"].startswith("movie:tmdb:")
                )
            ) or (
                permit.scope_key is not None and (
                    permit.category != "sonarr"
                    or not reservation["media_key"].startswith("season:tmdb:")
                )
            ):
                raise PermissionError("active_source_reservation_required")
            result = {"accepted": True, "infohash": permit.infohash}
            if permit.state == "confirmed":
                if permit.result != result:
                    raise PermissionError("confirmed_result_mismatch")
                connection.commit()
                return permit
            connection.execute(
                "UPDATE gateway_permits SET state = 'confirmed', result_json = ? "
                "WHERE permit_id = ? AND state IN ('authorized', 'dispatching', 'unknown')",
                (json.dumps(result, sort_keys=True), permit.permit_id),
            )
            connection.commit()
            permit.state = "confirmed"
            permit.result = result
            return permit

    def confirm_replacement(self, token: str) -> Permit:
        """Compatibility alias for callers confirming an already verified replacement."""
        return self.confirm_reconciled_source(token)

    def renew_replacement_authorized(
        self, token: str, *, capacity: CapacityEvidence, expires_at: datetime,
    ) -> Permit:
        """Renew an undispatched replacement without abandoning its exact-byte claim."""
        if not isinstance(capacity, CapacityEvidence) or expires_at <= datetime.now(UTC):
            raise ValueError("fresh replacement capacity and expiry are required")
        if self._db_path is None:
            with self._lock:
                permit = self._permits.get(token)
                if (
                    permit is None or permit.state != "authorized"
                    or permit.reservation_id is None or permit.budget_bytes is None
                ):
                    raise PermissionError("authorized_replacement_required")
                old = next((
                    item for item in self._permits.values()
                    if item.reservation_id == permit.reservation_id
                    and item.scope_key == permit.scope_key
                    and item.state == "superseded"
                ), None)
                if old is None or old.infohash not in capacity.paused_hashes:
                    raise PermissionError("source_not_stopped")
                if permit.budget_bytes > max(
                    0, capacity.free_bytes - capacity.other_pending_bytes
                ):
                    raise PermissionError("waiting_space")
                permit.expires_at = expires_at
                return permit

        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token = ?", (token,)
            ).fetchone()
            permit = self._permit_from_row(row) if row is not None else None
            if (
                permit is None or permit.state != "authorized"
                or permit.reservation_id is None
                or permit.budget_bytes is None or permit.budget_bytes <= 0
            ):
                raise PermissionError("authorized_replacement_required")
            old = connection.execute(
                "SELECT infohash FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? AND state = 'superseded' LIMIT 1",
                (permit.reservation_id, permit.scope_key),
            ).fetchone()
            if old is None or old["infohash"] not in capacity.paused_hashes:
                raise PermissionError("source_not_stopped")
            if not connection.execute(
                "SELECT 1 FROM reservations WHERE id = ? "
                "AND state IN ('reserved', 'downloading', 'waiting_episodes')",
                (permit.reservation_id,),
            ).fetchone():
                raise PermissionError("reservation_required")
            pending = self._pending_bytes(connection, capacity)
            if permit.expires_at <= datetime.now(UTC):
                pending += min(
                    permit.budget_bytes,
                    capacity.remaining_by_hash.get(permit.infohash, permit.budget_bytes),
                )
            if pending > capacity.free_bytes:
                raise PermissionError("waiting_space")
            connection.execute(
                "UPDATE gateway_permits SET expires_at = ? WHERE permit_id = ? "
                "AND state = 'authorized'",
                (expires_at.isoformat(), permit.permit_id),
            )
            connection.commit()
            permit.expires_at = expires_at
            return permit

    def replace_confirmed(
        self, old_token: str, *, infohash: str, metadata_sha256: str,
        selected_files: tuple[str, ...], budget_bytes: int,
        capacity: CapacityEvidence, expires_at: datetime,
        reported_seeders: int | None = None,
    ) -> Permit:
        """Atomically replace one stopped source while retaining its bytes and audit row."""
        if not isinstance(infohash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
            raise ValueError("invalid replacement infohash")
        digest = self._validate_metadata_digest(metadata_sha256)
        if digest is None:
            raise ValueError("replacement metadata is required")
        reported_seeders = self._validate_reported_seeders(reported_seeders)
        if (
            not isinstance(selected_files, tuple) or not selected_files
            or any(not isinstance(name, str) or not name for name in selected_files)
            or len(set(selected_files)) != len(selected_files)
            or isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int)
            or budget_bytes <= 0 or not isinstance(capacity, CapacityEvidence)
            or expires_at <= datetime.now(UTC)
        ):
            raise ValueError("invalid replacement evidence")
        new_hash = infohash.lower()

        def replacement(old: Permit) -> Permit:
            return Permit(
                permit_id=str(uuid4()), token=secrets.token_urlsafe(32),
                infohash=new_hash, metadata_sha256=digest,
                destination=old.destination, category=old.category,
                reservation_id=old.reservation_id, scope_key=old.scope_key,
                selected_files=selected_files, budget_bytes=budget_bytes,
                expires_at=expires_at,
                reported_seeders=reported_seeders,
            )

        if self._db_path is None:
            with self._lock:
                old = self._permits.get(old_token)
                if old is None or old.state != "confirmed" or old.reservation_id is None:
                    raise PermissionError("confirmed_source_required")
                if old.infohash not in capacity.paused_hashes:
                    raise PermissionError("source_not_stopped")
                if any(
                    item.reservation_id == old.reservation_id
                    and item.scope_key == old.scope_key and item.state == "superseded"
                    for item in self._permits.values()
                ):
                    raise PermissionError("replacement_limit")
                if any(
                    item.infohash == new_hash and item.reservation_id == old.reservation_id
                    and item.scope_key == old.scope_key for item in self._permits.values()
                ):
                    raise ValueError("same_infohash_or_prior_source")
                if budget_bytes > max(0, capacity.free_bytes - capacity.other_pending_bytes):
                    raise PermissionError("waiting_space")
                new = replacement(old)
                old.state = "superseded"
                self._permits[new.token] = new
                return new

        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token = ?", (old_token,)
            ).fetchone()
            old = self._permit_from_row(row) if row is not None else None
            if old is None or old.state != "confirmed" or old.reservation_id is None:
                raise PermissionError("confirmed_source_required")
            if old.infohash not in capacity.paused_hashes:
                raise PermissionError("source_not_stopped")
            if connection.execute(
                "SELECT 1 FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? AND state = 'superseded' LIMIT 1",
                (old.reservation_id, old.scope_key),
            ).fetchone():
                raise PermissionError("replacement_limit")
            reservation = connection.execute(
                "SELECT media_key FROM reservations WHERE id = ? "
                "AND state IN ('reserved', 'downloading', 'waiting_episodes')",
                (old.reservation_id,),
            ).fetchone()
            if reservation is None:
                raise PermissionError("reservation_required")
            media_key = reservation["media_key"]
            if (
                (old.scope_key is None and (
                    old.category != "radarr" or not media_key.startswith("movie:tmdb:")
                ))
                or (old.scope_key is not None and (
                    old.category != "sonarr" or not media_key.startswith("season:tmdb:")
                ))
            ):
                raise PermissionError("source_identity_changed")
            if connection.execute(
                "SELECT 1 FROM movie_imports WHERE reservation_id = ? LIMIT 1",
                (old.reservation_id,),
            ).fetchone() or connection.execute(
                "SELECT 1 FROM episode_imports WHERE permit_id = ? LIMIT 1",
                (old.permit_id,),
            ).fetchone():
                raise PermissionError("import_started")
            if connection.execute(
                "SELECT 1 FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? AND infohash = ? LIMIT 1",
                (old.reservation_id, old.scope_key, new_hash),
            ).fetchone():
                raise ValueError("same_infohash_or_prior_source")
            if connection.execute(
                "SELECT 1 FROM gateway_permits WHERE infohash = ? "
                "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed') LIMIT 1",
                (new_hash,),
            ).fetchone():
                raise PermissionError("source_already_admitted")
            pending = self._pending_bytes(connection, capacity)
            if budget_bytes > max(0, capacity.free_bytes - pending):
                raise PermissionError("waiting_space")
            new = replacement(old)
            connection.execute(
                "UPDATE gateway_permits SET state = 'superseded' "
                "WHERE permit_id = ? AND state = 'confirmed'", (old.permit_id,),
            )
            connection.execute(
                """INSERT INTO gateway_permits(
                    permit_id, token, operation_id, reservation_id, scope_key, infohash,
                    metadata_sha256, destination, category, selected_files_json,
                    budget_bytes, reported_seeders, expires_at, state, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'authorized', NULL)""",
                (new.permit_id, new.token, new.operation_id, new.reservation_id,
                 new.scope_key, new.infohash, new.metadata_sha256, new.destination,
                 new.category, json.dumps(new.selected_files), new.budget_bytes,
                 new.reported_seeders,
                 new.expires_at.isoformat()),
            )
            connection.execute(
                """UPDATE reservations SET budget_bytes = COALESCE((
                    SELECT SUM(p.budget_bytes) FROM gateway_permits p
                    WHERE p.reservation_id = ? AND p.state IN
                    ('authorized', 'dispatching', 'unknown', 'confirmed')
                ), 0) WHERE id = ?""",
                (old.reservation_id, old.reservation_id),
            )
            connection.commit()
            return new

    def retire_expired_authorized(
        self, reservation_id: str, *, scope_key: str | None = None
    ) -> int:
        """Release an unused permit after its gateway authorization has expired."""
        if self._db_path is None:
            with self._lock:
                expired = [key for key, item in self._permits.items()
                           if item.reservation_id == reservation_id
                           and item.scope_key == scope_key
                           and item.state == "authorized"
                           and item.expires_at <= datetime.now(UTC)]
                for key in expired:
                    del self._permits[key]
                return len(expired)
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM gateway_permits WHERE reservation_id = ? AND scope_key IS ? "
                "AND state = 'authorized' AND expires_at <= ?",
                (reservation_id, scope_key, datetime.now(UTC).isoformat()),
            )
            if cursor.rowcount:
                connection.execute(
                    """UPDATE reservations SET budget_bytes = COALESCE((
                        SELECT SUM(p.budget_bytes) FROM gateway_permits p
                        WHERE p.reservation_id = ? AND p.state IN
                        ('authorized', 'dispatching', 'unknown', 'confirmed')
                    ), 0) WHERE id = ?""",
                    (reservation_id, reservation_id),
                )
            connection.commit()
            return cursor.rowcount

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
                if permit.result is not None and permit.state == "confirmed":
                    return permit.result
                if permit.state != "authorized":
                    raise PermissionError(f"permit_{permit.state}")
                permit.state = "dispatching"
                try:
                    result = effect(permit)
                except Exception:
                    if permit.state == "dispatching":
                        permit.state = "unknown"
                    raise
                if permit.state == "confirmed" and permit.result is not None:
                    return permit.result
                if permit.state != "dispatching":
                    raise PermissionError("permit_state_changed")
                permit.result = result
                permit.state = "confirmed"
                return result

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
            if permit.result is not None and permit.state == "confirmed":
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
                    "UPDATE gateway_permits SET state = 'unknown' "
                    "WHERE token = ? AND state = 'dispatching'", (token,)
                )
            raise
        with self._session() as update:
            cursor = update.execute(
                "UPDATE gateway_permits SET state = 'confirmed', result_json = ? "
                "WHERE token = ? AND state = 'dispatching'",
                (json.dumps(result, sort_keys=True), token),
            )
            if cursor.rowcount == 0:
                row = update.execute(
                    "SELECT state, result_json FROM gateway_permits WHERE token = ?", (token,)
                ).fetchone()
                if row is not None and row["state"] == "confirmed" and row["result_json"]:
                    return json.loads(row["result_json"])
                raise PermissionError("permit_state_changed")
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
