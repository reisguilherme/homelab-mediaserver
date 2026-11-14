from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from homeserver_control.domain.episode_identity import resolve_episode_identity
from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent
from homeserver_control.persistence.audited_derivations import SCHEMA as DERIVATION_SCHEMA
from homeserver_control.persistence.audited_derivations import decode as decode_derivation
from homeserver_control.worker.capacity_evidence import CapacityEvidence

from .probe_permits import ProbePermits

_SQLITE_LOCK_TIMEOUT_SECONDS = 5.0


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
    probe_parent_id: str | None = None
    quality_rank: tuple[int, ...] | None = None
    season_pack_parent_id: str | None = None
    pool_id: str = "ssd"
    filesystem_id: str | None = None


class PermitRegistry(ProbePermits):
    """Issue and consume permits with single-use effect transitions.

    The in-memory mode keeps contract tests lightweight. Production passes the
    controller database path so permits survive process restarts and the
    dispatching state prevents a second process from blindly repeating an
    uncertain upstream mutation.
    """

    def __init__(self, db_path: str | Path | None = None, *, storage_registry=None) -> None:
        self._permits: dict[str, Permit] = {}
        self._db_path = str(db_path) if db_path is not None else None
        self.storage_registry = storage_registry
        self._lock = RLock()
        self._token_locks: dict[str, Lock] = {}
        self._season_pack_bindings: dict[str, dict[str, tuple[str, ...]]] = {}
        self._pack_identity_receipts: dict[str, tuple] = {}
        self._controller_pause_holds: dict[str, tuple] = {}
        if self._db_path is not None:
            self._initialize_db()

    def _connect(self) -> sqlite3.Connection:
        if self._db_path is None:
            raise RuntimeError("permit database is not configured")
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + _SQLITE_LOCK_TIMEOUT_SECONDS
        while True:
            remaining = max(0, deadline - time.monotonic())
            connection = sqlite3.connect(self._db_path, timeout=remaining, isolation_level=None)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute(f"PRAGMA busy_timeout = {int(remaining * 1000)}")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA busy_timeout = 5000")
                return connection
            except sqlite3.OperationalError as error:
                # WAL conversion may return BUSY without running SQLite's busy
                # handler to avoid lock-upgrade deadlocks. Release every lock
                # before retrying another connection within the original budget.
                connection.close()
                code = getattr(error, "sqlite_errorcode", 0) & 0xff
                remaining = deadline - time.monotonic()
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or remaining <= 0:
                    raise
                time.sleep(min(0.025, remaining))
            except Exception:
                connection.close()
                raise

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
            connection.execute(DERIVATION_SCHEMA)
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
            if "probe_parent_id" not in columns:
                connection.execute("ALTER TABLE gateway_permits ADD COLUMN probe_parent_id TEXT")
            if "quality_rank_json" not in columns:
                connection.execute("ALTER TABLE gateway_permits ADD COLUMN quality_rank_json TEXT")
            if "pool_id" not in columns:
                connection.execute(
                    "ALTER TABLE gateway_permits ADD COLUMN pool_id TEXT NOT NULL DEFAULT 'ssd'"
                )
            if "filesystem_id" not in columns:
                connection.execute("ALTER TABLE gateway_permits ADD COLUMN filesystem_id TEXT")
            if self.storage_registry is not None:
                try:
                    sample = self.storage_registry.inspect("ssd")
                except (RuntimeError, ValueError, OSError):
                    pass
                else:
                    connection.execute(
                        "UPDATE gateway_permits SET filesystem_id=? WHERE pool_id='ssd' "
                        "AND filesystem_id IS NULL AND destination='/data/torrents'",
                        (sample.filesystem_id,),
                    )
            connection.execute("DROP INDEX IF EXISTS idx_gateway_permit_reservation")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_permit_reservation "
                "ON gateway_permits(reservation_id) "
                "WHERE reservation_id IS NOT NULL AND scope_key IS NULL "
                "AND probe_parent_id IS NULL "
                "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed')"
            )
            connection.execute("DROP INDEX IF EXISTS idx_gateway_permit_scope")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_gateway_permit_scope "
                "ON gateway_permits(reservation_id, scope_key) "
                "WHERE reservation_id IS NOT NULL AND scope_key IS NOT NULL "
                "AND probe_parent_id IS NULL "
                "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed')"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_active_probe "
                "ON gateway_permits(probe_parent_id) "
                "WHERE probe_parent_id IS NOT NULL "
                "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed')"
            )
            connection.execute("""CREATE TABLE IF NOT EXISTS source_handovers (
                candidate_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending')""")
            connection.execute("""CREATE TABLE IF NOT EXISTS source_cleanups (
                permit_id TEXT PRIMARY KEY, infohash TEXT NOT NULL,
                metadata_sha256 TEXT NOT NULL, pool_id TEXT NOT NULL,
                filesystem_id TEXT NOT NULL, destination TEXT NOT NULL,
                completed_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS source_cleanup_intents (
                permit_id TEXT PRIMARY KEY, infohash TEXT NOT NULL,
                metadata_sha256 TEXT NOT NULL, pool_id TEXT NOT NULL,
                filesystem_id TEXT NOT NULL, destination TEXT NOT NULL,
                requested_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS controller_pause_holds (
                permit_id TEXT PRIMARY KEY, infohash TEXT NOT NULL,
                metadata_sha256 TEXT, pool_id TEXT NOT NULL, filesystem_id TEXT,
                destination TEXT NOT NULL, reservation_id TEXT, scope_key TEXT,
                category TEXT NOT NULL, held_at TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS season_pack_episodes (
                parent_permit_id TEXT NOT NULL, scope_key TEXT NOT NULL,
                selected_files_json TEXT NOT NULL,
                PRIMARY KEY (parent_permit_id, scope_key))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS season_pack_identity_receipts (
                parent_permit_id TEXT PRIMARY KEY, infohash TEXT NOT NULL,
                metadata_sha256 TEXT NOT NULL, catalog_json TEXT NOT NULL,
                allow_absolute INTEGER NOT NULL)""")
            connection.execute("""CREATE TRIGGER IF NOT EXISTS remove_episode_identity_receipt
                AFTER DELETE ON gateway_permits BEGIN
                DELETE FROM season_pack_identity_receipts
                WHERE parent_permit_id=OLD.permit_id;
                END""")
            connection.commit()

    @staticmethod
    def _pending_bytes(
        connection: sqlite3.Connection,
        capacity: CapacityEvidence,
        *,
        include_infohash: str | None = None,
        pool_id: str = "ssd",
    ) -> int:
        rows = connection.execute(
            """SELECT p.infohash, p.budget_bytes, p.state FROM gateway_permits p
            JOIN reservations r ON r.id = p.reservation_id
            WHERE r.state IN ('reserved', 'downloading', 'waiting_episodes')
              AND (p.state IN ('authorized', 'dispatching', 'unknown', 'confirmed')
                   OR p.permit_id IN (SELECT parent_id FROM source_handovers WHERE state='pending'))
              AND (p.state != 'authorized' OR p.expires_at > ?)
              AND p.budget_bytes > 0
              AND p.pool_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM episode_imports e
                WHERE e.permit_id = p.permit_id AND e.state = 'complete'
              )""",
            (datetime.now(UTC).isoformat(), pool_id),
        ).fetchall()
        per_pool = capacity.pool(pool_id)
        # Registered queues retain commitments even after cancellation while
        # qBit still owns a stopped partial source. Progress counts once by hash.
        by_hash = dict(per_pool.remaining_by_hash) if capacity.pools else {}
        for row in rows:
            if (
                capacity.pools
                or row["state"] != "confirmed"
                or row["infohash"] not in capacity.paused_hashes
                or row["infohash"] == include_infohash
            ):
                by_hash[row["infohash"]] = max(
                    by_hash.get(row["infohash"], 0),
                    min(
                        row["budget_bytes"],
                        per_pool.remaining_by_hash.get(row["infohash"], row["budget_bytes"]),
                    ),
                )
        extra = connection.execute(
            "SELECT COALESCE(SUM(budget_bytes),0) FROM audited_derivations "
            "WHERE pool_id=? AND state IN ('reserved','writing','verified')", (pool_id,),
        ).fetchone()[0]
        return per_pool.other_pending_bytes + sum(by_hash.values()) + extra

    def pending_bytes(
        self,
        capacity: CapacityEvidence,
        *,
        include_infohash: str | None = None,
        pool_id: str = "ssd",
    ) -> int:
        if self._db_path is None:
            raise ValueError("persistent permits required for queue capacity")
        with self._session() as connection:
            return self._pending_bytes(
                connection, capacity, include_infohash=include_infohash, pool_id=pool_id
            )

    def reserve_audited_derivation(self, file_id: str, capacity: CapacityEvidence) -> dict:
        """Reserve only a persisted exact multipart job, under normal admission's lock.

        Call before gateway add. Retry reads the same reservation; it never creates
        a synthetic permit, changes a source budget or moves to another pool.
        """
        if self._db_path is None:
            raise PermissionError("persistent_derivation_required")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT f.file_json,j.job_json,m.manifest_json FROM audited_import_files f "
                    "JOIN audited_import_jobs j ON j.job_id=f.job_id "
                    "JOIN audited_import_manifests m ON m.singleton=1 WHERE f.file_id=?",
                    (file_id,),
                ).fetchone()
                if row is None:
                    raise PermissionError("sealed_derivation_required")
                target, job, manifest = (json.loads(row[key]) for key in
                                        ("file_json", "job_json", "manifest_json"))
                if job not in manifest["jobs"] or target not in job["files"] \
                        or "source_parts" not in target:
                    raise PermissionError("sealed_derivation_required")
                from homeserver_control.persistence.audited_imports import validate_manifest
                validate_manifest(manifest)
                raw = connection.execute(
                    "SELECT p.* FROM gateway_permits p "
                    "JOIN reservations r ON r.id=p.reservation_id "
                    "WHERE p.permit_id=? "
                    "AND r.state IN ('reserved','downloading','waiting_episodes')",
                    (job["permit_id"],),
                ).fetchone()
                if raw is None:
                    raise PermissionError("active_derivation_parent_required")
                permit = self._permit_from_row(raw)
                if (permit.state not in ("authorized", "confirmed") or permit.scope_key is not None
                        or permit.reservation_id != job["reservation_id"]
                        or permit.category != "sonarr" or job["category"] != "sonarr"
                        or permit.metadata_sha256 != job["metadata_sha256"]
                        or permit.budget_bytes != job["expected_payload_bytes"]
                        or set(permit.selected_files) != {p["path"] for p in job["payload_files"]}
                        or (permit.state == "authorized" and permit.expires_at <= datetime.now(UTC))
                        or not self.placement_valid(permit, writable=True)):
                    raise PermissionError("exact_derivation_parent_required")
                binding = json.dumps({"job_id": job["job_id"], "candidate_id": job["candidate_id"],
                                      "metadata_sha256": job["metadata_sha256"], "target": target},
                                     sort_keys=True, separators=(",", ":"))
                budget = target["derivation"]["max_output_bytes"]
                old = connection.execute("SELECT * FROM audited_derivations WHERE file_id=?",
                                         (file_id,)).fetchone()
                if old is not None:
                    if (old["binding_json"] != binding or old["permit_id"] != permit.permit_id
                            or old["reservation_id"] != permit.reservation_id
                            or old["pool_id"] != permit.pool_id
                            or old["filesystem_id"] != permit.filesystem_id
                            or old["budget_bytes"] != budget):
                        raise PermissionError("derivation_reservation_conflict")
                    connection.commit()
                    return decode_derivation(old)
                pool = capacity.pool(permit.pool_id)
                free = pool.usable_free_bytes
                if self.storage_registry is not None:
                    sample = self.storage_registry.inspect(permit.pool_id, writable=True)
                    if sample.filesystem_id != permit.filesystem_id \
                            or sample.filesystem_id != pool.pools[0].filesystem_id:
                        raise PermissionError("derivation_pool_identity_changed")
                    free = min(free, max(0, sample.free_bytes - pool.min_free_bytes))
                pending = self._pending_bytes(connection, capacity, pool_id=permit.pool_id)
                if budget > max(0, free - pending):
                    raise PermissionError("waiting_space")
                connection.execute(
                    "INSERT INTO audited_derivations "
                    "(file_id,permit_id,reservation_id,pool_id,filesystem_id,"
                    "budget_bytes,binding_json) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (file_id, permit.permit_id, permit.reservation_id, permit.pool_id,
                     permit.filesystem_id, budget, binding),
                )
                result = decode_derivation(connection.execute(
                    "SELECT * FROM audited_derivations WHERE file_id=?", (file_id,),
                ).fetchone())
                connection.commit()
                return result
            except Exception:
                connection.rollback()
                raise

    def available_bytes(self, capacity: CapacityEvidence) -> int:
        pools = [pool.pool_id for pool in capacity.pools] if capacity.pools else ["ssd"]
        return max(
            (
                max(0, capacity.pool(pool).usable_free_bytes
                    - self.pending_bytes(capacity, pool_id=pool))
                for pool in pools
            ),
            default=0,
        )

    def placement_valid(self, permit: Permit, *, writable: bool = True) -> bool:
        if self.storage_registry is None:
            return permit.pool_id == "ssd" and permit.destination == "/data/torrents"
        try:
            # The gateway deliberately has read-only physical binds. Inspection
            # still proves the host filesystem is RW; only the worker creates paths.
            sample = self.storage_registry.inspect(permit.pool_id)
            if permit.filesystem_id is not None and sample.filesystem_id != permit.filesystem_id:
                return False
            if permit.destination == "/data/torrents" and permit.pool_id == "ssd":
                return self.storage_registry.resolve("ssd", permit.destination).is_dir()
            expected = (
                f"/data/torrents/.placements/{permit.season_pack_parent_id or permit.permit_id}"
            )
            return (
                permit.destination == expected
                and self.storage_registry.validate_destination(
                    permit.pool_id, permit.season_pack_parent_id or permit.permit_id
                )
                == expected
            )
        except (RuntimeError, ValueError, OSError):
            return False

    def _select_pool(self, connection, permit: Permit, capacity: CapacityEvidence) -> None:
        if self.storage_registry is None:
            if capacity.pools:
                raise PermissionError("storage_registry_required")
            if permit.budget_bytes > max(
                0, capacity.usable_free_bytes - self._pending_bytes(connection, capacity)
            ):
                raise PermissionError("waiting_space")
            return
        for pool_id in self.storage_registry.pool_ids:
            try:
                pool = next(item for item in capacity.pools if item.pool_id == pool_id)
                sample = self.storage_registry.inspect(pool_id, writable=True)
                if sample.filesystem_id != pool.filesystem_id:
                    continue
                free = max(0, min(pool.free_bytes, sample.free_bytes) - pool.min_free_bytes)
                if permit.budget_bytes > max(
                    0, free - self._pending_bytes(connection, capacity, pool_id=pool_id)
                ):
                    continue
                destination = self.storage_registry.prepare_destination(pool_id, permit.permit_id)
            except (StopIteration, RuntimeError, ValueError, OSError):
                continue
            permit.pool_id = pool_id
            permit.filesystem_id = sample.filesystem_id
            permit.destination = destination
            return
        raise PermissionError("waiting_space")

    @staticmethod
    def _persist_placement(connection, permit: Permit) -> None:
        connection.execute(
            "UPDATE gateway_permits SET pool_id=?,filesystem_id=?,destination=? WHERE permit_id=?",
            (permit.pool_id, permit.filesystem_id, permit.destination, permit.permit_id),
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
            pool_id=row["pool_id"],
            filesystem_id=row["filesystem_id"],
            category=row["category"],
            selected_files=tuple(json.loads(row["selected_files_json"])),
            budget_bytes=row["budget_bytes"],
            reported_seeders=row["reported_seeders"],
            expires_at=PermitRegistry._expires(row["expires_at"]),
            state=row["state"],
            result=json.loads(row["result_json"]) if row["result_json"] is not None else None,
            probe_parent_id=row["probe_parent_id"],
            quality_rank=tuple(json.loads(row["quality_rank_json"]))
            if row["quality_rank_json"] is not None
            else None,
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
        if self.storage_registry is not None and capacity is None:
            raise ValueError("pool_capacity_required")
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
        if capacity is not None and (
            self._db_path is None
            or reservation_id is None
            or isinstance(budget_bytes, bool)
            or not isinstance(budget_bytes, int)
            or budget_bytes <= 0
        ):
            raise ValueError("exact-byte permit requires a persistent reservation and size")
        if self._db_path is None:
            with self._lock:
                if any(
                    item.reservation_id == reservation_id
                    and scope_key in self._season_pack_bindings.get(item.permit_id, {})
                    and item.state in {"authorized", "dispatching", "unknown", "confirmed"}
                    for item in self._permits.values()
                ):
                    raise PermissionError("episode_already_permitted")
                if reservation_id is not None and any(
                    item.reservation_id == reservation_id
                    and item.scope_key == scope_key
                    and item.state == "superseded"
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
                        not scope_key
                        or category != "sonarr"
                        or not reservation["media_key"].startswith("season:tmdb:")
                    ):
                        raise ValueError("invalid episode permit")
                    self._select_pool(connection, permit, capacity)
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
                        not scope_key
                        or category != "sonarr"
                        or reservation_id is None
                        or budget_bytes is None
                        or budget_bytes <= 0
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
                if (
                    reservation_id is not None
                    and connection.execute(
                        "SELECT 1 FROM gateway_permits WHERE reservation_id = ? "
                        "AND scope_key IS ? AND state = 'superseded' LIMIT 1",
                        (reservation_id, scope_key),
                    ).fetchone()
                ):
                    raise PermissionError("replacement_limit")
                if (
                    scope_key is not None
                    and connection.execute(
                        "SELECT 1 FROM season_pack_episodes e JOIN gateway_permits p "
                        "ON p.permit_id=e.parent_permit_id WHERE p.reservation_id=? "
                        "AND e.scope_key=? AND p.state IN "
                        "('authorized', 'dispatching', 'unknown', 'confirmed') LIMIT 1",
                        (reservation_id, scope_key),
                    ).fetchone()
                    is not None
                ):
                    raise PermissionError("episode_already_permitted")
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
                self._persist_placement(connection, permit)
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
                    and (self.storage_registry is not None or item.destination == destination)
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
                      AND (p.destination = ? OR ?) AND p.category = ?
                      AND p.state IN ('authorized', 'confirmed')
                      AND r.state IN ('reserved', 'downloading', 'waiting_episodes')
                      AND p.budget_bytes IS NOT NULL AND p.budget_bytes <= r.budget_bytes
                    """,
                    (
                        infohash.lower(),
                        digest,
                        destination,
                        self.storage_registry is not None,
                        category,
                    ),
                ).fetchall()
            matches = [
                self._permit_from_row(row)
                for row in rows
                if datetime.now(UTC) < self._expires(row["expires_at"])
            ]
        if len(matches) != 1:
            raise PermissionError("permit_required_or_ambiguous")
        return matches[0]

    def find_for_magnet(self, *, infohash: str, destination: str, category: str) -> Permit:
        """Resolve a magnet only after metadata and reservation were verified.

        The v1 infohash identifies the inspected torrent info dictionary. A
        headerless Arr request cannot choose a permit token or bypass its
        reserved budget.
        """
        if self._db_path is None:
            with self._lock:
                matches = [
                    item
                    for item in self._permits.values()
                    if item.infohash == infohash.lower()
                    and (self.storage_registry is not None or item.destination == destination)
                    and item.category == category
                    and item.metadata_sha256 is not None
                    and item.budget_bytes is not None
                    and item.budget_bytes > 0
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
                    WHERE p.infohash = ? AND (p.destination = ? OR ?) AND p.category = ?
                      AND p.state IN ('authorized', 'confirmed')
                      AND p.metadata_sha256 IS NOT NULL
                      AND p.budget_bytes > 0 AND p.budget_bytes <= r.budget_bytes
                      AND p.selected_files_json != '[]'
                      AND r.state IN ('reserved', 'downloading', 'waiting_episodes')
                    """,
                    (infohash.lower(), destination, self.storage_registry is not None, category),
                ).fetchall()
            matches = [
                self._permit_from_row(row)
                for row in rows
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
                "SELECT 1 FROM gateway_permits WHERE infohash = ? AND (state = 'confirmed' "
                "OR permit_id IN (SELECT parent_id FROM source_handovers "
                "WHERE state='pending')) LIMIT 1",
                (infohash.lower(),),
            ).fetchone()
        return row is not None

    def queue_entry(self, entry: dict) -> dict:
        """Attribute queue progress only to an identity-checked physical destination."""
        infohash = entry.get("hash")
        admitted = self.is_admitted(infohash) if isinstance(infohash, str) else False
        payload = {key: entry.get(key) for key in ("hash", "total_size", "amount_left", "state")}
        payload["admitted"] = admitted
        if self.storage_registry is None:
            return payload
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_permits WHERE infohash=? AND destination=? AND category=?",
                (
                    infohash.lower() if isinstance(infohash, str) else "",
                    entry.get("save_path", "").rstrip("/"),
                    entry.get("category"),
                ),
            ).fetchall()
        placements = {(row["pool_id"], row["filesystem_id"], row["destination"]) for row in rows}
        if len(placements) != 1:
            return {**payload, "placement_verified": False}
        permit = self._permit_from_row(rows[0])
        return {
            **payload,
            "admitted": True,
            "pool_id": permit.pool_id,
            "filesystem_id": permit.filesystem_id
            or self.storage_registry.pools[permit.pool_id].filesystem_id,
            "save_path": permit.destination,
            "reserved_bytes": permit.budget_bytes,
            "placement_verified": self.placement_valid(permit, writable=False),
        }

    def queue_entries(self, entries) -> list[dict]:
        """Use request-local pool proof while checking every physical placement."""
        if self.storage_registry is None:
            return [self.queue_entry(entry) for entry in entries]
        with self.storage_registry.read_snapshot() as storage:
            reader = copy(self)
            reader.storage_registry = storage
            result = [reader.queue_entry(entry) for entry in entries]
        return result

    @staticmethod
    def _season_pack_view(parent: Permit, scope: str, files: tuple[str, ...]) -> Permit:
        return replace(
            parent,
            permit_id=f"{parent.permit_id}:{scope}",
            scope_key=scope,
            selected_files=files,
            season_pack_parent_id=parent.permit_id,
        )

    @staticmethod
    def _normalize_episode_catalog(catalog, allow_absolute):
        if type(allow_absolute) is not bool or not isinstance(catalog, (list, tuple)):
            raise ValueError("invalid canonical episode identity evidence")
        if not catalog or len(catalog) > 20000:
            raise ValueError("invalid canonical episode identity evidence")
        rows = []
        for row in catalog:
            if not isinstance(row, dict):
                raise ValueError("invalid canonical episode identity evidence")
            season, episode = row.get("seasonNumber"), row.get("episodeNumber")
            if type(season) is int and season == 0:
                continue  # Specials cannot identify normal season payloads.
            if type(season) is not int or season <= 0 or type(episode) is not int or episode <= 0:
                raise ValueError("invalid canonical episode identity evidence")
            identity = {"seasonNumber": season, "episodeNumber": episode}
            absolute = row.get("absoluteEpisodeNumber")
            if absolute is not None and absolute != 0:
                if type(absolute) is not int or absolute <= 0:
                    raise ValueError("invalid canonical absolute episode evidence")
                identity["absoluteEpisodeNumber"] = absolute
            rows.append(identity)
        return json.dumps(rows, sort_keys=True), allow_absolute

    def _pack_identity_receipt(self, parent, connection=None):
        if self._db_path is None:
            return self._pack_identity_receipts.get(parent.permit_id)
        if connection is None:
            with self._session() as session:
                return self._pack_identity_receipt(parent, session)
        row = connection.execute(
            "SELECT * FROM season_pack_identity_receipts WHERE parent_permit_id=?",
            (parent.permit_id,),
        ).fetchone()
        if row is None:
            return None
        if row["infohash"] != parent.infohash or row["metadata_sha256"] != parent.metadata_sha256:
            raise ValueError("pack identity receipt differs from admitted metadata")
        return row["catalog_json"], bool(row["allow_absolute"])

    def _persist_pack_identity_receipt(self, connection, parent, receipt):
        if receipt is None:
            return
        previous = self._pack_identity_receipt(parent, connection)
        if previous is not None and previous != receipt:
            raise ValueError("pack identity evidence cannot change")
        if connection is None:
            self._pack_identity_receipts[parent.permit_id] = receipt
        else:
            connection.execute(
                "INSERT OR IGNORE INTO season_pack_identity_receipts VALUES (?,?,?,?,?)",
                (parent.permit_id, parent.infohash, parent.metadata_sha256, receipt[0], receipt[1]),
            )

    def _validate_pack_bindings(
        self, parent: Permit, episode_files: dict[str, tuple[str, ...]], *,
        episode_catalog=None, allow_absolute=False, connection=None,
    ) -> None:
        # Only trusted Python callers supply Sonarr's complete canonical catalog.
        # The immutable receipt binds contextual EP/absolute names to this torrent.
        receipt = (
            self._normalize_episode_catalog(episode_catalog, allow_absolute)
            if episode_catalog is not None else self._pack_identity_receipt(parent, connection)
        )
        pack = re.fullmatch(r"S([0-9]{2,})PACK", parent.scope_key or "")
        if (
            pack is None
            or parent.category != "sonarr"
            or parent.reservation_id is None
            or parent.probe_parent_id is not None
            or parent.metadata_sha256 is None
            or not self.placement_valid(parent)
            or parent.state not in {"authorized", "dispatching", "unknown", "confirmed"}
            or not isinstance(episode_files, dict)
            or not episode_files
        ):
            raise ValueError("invalid season pack bindings")
        for scope, files in episode_files.items():
            episode = re.fullmatch(r"S([0-9]{2,})E([0-9]{2,})", scope)
            if (
                episode is None
                or int(episode[1]) != int(pack[1])
                or int(episode[2]) <= 0
                or not isinstance(files, tuple)
                or not files
                or len(files) != len(set(files))
                or any(
                    not isinstance(path, str) or path not in parent.selected_files for path in files
                )
            ):
                raise ValueError("invalid season pack episode")
            videos = [
                path
                for path in files
                if PurePosixPath(path).suffix.lower()
                in {
                    ".mkv",
                    ".mp4",
                    ".avi",
                    ".m4v",
                    ".ts",
                    ".m2ts",
                    ".mov",
                    ".webm",
                }
            ]
            tag = re.compile(
                rf"(?<![a-z0-9])s0*{int(episode[1])}e0*{int(episode[2])}"
                r"(?![0-9]|e[0-9]|[ ._-]*[-e][0-9])",
                re.I,
            )
            identities_match = (
                all(resolve_episode_identity(
                    path, json.loads(receipt[0]), season=int(episode[1]),
                    allow_absolute=receipt[1],
                ) == (int(episode[1]), int(episode[2])) for path in files)
                if receipt is not None else
                all(tag.search(PurePosixPath(path).name) for path in files)
            )
            if len(videos) != 1 or not identities_match:
                raise ValueError("pack file belongs to another episode")

    def season_pack_bindings(self, token: str) -> dict[str, tuple[str, ...]]:
        parent = self.get(token)
        if parent is None or not re.fullmatch(r"S[0-9]{2,}PACK", parent.scope_key or ""):
            raise PermissionError("season pack source required")
        if self._db_path is None:
            return dict(self._season_pack_bindings.get(parent.permit_id, {}))
        with self._session() as connection:
            return self._pack_bindings(connection, parent.permit_id)

    def bind_episode_identity(self, token, *, episode_catalog, allow_absolute=False):
        """Bind trusted Sonarr identities before dispatching an individual episode."""
        receipt = self._normalize_episode_catalog(episode_catalog, allow_absolute)

        def validate(permit):
            scope = re.fullmatch(r"S([0-9]{2,})E([0-9]{2,})", permit.scope_key or "")
            if permit.category != "sonarr" or scope is None or permit.season_pack_parent_id:
                raise ValueError("episode source identity required")
            if not all(resolve_episode_identity(
                path, json.loads(receipt[0]), season=int(scope[1]), allow_absolute=receipt[1],
            ) == (int(scope[1]), int(scope[2])) for path in permit.selected_files):
                raise ValueError("source file belongs to another episode")

        if self._db_path is None:
            with self._lock:
                permit = self.get(token)
                if permit is None:
                    raise PermissionError("permit_required")
                validate(permit)
                self._persist_pack_identity_receipt(None, permit, receipt)
            return
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token=?", (token,),
            ).fetchone()
            if row is None:
                raise PermissionError("permit_required")
            permit = self._permit_from_row(row)
            validate(permit)
            self._persist_pack_identity_receipt(connection, permit, receipt)
            connection.commit()

    def episode_identity_matches(self, permit, path):
        """Recheck the original immutable receipt during import, including pack views."""
        scope = re.fullmatch(r"S([0-9]{2,})E([0-9]{2,})", permit.scope_key or "")
        if scope is None or path not in permit.selected_files:
            return False
        owner_id = permit.season_pack_parent_id or permit.permit_id
        if self._db_path is None:
            owner = next((p for p in self._permits.values() if p.permit_id == owner_id), None)
            receipt = self._pack_identity_receipt(owner) if owner else None
        else:
            with self._session() as connection:
                row = connection.execute(
                    "SELECT * FROM gateway_permits WHERE permit_id=?", (owner_id,),
                ).fetchone()
                owner = self._permit_from_row(row) if row else None
                receipt = self._pack_identity_receipt(owner, connection) if owner else None
                if owner and permit.season_pack_parent_id:
                    bindings = self._pack_bindings(connection, owner_id)
                    if bindings.get(permit.scope_key) != permit.selected_files:
                        return False
        if owner is None or (owner.infohash, owner.metadata_sha256) != (
            permit.infohash, permit.metadata_sha256,
        ):
            return False
        rows = json.loads(receipt[0]) if receipt else [{
            "seasonNumber": int(scope[1]), "episodeNumber": int(scope[2]),
        }]
        return resolve_episode_identity(
            path, rows, season=int(scope[1]) if receipt else None,
            allow_absolute=receipt[1] if receipt else False,
        ) == (int(scope[1]), int(scope[2]))

    @staticmethod
    def _pack_bindings(connection, permit_id: str) -> dict[str, tuple[str, ...]]:
        return {
            row["scope_key"]: tuple(json.loads(row["selected_files_json"]))
            for row in connection.execute(
                "SELECT * FROM season_pack_episodes WHERE parent_permit_id=?", (permit_id,),
            )
        }

    def _require_pack_replacement(
        self, connection, old, episode_files, quality_rank, *, allow_quality_change=False,
    ) -> None:
        self._require_pack_reservation(connection, old)
        previous = self._pack_bindings(connection, old.permit_id)
        if (
            not isinstance(episode_files, dict) or not previous
            or set(episode_files) != set(previous)
        ):
            raise PermissionError("replacement must preserve every pack episode binding")
        if connection.execute(
            "SELECT 1 FROM episode_imports WHERE permit_id=? OR permit_id LIKE ? LIMIT 1",
            (old.permit_id, f"{old.permit_id}:S%"),
        ).fetchone():
            raise PermissionError("import_started")
        if not allow_quality_change and old.quality_rank is not None and (
            quality_rank is None
            or any(new < floor for new, floor in zip(
                quality_rank[:2], old.quality_rank[:2], strict=True,
            ))
        ):
            raise PermissionError("quality_regression")
        for scope in previous:
            if connection.execute(
                "SELECT 1 FROM gateway_permits p LEFT JOIN season_pack_episodes e "
                "ON e.parent_permit_id=p.permit_id WHERE p.permit_id!=? "
                "AND p.reservation_id=? AND (p.scope_key=? OR e.scope_key=?) "
                "AND p.state IN ('authorized','dispatching','unknown','confirmed') LIMIT 1",
                (old.permit_id, old.reservation_id, scope, scope),
            ).fetchone():
                raise PermissionError("episode_already_permitted")

    def _validate_pack_replacements(
        self, connection, parent: Permit, episode_files, tokens, capacity
    ) -> list[Permit]:
        if (
            parent.state != "authorized"
            or parent.expires_at <= datetime.now(UTC)
            or parent.result is not None
            or not isinstance(parent.budget_bytes, int)
            or parent.budget_bytes <= 0
        ):
            raise PermissionError("unused_pack_permit_required")
        self._require_probe_reservation(connection, parent)
        for scope, files in episode_files.items():
            self._require_probe_reservation(
                connection, self._season_pack_view(parent, scope, files)
            )
        tables = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        old_sources = []
        for token in tokens:
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token=?", (token,)
            ).fetchone()
            old = self._permit_from_row(row) if row is not None else None
            if (
                old is None or old.state != "confirmed" or old.probe_parent_id is not None
                or old.reservation_id != parent.reservation_id
                or old.scope_key not in episode_files or old.category != "sonarr"
                or old.infohash == parent.infohash or not self.placement_valid(old)
                or connection.execute(
                    "SELECT 1 FROM season_pack_episodes WHERE parent_permit_id=?",
                    (old.permit_id,),
                ).fetchone()
            ):
                raise PermissionError("confirmed_episode_source_required")
            if old.quality_rank is not None and (
                parent.quality_rank is None or parent.quality_rank < old.quality_rank
                or any(new < current for new, current in zip(
                    parent.quality_rank[:2], old.quality_rank[:2], strict=True
                ))
            ):
                raise PermissionError("quality_regression")
            self._require_probe_reservation(connection, old)
            try:
                pool = capacity.pool(old.pool_id)
            except ValueError as error:
                raise PermissionError("source_capacity_unavailable") from error
            if (
                old.infohash not in pool.paused_hashes
                or pool.remaining_by_hash.get(old.infohash, 0) <= 0
            ):
                raise PermissionError("paused_incomplete_source_required")
            if capacity.pools and (
                pool.pools[0].filesystem_id != old.filesystem_id
            ):
                raise PermissionError("source_capacity_unavailable")
            if connection.execute(
                "SELECT 1 FROM episode_imports WHERE permit_id=? LIMIT 1", (old.permit_id,)
            ).fetchone():
                raise PermissionError("import_started")
            if "protected_sources" in tables and connection.execute(
                "SELECT 1 FROM protected_sources WHERE infohash=? COLLATE NOCASE LIMIT 1",
                (old.infohash,),
            ).fetchone():
                raise PermissionError("source_protected")
            if connection.execute(
                "SELECT 1 FROM gateway_permits WHERE probe_parent_id=? "
                "AND state IN ('authorized','dispatching','unknown','confirmed') LIMIT 1",
                (old.permit_id,),
            ).fetchone() or connection.execute(
                "SELECT 1 FROM source_handovers WHERE state='pending' "
                "AND (parent_id=? OR candidate_id=?) LIMIT 1",
                (old.permit_id, old.permit_id),
            ).fetchone():
                raise PermissionError("source_transition_pending")
            if "source_probes" in tables and connection.execute(
                "SELECT 1 FROM source_probes s JOIN gateway_permits p "
                "ON p.permit_id=s.candidate_id WHERE s.parent_id=? AND s.decision!='reject' "
                "AND p.state IN ('authorized','dispatching','unknown','confirmed') LIMIT 1",
                (old.permit_id,),
            ).fetchone():
                raise PermissionError("source_transition_pending")
            old_sources.append(old)
        for scope in episode_files:
            history = connection.execute(
                "SELECT DISTINCT p.* FROM gateway_permits p LEFT JOIN season_pack_episodes e "
                "ON e.parent_permit_id=p.permit_id WHERE p.reservation_id=? "
                "AND (p.scope_key=? OR e.scope_key=?) "
                "AND p.state IN ('superseded','probe_rejected')",
                (parent.reservation_id, scope, scope),
            ).fetchall()
            for row in history:
                try:
                    pool = capacity.pool(row["pool_id"])
                except ValueError as error:
                    raise PermissionError("history_capacity_unavailable") from error
                if capacity.pools and pool.pools[0].filesystem_id != row["filesystem_id"]:
                    raise PermissionError("history_capacity_unavailable")
                # Absent from the progress map is ambiguous: non-admitted
                # torrents may contribute only to other_pending_bytes. Require
                # an explicit zero or positive proof of the stopped hash.
                if not self._historical_source_stopped(
                    connection, self._permit_from_row(row), capacity,
                ):
                    raise PermissionError("historical_source_not_stopped")
        try:
            pool = capacity.pool(parent.pool_id)
        except ValueError as error:
            raise PermissionError("pool_capacity_unavailable") from error
        if capacity.pools and pool.pools[0].filesystem_id != parent.filesystem_id:
            raise PermissionError("pool_capacity_unavailable")
        pending = self._pending_bytes(connection, capacity, pool_id=parent.pool_id)
        if not capacity.pools:
            # Legacy accounting normally excludes paused confirmed sources. A
            # replacement must keep their remaining commitments in this check.
            pending += sum(pool.remaining_by_hash[old.infohash] for old in old_sources)
        if pending > pool.usable_free_bytes:
            raise PermissionError("waiting_space")
        return old_sources

    def bind_season_pack(
        self, token: str, *, episode_files: dict[str, tuple[str, ...]],
        replacement_tokens: tuple[str, ...] = (), capacity: CapacityEvidence | None = None,
        episode_catalog=None, allow_absolute=False,
    ) -> None:
        """Persist import identities without creating extra admission/capacity rows."""
        if (
            not isinstance(replacement_tokens, tuple)
            or any(not isinstance(item, str) or not item for item in replacement_tokens)
            or len(set(replacement_tokens)) != len(replacement_tokens)
            or replacement_tokens and (
                self._db_path is None or not isinstance(capacity, CapacityEvidence)
            )
        ):
            raise ValueError("persistent_pack_replacement_evidence_required")
        parent = self.get(token)
        if parent is None:
            raise PermissionError("permit_required")
        receipt = (
            self._normalize_episode_catalog(episode_catalog, allow_absolute)
            if episode_catalog is not None else None
        )
        self._validate_pack_bindings(
            parent, episode_files, episode_catalog=episode_catalog, allow_absolute=allow_absolute,
        )
        if self._db_path is None:
            with self._lock:
                previous = self._season_pack_bindings.get(parent.permit_id)
                if previous is not None and previous != episode_files:
                    raise ValueError("season pack bindings cannot change")
                if any(
                    item.reservation_id == parent.reservation_id
                    and item.scope_key in episode_files
                    and item.state in {"authorized", "dispatching", "unknown", "confirmed"}
                    for item in self._permits.values()
                ):
                    raise PermissionError("episode_already_permitted")
                self._persist_pack_identity_receipt(None, parent, receipt)
                self._season_pack_bindings[parent.permit_id] = dict(episode_files)
            return
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT p.*, r.media_key FROM gateway_permits p "
                "JOIN reservations r ON r.id=p.reservation_id WHERE p.token=? "
                "AND r.state IN ('reserved', 'downloading', 'waiting_episodes')",
                (token,),
            ).fetchone()
            if row is None:
                raise PermissionError("reservation_required")
            parent = self._permit_from_row(row)
            self._validate_pack_bindings(
                parent, episode_files, episode_catalog=episode_catalog,
                allow_absolute=allow_absolute, connection=connection,
            )
            season = re.fullmatch(r"season:tmdb:[1-9][0-9]*:([1-9][0-9]*)", row["media_key"])
            if season is None or parent.scope_key != f"S{int(season[1]):02d}PACK":
                raise ValueError("pack belongs to another season")
            previous = {
                entry["scope_key"]: tuple(json.loads(entry["selected_files_json"]))
                for entry in connection.execute(
                    "SELECT * FROM season_pack_episodes WHERE parent_permit_id=?",
                    (parent.permit_id,),
                )
            }
            if previous and previous != episode_files:
                raise ValueError("season pack bindings cannot change")
            old_sources = []
            if replacement_tokens:
                if previous:
                    raise PermissionError("pack_already_bound")
                old_sources = self._validate_pack_replacements(
                    connection, parent, episode_files, replacement_tokens, capacity
                )
            replacement_ids = {old.permit_id for old in old_sources}
            for scope in episode_files:
                others = connection.execute(
                    "SELECT p.permit_id FROM gateway_permits p LEFT JOIN season_pack_episodes e "
                    "ON e.parent_permit_id=p.permit_id WHERE p.permit_id != ? "
                    "AND p.state IN ('authorized', 'dispatching', 'unknown', 'confirmed') "
                    "AND p.reservation_id=? AND (p.scope_key=? OR e.scope_key=?)",
                    (parent.permit_id, parent.reservation_id, scope, scope),
                ).fetchall()
                if any(other["permit_id"] not in replacement_ids for other in others):
                    raise PermissionError("episode_already_permitted")
            if (
                connection.execute(
                    "SELECT 1 FROM gateway_permits WHERE infohash=? AND permit_id != ? "
                    "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed') LIMIT 1",
                    (parent.infohash, parent.permit_id),
                ).fetchone()
                is not None
            ):
                raise PermissionError("torrent_already_permitted")
            for old in old_sources:
                connection.execute(
                    "UPDATE gateway_permits SET state='superseded' "
                    "WHERE permit_id=? AND state='confirmed'", (old.permit_id,),
                )
            connection.executemany(
                "INSERT OR IGNORE INTO season_pack_episodes "
                "(parent_permit_id, scope_key, selected_files_json) VALUES (?, ?, ?)",
                [
                    (parent.permit_id, scope, json.dumps(files))
                    for scope, files in episode_files.items()
                ],
            )
            if old_sources:
                self._update_probe_budget(connection, parent.reservation_id)
            self._persist_pack_identity_receipt(connection, parent, receipt)
            connection.commit()

    def get_for_reservation(
        self, reservation_id: str, *, scope_key: str | None = None
    ) -> Permit | None:
        if self._db_path is None:
            with self._lock:
                physical = next(
                    (
                        item
                        for item in self._permits.values()
                        if item.reservation_id == reservation_id
                        and item.scope_key == scope_key
                        and item.probe_parent_id is None
                        and item.state in {"authorized", "dispatching", "unknown", "confirmed"}
                    ),
                    None,
                )
                if physical is not None:
                    return physical
                for parent in self._permits.values():
                    files = self._season_pack_bindings.get(parent.permit_id, {}).get(scope_key)
                    if (
                        files is not None
                        and parent.reservation_id == reservation_id
                        and parent.state in {"authorized", "dispatching", "unknown", "confirmed"}
                    ):
                        return self._season_pack_view(parent, scope_key, files)
                return None
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? AND probe_parent_id IS NULL AND state IN "
                "('authorized', 'dispatching', 'unknown', 'confirmed')",
                (reservation_id, scope_key),
            ).fetchone()
            if row is None:
                binding = connection.execute(
                    "SELECT p.*, e.scope_key AS episode_scope, "
                    "e.selected_files_json AS episode_files "
                    "FROM season_pack_episodes e JOIN gateway_permits p "
                    "ON p.permit_id=e.parent_permit_id WHERE p.reservation_id=? AND e.scope_key=? "
                    "AND p.probe_parent_id IS NULL "
                    "AND p.state IN ('authorized', 'dispatching', 'unknown', 'confirmed')",
                    (reservation_id, scope_key),
                ).fetchone()
                if binding is not None:
                    return self._season_pack_view(
                        self._permit_from_row(binding),
                        binding["episode_scope"],
                        tuple(json.loads(binding["episode_files"])),
                    )
        return self._permit_from_row(row) if row is not None else None

    @staticmethod
    def _pause_identity(permit: Permit) -> tuple:
        return (permit.permit_id, permit.infohash, permit.metadata_sha256,
                permit.pool_id, permit.filesystem_id, permit.destination,
                permit.reservation_id, permit.scope_key, permit.category)

    def _controller_pause_held(self, connection, permit: Permit) -> bool:
        return connection.execute(
            "SELECT 1 FROM controller_pause_holds WHERE permit_id=? AND infohash=? "
            "AND metadata_sha256 IS ? AND pool_id=? AND filesystem_id IS ? "
            "AND destination=? AND reservation_id IS ? AND scope_key IS ? AND category=?",
            self._pause_identity(permit),
        ).fetchone() is not None

    def controller_pause_held(self, permit: Permit) -> bool:
        if self._db_path is None:
            with self._lock:
                current = self._permits.get(permit.token)
                return (
                    current is not None and current.state == "confirmed"
                    and self._controller_pause_holds.get(permit.permit_id)
                    == self._pause_identity(permit)
                )
        with self._session() as connection:
            row = connection.execute(
                "SELECT state FROM gateway_permits WHERE token=?", (permit.token,),
            ).fetchone()
            return bool(row and row[0] == "confirmed"
                        and self._controller_pause_held(connection, permit))

    def mark_controller_pause(self, permit: Permit) -> None:
        """Commit a policy pause before STOP, including uncertain native effects."""
        if permit.category != "sonarr" or not re.fullmatch(
            r"S[0-9]{2,}E[0-9]{2,}", permit.scope_key or "",
        ):
            raise PermissionError("confirmed episode source required")
        if self._db_path is None:
            with self._lock:
                if self._permits.get(permit.token) != permit or permit.state != "confirmed":
                    raise PermissionError("current episode source changed")
                self._controller_pause_holds[permit.permit_id] = self._pause_identity(permit)
            return
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token=? AND state='confirmed'",
                (permit.token,),
            ).fetchone()
            if row is None or self._permit_from_row(row) != permit:
                raise PermissionError("current episode source changed")
            self._require_probe_reservation(connection, permit)
            if self._source_cleanup_intended(connection, permit):
                raise PermissionError("current source cleanup is pending")
            connection.execute(
                "INSERT OR REPLACE INTO controller_pause_holds VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*self._pause_identity(permit), datetime.now(UTC).isoformat()),
            )
            connection.commit()

    def clear_controller_pause(self, permit: Permit) -> None:
        if self._db_path is None:
            with self._lock:
                if (
                    self._controller_pause_holds.get(permit.permit_id)
                    == self._pause_identity(permit)
                ):
                    self._controller_pause_holds.pop(permit.permit_id)
            return
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token=? AND state='confirmed'",
                (permit.token,),
            ).fetchone()
            if row is None or self._permit_from_row(row) != permit:
                raise PermissionError("current episode source changed")
            self._require_probe_reservation(connection, permit)
            if self._controller_pause_held(connection, permit):
                connection.execute(
                    "DELETE FROM controller_pause_holds WHERE permit_id=?", (permit.permit_id,),
                )
            connection.commit()

    def list_discarded_sources(
        self, *, limit: int = 100, after_id: str | None = None,
        stopped_infohashes: frozenset[str] | None = None,
    ) -> list[Permit]:
        """List durable historical sources; the gateway still proves deletability."""
        if self._db_path is None:
            return []
        stopped = json.dumps(sorted(stopped_infohashes)) if stopped_infohashes is not None else None
        with self._session() as connection:
            rows = connection.execute(
                "SELECT p.* FROM gateway_permits p "
                "WHERE (p.state IN ('probe_rejected','superseded') OR "
                "(p.state='confirmed' AND p.probe_parent_id IS NULL "
                "AND (? IS NULL OR p.infohash IN (SELECT value FROM json_each(?)) OR EXISTS ("
                "SELECT 1 FROM source_cleanup_intents i WHERE i.permit_id=p.permit_id "
                "AND i.infohash=p.infohash AND i.metadata_sha256=p.metadata_sha256 "
                "AND i.pool_id=p.pool_id AND i.filesystem_id=p.filesystem_id "
                "AND i.destination=p.destination)) "
                "AND NOT EXISTS (SELECT 1 FROM controller_pause_holds h "
                "WHERE h.permit_id=p.permit_id AND h.infohash=p.infohash "
                "AND h.metadata_sha256 IS p.metadata_sha256 AND h.pool_id=p.pool_id "
                "AND h.filesystem_id IS p.filesystem_id AND h.destination=p.destination "
                "AND h.reservation_id IS p.reservation_id AND h.scope_key IS p.scope_key "
                "AND h.category=p.category) "
                "AND NOT EXISTS (SELECT 1 FROM movie_imports m "
                "WHERE m.reservation_id=p.reservation_id) "
                "AND NOT EXISTS (SELECT 1 FROM episode_imports e "
                "WHERE e.permit_id=p.permit_id OR e.permit_id LIKE p.permit_id || ':S%'))) "
                "AND (? IS NULL OR p.permit_id>?) AND NOT EXISTS ("
                "SELECT 1 FROM source_cleanups c WHERE c.permit_id=p.permit_id "
                "AND c.infohash=p.infohash AND c.metadata_sha256=p.metadata_sha256 "
                "AND c.pool_id=p.pool_id AND c.filesystem_id=p.filesystem_id "
                "AND c.destination=p.destination) ORDER BY p.permit_id LIMIT ?",
                (stopped, stopped, after_id, after_id, limit),
            ).fetchall()
        return [self._permit_from_row(row) for row in rows]

    def discarded_source(self, token: str) -> Permit:
        """Resolve a discarded trial without granting deletion of active media.

        An exclusive registered placement prevents a historical torrent from
        sharing its payload directory with another source. Keep historical
        states intact: acquisition must never pick these hashes again.
        """
        if self._db_path is None or self.storage_registry is None:
            raise PermissionError("persistent exclusive source required")
        with self._session() as connection:
            return self._discarded_source(connection, token)

    def _discarded_source(self, connection, token: str) -> Permit:
        row = connection.execute(
            "SELECT p.*,r.media_key FROM gateway_permits p JOIN reservations r "
            "ON r.id=p.reservation_id WHERE p.token=? "
            "AND p.state IN ('probe_rejected','superseded','confirmed','revoked')", (token,),
        ).fetchone()
        if row is None:
            raise PermissionError("discarded source required")
        permit = self._permit_from_row(row)
        if permit.state == "revoked" and not self._source_cleanup_complete(connection, permit):
            raise PermissionError("discarded source required")
        season = re.fullmatch(r"season:tmdb:[1-9][0-9]*:([1-9][0-9]*)", row["media_key"])
        episode = re.fullmatch(r"S([0-9]{2,})(?:E[0-9]{2,}|PACK)", permit.scope_key or "")
        if (
            permit.destination != f"/data/torrents/.placements/{permit.permit_id}"
            or permit.filesystem_id is None
            or not re.fullmatch(r"[0-9a-f]{40}", permit.infohash)
            or permit.category not in {"sonarr", "radarr"}
            or permit.reservation_id is None
            or permit.metadata_sha256 is None or not permit.selected_files
            or isinstance(permit.budget_bytes, bool)
            or not isinstance(permit.budget_bytes, int) or permit.budget_bytes <= 0
            or permit.category == "radarr" and (
                permit.scope_key is not None
                or not re.fullmatch(r"movie:tmdb:[1-9][0-9]*", row["media_key"])
            )
            or permit.category == "sonarr" and (
                season is None or episode is None or int(season[1]) != int(episode[1])
            )
        ):
            raise PermissionError("exclusive verified source required")
        if connection.execute(
            "SELECT 1 FROM gateway_permits WHERE (infohash=? OR destination=?) "
            "AND permit_id!=? "
            "AND state IN ('authorized','dispatching','unknown','confirmed') LIMIT 1",
            (permit.infohash, permit.destination, permit.permit_id),
        ).fetchone():
            raise ValueError("source has another active owner")
        if connection.execute(
            "SELECT 1 FROM source_handovers WHERE state='pending' "
            "AND (parent_id=? OR candidate_id=?) LIMIT 1",
            (permit.permit_id, permit.permit_id),
        ).fetchone() or connection.execute(
            "SELECT 1 FROM gateway_permits WHERE probe_parent_id=? "
            "AND state IN ('authorized','dispatching','unknown','confirmed') LIMIT 1",
            (permit.permit_id,),
        ).fetchone():
            raise ValueError("source transition still pending")
        if connection.execute(
            "SELECT 1 FROM episode_imports WHERE permit_id=? OR permit_id LIKE ? LIMIT 1",
            (permit.permit_id, f"{permit.permit_id}:S%"),
        ).fetchone():
            raise ValueError("episode source import has started")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='source_replacement_requests'",
        ).fetchone() and connection.execute(
            "SELECT 1 FROM source_replacement_requests "
            "WHERE (permit_id=? OR infohash=?) AND state IN ('queued','running') LIMIT 1",
            (permit.permit_id, permit.infohash),
        ).fetchone():
            raise ValueError("manual source replacement is pending")
        if permit.state == "confirmed":
            if self._controller_pause_held(connection, permit):
                raise ValueError("source paused by controller")
            if permit.probe_parent_id is not None:
                raise ValueError("active probe source cannot be cleaned")
            if (permit.scope_key or "").endswith("PACK"):
                raise ValueError("season pack cleanup requires review")
            if permit.category == "radarr" and connection.execute(
                "SELECT 1 FROM movie_imports WHERE reservation_id=? LIMIT 1",
                (permit.reservation_id,),
            ).fetchone():
                raise ValueError("movie source import has started")
        return permit

    def _source_cleanup_intended(self, connection, permit: Permit) -> bool:
        return connection.execute(
            "SELECT 1 FROM source_cleanup_intents WHERE permit_id=? AND infohash=? "
            "AND metadata_sha256=? AND pool_id=? AND filesystem_id=? AND destination=?",
            (permit.permit_id, permit.infohash, permit.metadata_sha256,
             permit.pool_id, permit.filesystem_id, permit.destination),
        ).fetchone() is not None

    def source_cleanup_intended(self, permit: Permit) -> bool:
        if self._db_path is None:
            return False
        with self._session() as connection:
            return self._source_cleanup_intended(connection, permit)

    def prepare_discard_source(
        self, token: str, check: Callable[[Permit, bool], None],
    ) -> None:
        """Commit cancellation before an asynchronous native delete can remove the hash."""
        if self._db_path is None or self.storage_registry is None:
            raise PermissionError("persistent exclusive source required")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            permit = self._discarded_source(connection, token)
            intended = self._source_cleanup_intended(connection, permit)
            check(permit, intended)
            if permit.state != "confirmed":
                raise ValueError("current source owner changed")
            if not intended:
                now = datetime.now(UTC).isoformat()
                if permit.category == "radarr":
                    connection.execute(
                        "UPDATE requests SET state='cancel_requested', updated_at=? "
                        "WHERE id=(SELECT request_id FROM reservations WHERE id=?) "
                        "AND state!='cancelled'", (now, permit.reservation_id),
                    )
                else:
                    media = connection.execute(
                        "SELECT media_key FROM reservations WHERE id=?",
                        (permit.reservation_id,),
                    ).fetchone()[0]
                    series_id = re.fullmatch(r"season:tmdb:([1-9][0-9]*):[1-9][0-9]*", media)[1]
                    connection.execute(
                        "INSERT OR IGNORE INTO tombstones VALUES (?,?,?)",
                        (f"episode:tmdb:{series_id}:{permit.scope_key}", now, permit.permit_id),
                    )
                connection.execute(
                    "INSERT OR REPLACE INTO source_cleanup_intents VALUES (?,?,?,?,?,?,?)",
                    (permit.permit_id, permit.infohash, permit.metadata_sha256,
                     permit.pool_id, permit.filesystem_id, permit.destination, now),
                )
            connection.commit()

    def discard_source(self, token: str, effect: Callable[[Permit], dict]) -> dict:
        """Hold ownership stable through the native effect and physical confirmation."""
        if self._db_path is None or self.storage_registry is None:
            raise PermissionError("persistent exclusive source required")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            permit = self._discarded_source(connection, token)
            if (
                permit.state == "confirmed"
                and not self._source_cleanup_intended(connection, permit)
            ):
                raise ValueError("current source cleanup intent required")
            result = effect(permit)
            if result not in ({"state": "deleted"}, {"state": "missing"}):
                raise ValueError("cleanup completion was not verified")
            connection.execute(
                "INSERT OR REPLACE INTO source_cleanups "
                "VALUES (?,?,?,?,?,?,?)", (
                    permit.permit_id, permit.infohash, permit.metadata_sha256,
                    permit.pool_id, permit.filesystem_id, permit.destination,
                    datetime.now(UTC).isoformat(),
                ),
            )
            if permit.state == "confirmed":
                connection.execute(
                    "UPDATE gateway_permits SET state='revoked' WHERE permit_id=?",
                    (permit.permit_id,),
                )
                if permit.category == "radarr":
                    connection.execute(
                        "UPDATE requests SET state='cancelled', updated_at=? "
                        "WHERE id=(SELECT request_id FROM reservations WHERE id=?)",
                        (datetime.now(UTC).isoformat(), permit.reservation_id),
                    )
                    connection.execute(
                        "UPDATE reservations SET state='cancelled',budget_bytes=0 WHERE id=?",
                        (permit.reservation_id,),
                    )
                else:
                    self._update_probe_budget(connection, permit.reservation_id)
            connection.commit()
            return result

    def _source_cleanup_complete(self, connection, permit: Permit) -> bool:
        return connection.execute(
            "SELECT 1 FROM source_cleanups WHERE permit_id=? AND infohash=? "
            "AND metadata_sha256=? AND pool_id=? AND filesystem_id=? AND destination=?",
            (permit.permit_id, permit.infohash, permit.metadata_sha256,
             permit.pool_id, permit.filesystem_id, permit.destination),
        ).fetchone() is not None

    def source_cleanup_complete(self, permit: Permit) -> bool:
        if self._db_path is None:
            return False
        with self._session() as connection:
            return self._source_cleanup_complete(connection, permit)

    def _historical_source_stopped(self, connection, permit, capacity) -> bool:
        try:
            pool = capacity.pool(permit.pool_id)
        except ValueError:
            return False
        if (
            permit.infohash in pool.paused_hashes
            or pool.remaining_by_hash.get(permit.infohash) == 0
        ):
            return True
        return (
            permit.infohash not in pool.remaining_by_hash
            and self._source_cleanup_complete(connection, permit)
        )

    def deletion_source(self, *, token: str, media_key: str, scope_key: str | None) -> Permit:
        """Resolve a verified source for an exact media/episode deletion.

        The in-memory registry has no authoritative reservation media key, so
        deletion is deliberately unavailable without the persistent database.
        Historical permits for another scope may still share a torrent hash;
        fail closed if any other live permit can own the same payload.
        Superseded/rejected episode sources are removable only after explicit
        whole-season deletion, so an ordinary episode or movie delete cannot
        expand its scope into download history.
        """
        if (
            self._db_path is None
            or not isinstance(token, str)
            or not isinstance(media_key, str)
            or (scope_key is not None and not isinstance(scope_key, str))
        ):
            raise PermissionError("persistent deletion source required")
        with self._session() as connection:
            row = connection.execute(
                "SELECT p.* FROM gateway_permits p "
                "JOIN reservations r ON r.id = p.reservation_id "
                "WHERE p.token = ? AND p.state IN ('confirmed','superseded','probe_rejected') "
                "AND r.media_key = ? AND p.scope_key IS ?",
                (token, media_key, scope_key),
            ).fetchone()
            if row is None:
                raise PermissionError("confirmed media source required")
            permit = self._permit_from_row(row)
            season = re.fullmatch(r"season:tmdb:[1-9][0-9]*:([1-9][0-9]*)", media_key)
            episode = re.fullmatch(r"S([0-9]{2,})E[0-9]{2,}", scope_key or "")
            if permit.state != "confirmed" and (
                permit.category != "sonarr"
                or season is None
                or episode is None
                or int(season[1]) != int(episode[1])
                or connection.execute(
                    "SELECT 1 FROM tombstones WHERE media_key=?",
                    (media_key,),
                ).fetchone()
                is None
            ):
                raise PermissionError("explicit season deletion required for historical source")
            if (
                not self.placement_valid(permit, writable=False)
                or not re.fullmatch(r"[0-9a-f]{40}", permit.infohash)
                or permit.metadata_sha256 is None
                or not permit.selected_files
                or (
                    scope_key is None
                    and (
                        permit.category != "radarr"
                        or not re.fullmatch(r"movie:tmdb:[1-9][0-9]*", media_key)
                    )
                )
                or (
                    scope_key is not None
                    and (
                        permit.category != "sonarr"
                        or season is None
                        or episode is None
                        or int(season[1]) != int(episode[1])
                    )
                )
            ):
                raise PermissionError("source identity is not deletable")
            other = connection.execute(
                "SELECT 1 FROM gateway_permits WHERE infohash = ? "
                "AND permit_id != ? AND state IN "
                "('authorized', 'dispatching', 'unknown', 'confirmed') LIMIT 1",
                (permit.infohash, permit.permit_id),
            ).fetchone()
            if other is not None:
                raise ValueError("torrent is shared by another active permit")
        return permit

    def _pack_deletion_parent(
        self,
        connection: sqlite3.Connection,
        *,
        token: str,
        media_key: str,
    ) -> tuple[Permit, dict[str, tuple[str, ...]]]:
        if not isinstance(token, str) or not isinstance(media_key, str):
            raise PermissionError("confirmed pack source required")
        row = connection.execute(
            "SELECT p.* FROM gateway_permits p "
                    "JOIN reservations r ON r.id=p.reservation_id "
            "WHERE p.token=? AND p.state='confirmed' AND r.media_key=?",
            (token, media_key),
        ).fetchone()
        season = re.fullmatch(r"season:tmdb:([1-9][0-9]*):([1-9][0-9]*)", media_key)
        if row is None or season is None:
            raise PermissionError("confirmed pack source required")
        parent = self._permit_from_row(row)
        if (
            parent.scope_key != f"S{int(season[2]):02d}PACK"
            or not re.fullmatch(r"[0-9a-f]{40}", parent.infohash)
            or not isinstance(parent.budget_bytes, int)
            or parent.budget_bytes <= 0
        ):
            raise PermissionError("pack source identity is invalid")
        bindings = {
            entry["scope_key"]: tuple(json.loads(entry["selected_files_json"]))
            for entry in connection.execute(
                "SELECT * FROM season_pack_episodes WHERE parent_permit_id=?",
                (parent.permit_id,),
            )
        }
        self._validate_pack_bindings(parent, bindings, connection=connection)
        if (
            connection.execute(
                "SELECT 1 FROM gateway_permits WHERE infohash=? AND permit_id != ? "
                "AND state IN ('authorized', 'dispatching', 'unknown', 'confirmed') LIMIT 1",
                (parent.infohash, parent.permit_id),
            ).fetchone()
            is not None
        ):
            raise ValueError("torrent is shared by another active permit")
        return parent, bindings

    def season_pack_deletion_source(
        self,
        *,
        token: str,
        media_key: str,
        scope_key: str,
    ) -> Permit:
        """Associate an explicit episode deletion without authorizing a pack delete."""
        if self._db_path is None:
            raise PermissionError("persistent pack source required")
        with self._session() as connection:
            parent, bindings = self._pack_deletion_parent(
                connection,
                token=token,
                media_key=media_key,
            )
            if scope_key not in bindings:
                raise PermissionError("confirmed pack episode required")
            return self._season_pack_view(parent, scope_key, bindings[scope_key])

    def season_pack_fully_deleted(self, *, token: str, media_key: str) -> bool:
        """Only all explicit child tombstones make the entire source removable."""
        if self._db_path is None:
            return False
        with self._session() as connection:
            try:
                _, bindings = self._pack_deletion_parent(
                    connection,
                    token=token,
                    media_key=media_key,
                )
            except (PermissionError, ValueError):
                return False
            tmdb = media_key.split(":")[2]
            return all(
                connection.execute(
                    "SELECT 1 FROM tombstones WHERE media_key=?",
                    (f"episode:tmdb:{tmdb}:{scope}",),
                ).fetchone()
                is not None
                for scope in bindings
            )

    def list_active_confirmed_movies(self) -> list[Permit]:
        """Return admitted movie sources whose reservations are still active."""
        if self._db_path is None:
            with self._lock:
                return [
                    permit
                    for permit in self._permits.values()
                    if permit.state == "confirmed"
                    and permit.category == "radarr"
                    and permit.probe_parent_id is None
                    and permit.scope_key is None
                    and permit.reservation_id is not None
                ]
        with self._session() as connection:
            rows = connection.execute(
                "SELECT p.* FROM gateway_permits p "
                "JOIN reservations r ON r.id = p.reservation_id "
                "WHERE p.state = 'confirmed' AND p.category = 'radarr' "
                "AND p.probe_parent_id IS NULL "
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
                episodes = []
                for permit in self._permits.values():
                    if (
                        permit.state != "confirmed"
                        or permit.category != "sonarr"
                        or permit.probe_parent_id is not None
                        or permit.scope_key is None
                        or permit.reservation_id is None
                    ):
                        continue
                    bindings = self._season_pack_bindings.get(permit.permit_id)
                    if bindings:
                        episodes.extend(
                            (
                                permit.reservation_id,
                                self._season_pack_view(
                                    permit,
                                    scope,
                                    files,
                                ),
                            )
                            for scope, files in sorted(bindings.items())
                        )
                    elif not permit.scope_key.endswith("PACK"):
                        episodes.append((permit.reservation_id, permit))
                return episodes
        with self._session() as connection:
            rows = connection.execute(
                "SELECT p.*, r.media_key FROM gateway_permits p "
                "JOIN reservations r ON r.id = p.reservation_id "
                "WHERE p.state = 'confirmed' AND p.category = 'sonarr' "
                "AND p.probe_parent_id IS NULL "
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
                if match is None:
                    continue
                series_key = f"season:tmdb:{match.group(1)}"
                permit = self._permit_from_row(row)
                if (permit.scope_key or "").endswith("PACK"):
                    bindings = connection.execute(
                        "SELECT e.* FROM season_pack_episodes e WHERE e.parent_permit_id=? "
                        "AND NOT EXISTS (SELECT 1 FROM episode_imports i WHERE "
                        "i.permit_id=e.parent_permit_id || ':' || e.scope_key "
                        "AND i.state='complete') ORDER BY e.scope_key",
                        (permit.permit_id,),
                    ).fetchall()
                    episodes.extend(
                        (
                            series_key,
                            self._season_pack_view(
                                permit,
                                entry["scope_key"],
                                tuple(json.loads(entry["selected_files_json"])),
                            ),
                        )
                        for entry in bindings
                    )
                else:
                    episodes.append((series_key, permit))
        return episodes

    def list_source_history(
        self,
        reservation_id: str,
        *,
        scope_key: str | None = None,
    ) -> list[Permit]:
        """Return every prior source for this exact movie or episode slot.

        Superseded rows retain metadata identity and must stay available to
        reject previously tried hashes and writes into their partial payloads.
        """
        if self._db_path is None:
            with self._lock:
                return sorted(
                    (
                        item
                        for item in self._permits.values()
                        if item.reservation_id == reservation_id and item.scope_key == scope_key
                    ),
                    key=lambda item: item.permit_id,
                )
        with self._session() as connection:
            rows = connection.execute(
                "SELECT * FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? ORDER BY permit_id",
                (reservation_id, scope_key),
            ).fetchall()
        return [self._permit_from_row(row) for row in rows]

    def had_superseded(self, reservation_id: str, *, scope_key: str | None = None) -> bool:
        """Report whether this exact movie or episode slot has failover history."""
        if self._db_path is None:
            with self._lock:
                return any(
                    item.reservation_id == reservation_id
                    and item.scope_key == scope_key
                    and item.state == "superseded"
                    for item in self._permits.values()
                )
        with self._session() as connection:
            row = connection.execute(
                "SELECT 1 FROM gateway_permits WHERE reservation_id = ? "
                "AND scope_key IS ? AND state = 'superseded' LIMIT 1",
                (reservation_id, scope_key),
            ).fetchone()
        return row is not None

    def list_uncertain(self, limit: int = 100, *, after_id: str | None = None) -> list[Permit]:
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
                        item
                        for item in self._permits.values()
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

    def _audited_collection_parent_bound(self, connection, permit: Permit, media_key: str) -> bool:
        """Permit an unscoped source only for its published, persisted audited job."""
        if (
            self._db_path is None
            or permit.category not in {"sonarr", "radarr"}
            or permit.scope_key is not None
        ):
            return False
        from homeserver_control.persistence.audited_imports import load_manifest

        try:
            database = Path(self._db_path)
            manifest = load_manifest(database.parent / "audited-imports.json", database)
            matches = [job for job in manifest["jobs"] if job["permit_id"] == permit.permit_id]
            if len(matches) != 1:
                return False
            job = matches[0]
            if (
                media_key != "collection:audited:" + job["candidate_id"]
                or job["reservation_id"] != permit.reservation_id
                or job["category"] != permit.category
                or job["metadata_sha256"] != permit.metadata_sha256
                or job["expected_payload_bytes"] != permit.budget_bytes
            ):
                return False
            persisted = connection.execute(
                "SELECT job_json FROM audited_import_jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()
            return persisted is not None and json.loads(persisted["job_json"]) == job
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return False

    def confirm_reconciled_source(self, token: str) -> Permit:
        """Confirm a source only after the gateway verifies persisted metadata and qBittorrent."""
        if self._db_path is None:
            with self._lock:
                permit = self._permits.get(token)
                if (
                    permit is None
                    or permit.state not in {"authorized", "dispatching", "unknown", "confirmed"}
                    or permit.reservation_id is None
                    or (
                        permit.state == "authorized"
                        and not self.had_superseded(
                            permit.reservation_id, scope_key=permit.scope_key
                        )
                    )
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
                permit is None
                or permit.state not in {"authorized", "dispatching", "unknown", "confirmed"}
                or permit.reservation_id is None
            ):
                raise PermissionError("reconcilable_source_required")
            if (
                permit.state == "authorized"
                and permit.probe_parent_id is None
                and not connection.execute(
                    "SELECT 1 FROM gateway_permits WHERE reservation_id = ? "
                    "AND scope_key IS ? AND state = 'superseded' LIMIT 1",
                    (permit.reservation_id, permit.scope_key),
                ).fetchone()
            ):
                raise PermissionError("replacement_permit_required")
            reservation = connection.execute(
                "SELECT media_key FROM reservations WHERE id = ? "
                "AND state IN ('reserved', 'downloading', 'waiting_episodes')",
                (permit.reservation_id,),
            ).fetchone()
            if (
                reservation is None
                or (
                    permit.scope_key is None
                    and not (
                        (
                            permit.category == "radarr"
                            and reservation["media_key"].startswith("movie:tmdb:")
                        )
                        or self._audited_collection_parent_bound(
                            connection, permit, reservation["media_key"]
                        )
                    )
                )
                or (
                    permit.scope_key is not None
                    and (
                        permit.category != "sonarr"
                        or not reservation["media_key"].startswith("season:tmdb:")
                    )
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
        self,
        token: str,
        *,
        capacity: CapacityEvidence,
        expires_at: datetime,
    ) -> Permit:
        """Renew an undispatched replacement without abandoning its exact-byte claim."""
        if not isinstance(capacity, CapacityEvidence) or expires_at <= datetime.now(UTC):
            raise ValueError("fresh replacement capacity and expiry are required")
        if self._db_path is None:
            with self._lock:
                permit = self._permits.get(token)
                if (
                    permit is None
                    or permit.state != "authorized"
                    or permit.reservation_id is None
                    or permit.budget_bytes is None
                ):
                    raise PermissionError("authorized_replacement_required")
                old_hashes = {
                    item.infohash
                    for item in self._permits.values()
                    if item.reservation_id == permit.reservation_id
                    and item.scope_key == permit.scope_key
                    and item.state == "superseded"
                }
                if not old_hashes or not old_hashes.issubset(capacity.paused_hashes):
                    raise PermissionError("source_not_stopped")
                if permit.budget_bytes > max(
                    0, capacity.usable_free_bytes - capacity.other_pending_bytes,
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
                permit is None
                or permit.state != "authorized"
                or permit.reservation_id is None
                or permit.budget_bytes is None
                or permit.budget_bytes <= 0
            ):
                raise PermissionError("authorized_replacement_required")
            history = connection.execute(
                    "SELECT * FROM gateway_permits WHERE reservation_id = ? "
                    "AND scope_key IS ? AND state = 'superseded'",
                    (permit.reservation_id, permit.scope_key),
                ).fetchall()
            if not history or not all(
                self._historical_source_stopped(connection, self._permit_from_row(row), capacity)
                for row in history
            ):
                raise PermissionError("source_not_stopped")
            if not connection.execute(
                "SELECT 1 FROM reservations WHERE id = ? "
                "AND state IN ('reserved', 'downloading', 'waiting_episodes')",
                (permit.reservation_id,),
            ).fetchone():
                raise PermissionError("reservation_required")
            pending = self._pending_bytes(connection, capacity, pool_id=permit.pool_id)
            if permit.expires_at <= datetime.now(UTC) and (
                not capacity.pools
                or permit.infohash not in capacity.pool(permit.pool_id).remaining_by_hash
            ):
                pending += min(
                    permit.budget_bytes,
                    capacity.pool(permit.pool_id).remaining_by_hash.get(
                        permit.infohash, permit.budget_bytes
                    ),
                )
            if pending > capacity.pool(permit.pool_id).usable_free_bytes:
                raise PermissionError("waiting_space")
            if self.storage_registry is not None and not self.placement_valid(permit):
                raise PermissionError("storage placement unavailable")
            connection.execute(
                "UPDATE gateway_permits SET expires_at = ? WHERE permit_id = ? "
                "AND state = 'authorized'",
                (expires_at.isoformat(), permit.permit_id),
            )
            connection.commit()
            permit.expires_at = expires_at
            return permit

    def replace_confirmed(
        self,
        old_token: str,
        *,
        infohash: str,
        metadata_sha256: str,
        selected_files: tuple[str, ...],
        budget_bytes: int,
        capacity: CapacityEvidence,
        expires_at: datetime,
        reported_seeders: int | None = None,
        torrent_content: bytes | None = None,
        quality_rank: tuple[int, ...] | None = None,
        episode_files: dict[str, tuple[str, ...]] | None = None,
        allow_quality_change: bool = False,
        episode_catalog=None, allow_absolute=False,
    ) -> Permit:
        """Atomically replace one stopped source while retaining its bytes and audit row."""
        if type(allow_quality_change) is not bool:
            raise ValueError("invalid manual quality choice")
        if not isinstance(infohash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
            raise ValueError("invalid replacement infohash")
        digest = self._validate_metadata_digest(metadata_sha256)
        if digest is None:
            raise ValueError("replacement metadata is required")
        reported_seeders = self._validate_reported_seeders(reported_seeders)
        if (
            not isinstance(selected_files, tuple)
            or not selected_files
            or any(not isinstance(name, str) or not name for name in selected_files)
            or len(set(selected_files)) != len(selected_files)
            or isinstance(budget_bytes, bool)
            or not isinstance(budget_bytes, int)
            or budget_bytes <= 0
            or not isinstance(capacity, CapacityEvidence)
            or expires_at <= datetime.now(UTC)
        ):
            raise ValueError("invalid replacement evidence")
        new_hash = infohash.lower()
        if quality_rank is not None and (
            not isinstance(quality_rank, tuple) or len(quality_rank) < 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in quality_rank)
        ):
            raise ValueError("invalid replacement quality")
        if torrent_content is not None:
            if (
                not isinstance(torrent_content, bytes)
                or not 0 < len(torrent_content) <= 16 * 1024**2
            ):
                raise ValueError("invalid replacement torrent content")
            try:
                inspected = inspect_torrent(torrent_content)
            except TorrentBytesError as error:
                raise ValueError("invalid replacement torrent content") from error
            if (
                inspected.infohash != new_hash or inspected.metadata_sha256 != digest
                or inspected.total_bytes != budget_bytes
                or not set(selected_files).issubset({file.path for file in inspected.files})
            ):
                raise ValueError("replacement torrent content differs from permit")
        if episode_files is not None and (self._db_path is None or torrent_content is None):
            raise PermissionError("persistent replacement pack metadata required")

        def replacement(old: Permit) -> Permit:
            return Permit(
                permit_id=str(uuid4()),
                token=secrets.token_urlsafe(32),
                infohash=new_hash,
                metadata_sha256=digest,
                destination=old.destination,
                category=old.category,
                reservation_id=old.reservation_id,
                scope_key=old.scope_key,
                selected_files=selected_files,
                budget_bytes=budget_bytes,
                expires_at=expires_at,
                reported_seeders=reported_seeders,
                quality_rank=quality_rank,
            )

        if self._db_path is None:
            with self._lock:
                old = self._permits.get(old_token)
                if old is None or old.state != "confirmed" or old.reservation_id is None:
                    raise PermissionError("confirmed_source_required")
                stopped_sources = {old.infohash} | {
                    item.infohash
                    for item in self._permits.values()
                    if item.reservation_id == old.reservation_id
                    and item.scope_key == old.scope_key
                    and item.state == "superseded"
                }
                if not stopped_sources.issubset(capacity.paused_hashes):
                    raise PermissionError("source_not_stopped")
                if any(
                    item.infohash == new_hash
                    and item.reservation_id == old.reservation_id
                    and item.scope_key == old.scope_key
                    for item in self._permits.values()
                ):
                    raise ValueError("same_infohash_or_prior_source")
                if budget_bytes > max(0, capacity.usable_free_bytes - capacity.other_pending_bytes):
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
            history = connection.execute(
                    "SELECT * FROM gateway_permits WHERE reservation_id = ? "
                    "AND scope_key IS ? AND state = 'superseded'",
                    (old.reservation_id, old.scope_key),
                ).fetchall()
            if old.infohash not in capacity.pool(old.pool_id).paused_hashes or not all(
                self._historical_source_stopped(connection, self._permit_from_row(row), capacity)
                for row in history
            ):
                raise PermissionError("source_not_stopped")
            reservation = connection.execute(
                "SELECT media_key FROM reservations WHERE id = ? "
                "AND state IN ('reserved', 'downloading', 'waiting_episodes')",
                (old.reservation_id,),
            ).fetchone()
            if reservation is None:
                raise PermissionError("reservation_required")
            media_key = reservation["media_key"]
            if (
                old.scope_key is None
                and (old.category != "radarr" or not media_key.startswith("movie:tmdb:"))
            ) or (
                old.scope_key is not None
                and (old.category != "sonarr" or not media_key.startswith("season:tmdb:"))
            ):
                raise PermissionError("source_identity_changed")
            self._require_probe_reservation(connection, old)
            is_pack = re.fullmatch(r"S[0-9]{2,}PACK", old.scope_key or "") is not None
            if is_pack:
                self._require_pack_replacement(
                    connection, old, episode_files, quality_rank,
                    allow_quality_change=allow_quality_change,
                )
            elif episode_files is not None:
                raise PermissionError("season pack source required")
            if connection.execute(
                "SELECT 1 FROM gateway_permits WHERE probe_parent_id=? "
                "AND state IN ('authorized','dispatching','unknown','confirmed') LIMIT 1",
                (old.permit_id,),
            ).fetchone() or connection.execute(
                "SELECT 1 FROM source_handovers WHERE state='pending' "
                "AND (parent_id=? OR candidate_id=?) LIMIT 1", (old.permit_id, old.permit_id),
            ).fetchone():
                raise PermissionError("source_transition_pending")
            if (
                connection.execute(
                    "SELECT 1 FROM movie_imports WHERE reservation_id = ? LIMIT 1",
                    (old.reservation_id,),
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM episode_imports WHERE permit_id = ? LIMIT 1",
                    (old.permit_id,),
                ).fetchone()
            ):
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
            new = replacement(old)
            self._select_pool(connection, new, capacity)
            if episode_catalog is not None and not is_pack:
                scope = re.fullmatch(r"S([0-9]{2,})E([0-9]{2,})", new.scope_key or "")
                receipt = self._normalize_episode_catalog(episode_catalog, allow_absolute)
                if scope is None or new.category != "sonarr" or not all(
                    resolve_episode_identity(
                        path, json.loads(receipt[0]), season=int(scope[1]),
                        allow_absolute=receipt[1],
                    ) == (int(scope[1]), int(scope[2])) for path in new.selected_files
                ):
                    raise ValueError("source file belongs to another episode")
            if is_pack:
                self._validate_pack_bindings(
                    new, episode_files, episode_catalog=episode_catalog,
                    allow_absolute=allow_absolute, connection=connection,
                )
            connection.execute(
                "UPDATE gateway_permits SET state = 'superseded' "
                "WHERE permit_id = ? AND state = 'confirmed'",
                (old.permit_id,),
            )
            connection.execute(
                """INSERT INTO gateway_permits(
                    permit_id, token, operation_id, reservation_id, scope_key, infohash,
                    metadata_sha256, destination, category, selected_files_json,
                    budget_bytes, reported_seeders, expires_at, state, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'authorized', NULL)""",
                (
                    new.permit_id,
                    new.token,
                    new.operation_id,
                    new.reservation_id,
                    new.scope_key,
                    new.infohash,
                    new.metadata_sha256,
                    new.destination,
                    new.category,
                    json.dumps(new.selected_files),
                    new.budget_bytes,
                    new.reported_seeders,
                    new.expires_at.isoformat(),
                ),
            )
            self._persist_placement(connection, new)
            if quality_rank is not None:
                connection.execute(
                    "UPDATE gateway_permits SET quality_rank_json=? WHERE permit_id=?",
                    (json.dumps(quality_rank), new.permit_id),
                )
            if is_pack:
                connection.executemany(
                    "INSERT INTO season_pack_episodes VALUES (?,?,?)",
                    [(new.permit_id, scope, json.dumps(files))
                     for scope, files in episode_files.items()],
                )
            if episode_catalog is not None:
                self._persist_pack_identity_receipt(
                    connection, new,
                    self._normalize_episode_catalog(episode_catalog, allow_absolute),
                )
            if torrent_content is not None:
                connection.execute("""CREATE TABLE IF NOT EXISTS torrent_artifacts (
                    permit_id TEXT PRIMARY KEY, infohash TEXT NOT NULL,
                    metadata_sha256 TEXT NOT NULL, content BLOB NOT NULL)""")
                connection.execute(
                    "INSERT INTO torrent_artifacts VALUES (?,?,?,?)",
                    (new.permit_id, new.infohash, new.metadata_sha256, torrent_content),
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

    def cancel_authorized(self, token: str) -> bool:
        """Cancel an unused admission; a dispatching/confirmed source is untouched."""
        if self._db_path is None:
            with self._lock:
                permit = self._permits.get(token)
                if permit is None or permit.state != "authorized":
                    return False
                del self._permits[token]
                self._season_pack_bindings.pop(permit.permit_id, None)
                self._pack_identity_receipts.pop(permit.permit_id, None)
                return True
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT permit_id, reservation_id FROM gateway_permits "
                "WHERE token=? AND state='authorized'",
                (token,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            connection.execute("DELETE FROM gateway_permits WHERE permit_id=?", (row["permit_id"],))
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='torrent_artifacts'"
            ).fetchone():
                connection.execute("DELETE FROM torrent_artifacts WHERE permit_id=?",
                                   (row["permit_id"],))
            connection.execute(
                "DELETE FROM season_pack_episodes WHERE parent_permit_id=?",
                (row["permit_id"],),
            )
            if row["reservation_id"] is not None:
                connection.execute(
                    """UPDATE reservations SET budget_bytes=COALESCE((
                        SELECT SUM(p.budget_bytes) FROM gateway_permits p
                        WHERE p.reservation_id=? AND p.state IN
                        ('authorized', 'dispatching', 'unknown', 'confirmed')
                    ), 0) WHERE id=?""",
                    (row["reservation_id"], row["reservation_id"]),
                )
            connection.commit()
            return True

    def retire_expired_authorized(
        self, reservation_id: str, *, scope_key: str | None = None
    ) -> int:
        """Release an unused permit after its gateway authorization has expired."""
        if self._db_path is None:
            with self._lock:
                expired = [
                    key
                    for key, item in self._permits.items()
                    if item.reservation_id == reservation_id
                    and item.scope_key == scope_key
                    and item.state == "authorized"
                    and item.expires_at <= datetime.now(UTC)
                ]
                for key in expired:
                    self._pack_identity_receipts.pop(self._permits[key].permit_id, None)
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
        preflight: Callable[[Permit], None] | None = None,
    ) -> dict[str, Any]:
        """Keep preflight read-only; only effect starts an uncertain external write."""
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
                effect_started = False
                try:
                    if preflight is not None:
                        preflight(permit)
                    effect_started = True
                    result = effect(permit)
                except Exception:
                    if permit.state == "dispatching":
                        permit.state = "unknown" if effect_started else "authorized"
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
            ordinary_replacement = connection.execute(
                "SELECT 1 FROM gateway_permits WHERE reservation_id=? "
                "AND scope_key IS ? AND state='superseded' LIMIT 1",
                (permit.reservation_id, permit.scope_key),
            ).fetchone() is not None
            if permit.probe_parent_id is not None or ordinary_replacement:
                self._require_probe_reservation(connection, permit)
            is_pack = re.fullmatch(r"S[0-9]{2,}PACK", permit.scope_key or "") is not None
            if is_pack:
                self._require_pack_reservation(connection, permit)
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

        effect_started = False
        try:
            if permit.probe_parent_id is not None or is_pack or ordinary_replacement:
                with self._session() as guard:
                    guard.execute("BEGIN IMMEDIATE")
                    current = guard.execute(
                        "SELECT * FROM gateway_permits WHERE token=? AND state='dispatching'",
                        (permit.token,),
                    ).fetchone()
                    if current is None or replace(
                        self._permit_from_row(current), state=permit.state,
                    ) != permit:
                        raise PermissionError("permit_state_changed")
                    if is_pack:
                        self._require_pack_reservation(guard, permit)
                    else:
                        self._require_probe_reservation(guard, permit)
                    if preflight is not None:
                        preflight(permit)
                    effect_started = True
                    result = effect(permit)
                    guard.commit()
            else:
                if preflight is not None:
                    preflight(permit)
                effect_started = True
                result = effect(permit)
        except Exception:
            with self._session() as update:
                update.execute(
                    "UPDATE gateway_permits SET state = ? "
                    "WHERE token = ? AND state = 'dispatching'",
                    ("unknown" if effect_started else "authorized", token),
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

    def _require_pack_reservation(self, connection, permit: Permit) -> None:
        """Guard the whole physical pack, including episodes outside its bindings."""
        self._require_probe_reservation(connection, permit)
        reservation = connection.execute(
            "SELECT media_key FROM reservations WHERE id=?", (permit.reservation_id,)
        ).fetchone()
        season = re.fullmatch(r"season:tmdb:([1-9][0-9]*):([1-9][0-9]*)",
                              reservation["media_key"])
        if (
            season is None or permit.category != "sonarr"
            or permit.scope_key != f"S{int(season[2]):02d}PACK"
        ):
            raise PermissionError("pack_identity_changed")
        bindings = {
            row["scope_key"]: tuple(json.loads(row["selected_files_json"]))
            for row in connection.execute(
                "SELECT * FROM season_pack_episodes WHERE parent_permit_id=?",
                (permit.permit_id,),
            )
        }
        if not bindings:
            raise PermissionError("pack_bindings_required")
        self._validate_pack_bindings(permit, bindings, connection=connection)
        prefix = f"episode:tmdb:{season[1]}:S{int(season[2]):02d}E"
        if connection.execute(
            "SELECT 1 FROM tombstones WHERE media_key GLOB ? LIMIT 1", (prefix + "*",)
        ).fetchone():
            raise PermissionError("media_tombstoned")

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
