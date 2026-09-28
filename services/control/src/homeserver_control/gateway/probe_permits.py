"""Additional durable admissions for one measured candidate of the same acquisition."""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import UTC, datetime
from uuid import uuid4


class ProbePermits:
    @staticmethod
    def _require_probe_reservation(connection, permit):
        row = connection.execute(
            "SELECT r.media_key,q.state FROM reservations r "
            "JOIN requests q ON q.id=r.request_id WHERE r.id=? "
            "AND r.state IN ('reserved','downloading','waiting_episodes')",
            (permit.reservation_id,),
        ).fetchone()
        if row is None or row["state"] in ("cancel_requested", "cancelled"):
            raise PermissionError("reservation_required")
        keys = [row["media_key"]]
        if permit.scope_key is not None:
            match = re.fullmatch(r"S(\d+)E(\d+)", permit.scope_key)
            media = re.fullmatch(r"season:tmdb:(\d+):(\d+)", row["media_key"])
            if match and media:
                keys.append(f"episode:tmdb:{media[1]}:{permit.scope_key}")
        if any(
            connection.execute("SELECT 1 FROM tombstones WHERE media_key=?", (key,)).fetchone()
            for key in keys
        ):
            raise PermissionError("media_tombstoned")

    def probe_reservation_active(self, permit):
        with self._session() as connection:
            try:
                self._require_probe_reservation(connection, permit)
                return True
            except PermissionError:
                return False

    def probe_cancelled(self, permit):
        with self._session() as connection:
            row = connection.execute(
                "SELECT q.state,r.media_key FROM reservations r JOIN requests q "
                "ON q.id=r.request_id WHERE r.id=?",
                (permit.reservation_id,),
            ).fetchone()
            if row is None:
                return False
            if row["state"] in ("cancel_requested", "cancelled"):
                return True
            keys = [row["media_key"]]
            media = re.fullmatch(r"season:tmdb:(\d+):(\d+)", row["media_key"])
            if media and permit.scope_key:
                keys.append(f"episode:tmdb:{media[1]}:{permit.scope_key}")
            return any(
                connection.execute("SELECT 1 FROM tombstones WHERE media_key=?", (key,)).fetchone()
                for key in keys
            )

    def active_source(self, permit) -> bool:
        primary = self.get_for_reservation(permit.reservation_id, scope_key=permit.scope_key)
        return primary is not None and (
            primary.permit_id == permit.permit_id or primary.permit_id == permit.probe_parent_id
        )

    def pending_handover(self, token: str):
        if self._db_path is None:
            return None
        with self._session() as connection:
            row = connection.execute(
                "SELECT p.* FROM source_handovers h JOIN gateway_permits p "
                "ON p.permit_id=h.parent_id JOIN gateway_permits n "
                "ON n.permit_id=h.candidate_id WHERE n.token=? AND h.state='pending'",
                (token,),
            ).fetchone()
        return self._permit_from_row(row) if row is not None else None

    def complete_handover(self, token: str) -> None:
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE source_handovers SET state='complete' WHERE candidate_id "
                "IN (SELECT permit_id FROM gateway_permits WHERE token=?)",
                (token,),
            )
            row = connection.execute(
                "SELECT reservation_id FROM gateway_permits WHERE token=?", (token,)
            ).fetchone()
            if row is not None:
                self._update_probe_budget(connection, row["reservation_id"])
            connection.commit()

    def probe_verified_faster(self, candidate_id: str, max_age_seconds: float = 120) -> bool:
        with self._session() as connection:
            # Only worker observations authenticated through the gateway can provide this proof.
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='source_probes'"
            ).fetchone():
                return False
            row = connection.execute(
                "SELECT last_observed_at FROM source_probes WHERE candidate_id=? "
                "AND decision='promote'",
                (candidate_id,),
            ).fetchone()
            return (
                row is not None
                and row["last_observed_at"] is not None
                and (0 <= time.time() - row["last_observed_at"] <= max_age_seconds)
            )

    def get_probe(self, parent_id: str):
        if self._db_path is None:
            return None
        with self._session() as connection:
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE probe_parent_id = ? AND state IN "
                "('authorized', 'dispatching', 'unknown', 'confirmed')",
                (parent_id,),
            ).fetchone()
        return self._permit_from_row(row) if row is not None else None

    def set_quality(self, token: str, rank: tuple[int, ...]) -> None:
        if len(rank) < 2 or any(isinstance(v, bool) or not isinstance(v, int) for v in rank):
            raise ValueError("invalid source quality")
        if self._db_path is None:
            self._permits[token].quality_rank = rank
            return
        with self._session() as connection:
            connection.execute(
                "UPDATE gateway_permits SET quality_rank_json = ? WHERE token = ?",
                (json.dumps(rank), token),
            )

    def issue_probe(
        self,
        old_token: str,
        *,
        infohash: str,
        metadata_sha256: str,
        selected_files: tuple[str, ...],
        budget_bytes: int,
        capacity,
        expires_at: datetime,
        reported_seeders: int | None = None,
        quality_rank: tuple[int, ...] | None = None,
    ):
        from .permits import Permit

        if self._db_path is None:
            raise PermissionError("persistent_probe_required")
        digest = self._validate_metadata_digest(metadata_sha256)
        if (
            not re.fullmatch(r"[0-9a-fA-F]{40}", infohash)
            or digest is None
            or not selected_files
            or len(set(selected_files)) != len(selected_files)
            or any(not isinstance(path, str) or not path for path in selected_files)
            or isinstance(budget_bytes, bool)
            or not isinstance(budget_bytes, int)
            or budget_bytes <= 0
            or expires_at <= datetime.now(UTC)
        ):
            raise ValueError("invalid probe evidence")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            old_row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token = ?", (old_token,)
            ).fetchone()
            old = self._permit_from_row(old_row) if old_row else None
            if (
                old is None
                or old.state != "confirmed"
                or old.probe_parent_id is not None
                or old.reservation_id is None
            ):
                raise PermissionError("confirmed_source_required")
            self._require_probe_reservation(connection, old)
            if old.quality_rank is None:
                raise PermissionError("source_quality_unknown")
            if connection.execute(
                "SELECT 1 FROM source_handovers WHERE candidate_id=? AND state='pending'",
                (old.permit_id,),
            ).fetchone():
                raise PermissionError("handover_pending")
            if not connection.execute(
                "SELECT 1 FROM reservations WHERE id = ? AND state IN "
                "('reserved', 'downloading', 'waiting_episodes')",
                (old.reservation_id,),
            ).fetchone():
                raise PermissionError("reservation_required")
            if connection.execute(
                "SELECT 1 FROM gateway_permits WHERE probe_parent_id = ? "
                "AND state IN ('authorized','dispatching','unknown','confirmed')",
                (old.permit_id,),
            ).fetchone():
                raise PermissionError("probe_already_active")
            if (
                connection.execute(
                    "SELECT 1 FROM movie_imports WHERE reservation_id = ?", (old.reservation_id,)
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM episode_imports WHERE permit_id = ?", (old.permit_id,)
                ).fetchone()
            ):
                raise PermissionError("import_started")
            if connection.execute(
                "SELECT 1 FROM gateway_permits WHERE infohash = ? AND "
                "(reservation_id = ? OR state IN "
                "('authorized','dispatching','unknown','confirmed'))",
                (infohash.lower(), old.reservation_id),
            ).fetchone():
                raise PermissionError("source_already_tried")
            if set(selected_files) & set(old.selected_files):
                raise PermissionError("source_paths_overlap")
            if old.quality_rank is not None and (
                quality_rank is None
                or any(
                    new < current
                    for new, current in zip(quality_rank[:2], old.quality_rank[:2], strict=True)
                )
            ):
                raise PermissionError("quality_downgrade")
            if budget_bytes + self._pending_bytes(connection, capacity) > capacity.free_bytes:
                raise PermissionError("waiting_space")
            probe = Permit(
                permit_id=str(uuid4()),
                token=secrets.token_urlsafe(32),
                infohash=infohash.lower(),
                destination=old.destination,
                category=old.category,
                reservation_id=old.reservation_id,
                scope_key=old.scope_key,
                metadata_sha256=digest,
                selected_files=selected_files,
                budget_bytes=budget_bytes,
                reported_seeders=self._validate_reported_seeders(reported_seeders),
                expires_at=expires_at,
                probe_parent_id=old.permit_id,
                quality_rank=quality_rank,
            )
            connection.execute(
                """INSERT INTO gateway_permits(
                permit_id,token,operation_id,reservation_id,scope_key,infohash,
                metadata_sha256,destination,category,selected_files_json,budget_bytes,
                reported_seeders,expires_at,state,probe_parent_id,quality_rank_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'authorized',?,?)""",
                (
                    probe.permit_id,
                    probe.token,
                    probe.operation_id,
                    probe.reservation_id,
                    probe.scope_key,
                    probe.infohash,
                    digest,
                    probe.destination,
                    probe.category,
                    json.dumps(selected_files),
                    budget_bytes,
                    reported_seeders,
                    expires_at.isoformat(),
                    old.permit_id,
                    json.dumps(quality_rank) if quality_rank is not None else None,
                ),
            )
            self._update_probe_budget(connection, old.reservation_id)
            connection.commit()
        return probe

    @staticmethod
    def _update_probe_budget(connection, reservation_id):
        connection.execute(
            """UPDATE reservations SET budget_bytes = COALESCE((
            SELECT SUM(budget_bytes) FROM gateway_permits WHERE reservation_id = ?
            AND (state IN ('authorized','dispatching','unknown','confirmed') OR permit_id IN (
                SELECT parent_id FROM source_handovers WHERE state='pending'))),0) WHERE id = ?""",
            (reservation_id, reservation_id),
        )

    def promote_probe(self, token: str):
        if self._db_path is None:
            raise PermissionError("persistent_probe_required")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token = ?", (token,)
            ).fetchone()
            if row is None or row["state"] != "confirmed" or row["probe_parent_id"] is None:
                raise PermissionError("confirmed_probe_required")
            probe = self._permit_from_row(row)
            self._require_probe_reservation(connection, probe)
            if not connection.execute(
                "SELECT 1 FROM reservations WHERE id = ? AND state IN "
                "('reserved','downloading','waiting_episodes')",
                (probe.reservation_id,),
            ).fetchone():
                raise PermissionError("reservation_required")
            if (
                connection.execute(
                    "SELECT 1 FROM movie_imports WHERE reservation_id = ?", (probe.reservation_id,)
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM episode_imports WHERE permit_id = ?", (probe.probe_parent_id,)
                ).fetchone()
            ):
                raise PermissionError("import_started")
            changed = connection.execute(
                "UPDATE gateway_permits SET state='superseded' "
                "WHERE permit_id=? AND state='confirmed' AND "
                "probe_parent_id IS NULL",
                (probe.probe_parent_id,),
            )
            if changed.rowcount != 1:
                raise PermissionError("primary_source_changed")
            connection.execute(
                "INSERT INTO source_handovers(candidate_id,parent_id) VALUES (?,?)",
                (probe.permit_id, probe.probe_parent_id),
            )
            connection.execute(
                "UPDATE gateway_permits SET probe_parent_id=NULL WHERE token=?", (token,)
            )
            self._update_probe_budget(connection, probe.reservation_id)
            connection.commit()
            probe.probe_parent_id = None
        return probe

    def reject_probe(self, token: str) -> None:
        if self._db_path is None:
            raise PermissionError("persistent_probe_required")
        with self._session() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM gateway_permits WHERE token=?", (token,)
            ).fetchone()
            if row is None or row["probe_parent_id"] is None:
                raise PermissionError("probe_required")
            connection.execute(
                "UPDATE gateway_permits SET state='probe_rejected' WHERE token=?", (token,)
            )
            self._update_probe_budget(connection, row["reservation_id"])
            connection.commit()
