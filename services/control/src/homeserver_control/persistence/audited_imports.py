"""Validate a sealed import plan and persist intent before native import requests."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from homeserver_control.domain.subtitle_content import valid_srt

from .audited_derivations import SCHEMA as DERIVATION_SCHEMA
from .audited_derivations import decode as decode_derivation

MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_HEX = re.compile(r"[0-9a-fA-F]{64}")
_SCOPE = re.compile(r"S\d{2,}E\d{2,}")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,95}")


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError("invalid audited import manifest")


def _integer(value, minimum=0) -> bool:
    return type(value) is int and minimum <= value <= 2**63 - 1


def _text(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(ord(character) < 32 for character in value)
    )


def _path(value) -> bool:
    return (
        _text(value)
        and "\\" not in value
        and ":" not in value
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )


def _json(data) -> str:
    try:
        return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("invalid audited import JSON") from None


def validate_manifest(data: dict) -> dict:
    """Keep annotations intact while validating identities and the payload partition."""
    encoded = _json(data)
    _require(len(encoded.encode("utf-8")) <= MAX_MANIFEST_BYTES)
    data = json.loads(encoded)
    try:
        _require(isinstance(data, dict))
        _require(type(data["schema_version"]) is int and data["schema_version"] == 1)
        _require(data["mode"] == "audited_manifest")
        _require(_text(data["manifest_id"]))
        _require(isinstance(data["manifest_sha256"], str))
        _require(_HEX.fullmatch(data["manifest_sha256"]) is not None)
        _require(isinstance(data["jobs"], list) and bool(data["jobs"]))
        job_ids, permits, reservations = set(), set(), set()
        episode_keys = set()
        movie_ids = set()
        for job in data["jobs"]:
            _require(isinstance(job, dict))
            for key, seen in (
                ("job_id", job_ids),
                ("permit_id", permits),
                ("reservation_id", reservations),
            ):
                value = job[key]
                _require(_text(value) and value not in seen)
                seen.add(value)
            for key in ("candidate_id", "metadata_sha256"):
                _require(isinstance(job[key], str) and _HEX.fullmatch(job[key]) is not None)
            _require(job["category"] in ("sonarr", "radarr"))
            _require(_text(job["entry_id"]) or _integer(job["entry_id"], 1))
            _require(_integer(job["tmdb_id"], 1) and _text(job["release_title"]))
            _require(_integer(job["expected_payload_bytes"], 1))
            _require(_integer(job["padding_bytes"]))
            _require(isinstance(job["payload_files"], list) and bool(job["payload_files"]))
            _require(isinstance(job["files"], list) and bool(job["files"]))
            _require(isinstance(job["excluded_files"], list))
            payload = {}
            for row in job["payload_files"]:
                _require(isinstance(row, dict) and _path(row["path"]))
                _require(row["path"] not in payload and _integer(row["size_bytes"]))
                payload[row["path"]] = row["size_bytes"]
            _require(sum(payload.values()) + job["padding_bytes"] == job["expected_payload_bytes"])
            partition = set()
            for row in job["files"] + job["excluded_files"]:
                _require(isinstance(row, dict) and _path(row["path"]))
                parts = row.get("source_parts", [row])
                if "source_parts" in row:
                    _require(row in job["files"] and row.get("kind") == "episode")
                    _require(isinstance(parts, list) and len(parts) == 2)
                    _require(parts[0]["path"] == row["path"]
                             and parts[0]["size_bytes"] == row["size_bytes"])
                    _require(all(isinstance(part, dict) and _path(part["path"])
                                 and Path(part["path"]).suffix.lower() == ".mp4"
                                 and _integer(part["size_bytes"], 1) for part in parts))
                    recipe = row["derivation"]
                    bound = 2 * sum(part["size_bytes"] for part in parts)
                    _require(isinstance(recipe, dict)
                             and recipe.get("recipe") == "concat_mp4_streamcopy_v1"
                             and _integer(recipe.get("max_output_bytes"), 1)
                             and recipe["max_output_bytes"] == bound)
                    _require(all(len(row[key]) == 1 for key in
                                 ("native_episode_ids", "canonical_episode_ids", "scopes")))
                else:
                    _require("derivation" not in row)
                for part in parts:
                    path = part["path"]
                    _require(path not in partition and path in payload)
                    _require(_integer(part["size_bytes"]) and part["size_bytes"] == payload[path])
                    partition.add(path)
            _require(partition == payload.keys())
            for row in job["excluded_files"]:
                _require(_text(row["reason"]))
            for target in job["files"]:
                _require(_integer(target["size_bytes"], 1))
                if "subtitle_release_title" in target:
                    _require(_text(target["subtitle_release_title"]))
                if target["kind"] == "movie":
                    _require(job["category"] == "radarr")
                    movie = target["native_movie_id"]
                    _require(_integer(movie, 1) and movie not in movie_ids)
                    movie_ids.add(movie)
                else:
                    _require(target["kind"] == "episode" and job["category"] == "sonarr")
                    series = target["native_series_id"]
                    _require(_integer(series, 1))
                    arrays = [
                        target[key]
                        for key in (
                            "native_episode_ids",
                            "canonical_episode_ids",
                            "scopes",
                        )
                    ]
                    _require(all(isinstance(values, list) and values for values in arrays))
                    _require(len({len(values) for values in arrays}) == 1)
                    for label, values in zip(("native", "canonical", "scope"), arrays, strict=True):
                        _require(len(set(values)) == len(values))
                        for value in values:
                            _require(
                                isinstance(value, str) and _SCOPE.fullmatch(value) is not None
                                if label == "scope"
                                else _integer(value, 1)
                            )
                            identity = (series, label, value)
                            _require(identity not in episode_keys)
                            episode_keys.add(identity)
    except (KeyError, TypeError, AttributeError, RecursionError):
        raise ValueError("invalid audited import manifest") from None
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate audited import JSON key")
        result[key] = value
    return result


def _open_manifest(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if os.name != "posix":
        return os.open(path, flags)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:-1]:
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name, flags, dir_fd=directory)
    finally:
        os.close(directory)


def load_manifest(path: Path, database: Path) -> dict:
    """Read only the fixed private regular file beside the control database."""
    path, database = Path(path), Path(database)
    if not path.is_absolute() or not database.is_absolute():
        raise ValueError("audited import paths must be absolute")
    if path != database.parent / "audited-imports.json":
        raise ValueError("audited import manifest path is fixed")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("audited import manifest symlink forbidden")
    try:
        descriptor = _open_manifest(path)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("audited import manifest must be a regular file")
            if os.name == "posix" and info.st_mode & 0o077:
                raise ValueError("audited import manifest must be private")
            if info.st_size > MAX_MANIFEST_BYTES:
                raise ValueError("audited import manifest too large")
            content = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(content) > MAX_MANIFEST_BYTES:
            raise ValueError("audited import manifest too large")
        parsed = json.loads(content, object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("audited import manifest unavailable or invalid") from None
    return validate_manifest(parsed)


class AuditedImportStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            connection.execute(DERIVATION_SCHEMA)
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS audited_import_manifests (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    manifest_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
                    jobs_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audited_import_manifest_history (
                    manifest_id TEXT PRIMARY KEY, manifest_sha256 TEXT NOT NULL,
                    jobs_sha256 TEXT NOT NULL, manifest_json TEXT NOT NULL
                );
                INSERT OR IGNORE INTO audited_import_manifest_history
                    SELECT manifest_id, manifest_sha256, jobs_sha256, manifest_json
                    FROM audited_import_manifests;
                CREATE TABLE IF NOT EXISTS audited_import_jobs (
                    job_id TEXT PRIMARY KEY, ordinal INTEGER NOT NULL,
                    manifest_id TEXT REFERENCES audited_import_manifest_history(manifest_id),
                    state TEXT NOT NULL DEFAULT 'planned', job_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audited_import_files (
                    file_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES audited_import_jobs(job_id),
                    ordinal INTEGER NOT NULL, file_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'planned', command_id INTEGER,
                    imported_path TEXT, probe_json TEXT, subtitle_language TEXT,
                    subtitle_content BLOB, reason TEXT,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    last_attempt REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS audited_import_supersessions (
                    old_job_id TEXT PRIMARY KEY REFERENCES audited_import_jobs(job_id),
                    new_job_ids_json TEXT NOT NULL,
                    parent_manifest_id TEXT NOT NULL,
                    replacement_manifest_id TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    replaced_at REAL NOT NULL
                );
            """)
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(audited_import_jobs)")
            }
            if "manifest_id" not in columns:
                connection.execute("ALTER TABLE audited_import_jobs ADD COLUMN manifest_id TEXT")
            if connection.execute(
                "SELECT 1 FROM audited_import_jobs WHERE manifest_id IS NULL LIMIT 1"
            ).fetchone() is not None:
                for history in connection.execute(
                    "SELECT manifest_id, manifest_json FROM audited_import_manifest_history "
                    "ORDER BY rowid"
                ).fetchall():
                    for job in json.loads(history["manifest_json"])["jobs"]:
                        connection.execute(
                            "UPDATE audited_import_jobs SET manifest_id=? "
                            "WHERE job_id=? AND manifest_id IS NULL",
                            (history["manifest_id"], job["job_id"]),
                        )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def derivation(self, file_id: str):
        with self._connect() as connection:
            return decode_derivation(connection.execute(
                "SELECT * FROM audited_derivations WHERE file_id=?", (file_id,),
            ).fetchone())

    def begin_derivation(self, file_id: str) -> None:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE audited_derivations SET state='writing' "
                "WHERE file_id=? AND state='reserved'", (file_id,),
            )
            if result.rowcount != 1:
                raise ValueError("derivation write intent already exists")
            connection.commit()

    def record_derivation_output(self, file_id: str, receipt: dict) -> None:
        encoded = _json(receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM audited_derivations WHERE file_id=?",
                                     (file_id,)).fetchone()
            if row is None or row["state"] not in ("writing", "verified"):
                raise ValueError("derivation write intent is missing")
            output = receipt["output"]
            _require(_integer(output["size_bytes"], 1)
                     and output["size_bytes"] <= row["budget_bytes"])
            _require(isinstance(output["sha256"], str)
                     and _HEX.fullmatch(output["sha256"]) is not None)
            _require(_integer(output["device"]) and _integer(output["inode"], 1))
            if row["receipt_json"] is not None and row["receipt_json"] != encoded:
                raise ValueError("derivation output receipt changed")
            connection.execute("UPDATE audited_derivations SET state='verified',receipt_json=? "
                               "WHERE file_id=?", (encoded, file_id))
            connection.commit()

    def publish_derivation(self, file_id: str, receipt: dict) -> None:
        # Caller proves publication, physical inode/hash and current same-pool free
        # evidence first. Reserved bytes remain counted through every earlier crash.
        encoded = _json(receipt)
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE audited_derivations SET state='published' "
                "WHERE file_id=? AND state IN ('verified','published') AND receipt_json=?",
                (file_id, encoded),
            )
            if result.rowcount != 1:
                raise ValueError("derivation publication receipt is not exact")
            connection.commit()

    def install_manifest(self, manifest: dict) -> None:
        manifest = validate_manifest(manifest)
        encoded = _json(manifest)
        digest = hashlib.sha256(_json(manifest["jobs"]).encode("utf-8")).hexdigest()
        binding = (manifest["manifest_id"], manifest["manifest_sha256"], digest, encoded)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT manifest_id, manifest_sha256, jobs_sha256, manifest_json "
                "FROM audited_import_manifests WHERE singleton = 1"
            ).fetchone()
            history = connection.execute(
                "SELECT manifest_id, manifest_sha256, jobs_sha256, manifest_json "
                "FROM audited_import_manifest_history WHERE manifest_id=?",
                (binding[0],),
            ).fetchone()
            if history is not None and tuple(history) != binding:
                raise ValueError("audited import manifest identity is already sealed")
            if existing is not None and tuple(existing) == binding:
                return
            old_jobs = {
                row["job_id"]: row["job_json"]
                for row in connection.execute(
                    "SELECT j.job_id,j.job_json FROM audited_import_jobs j WHERE NOT EXISTS "
                    "(SELECT 1 FROM audited_import_supersessions s WHERE s.old_job_id=j.job_id)"
                )
            }
            incoming = {job["job_id"]: _json(job) for job in manifest["jobs"]}
            if any(connection.execute(
                "SELECT 1 FROM audited_import_supersessions WHERE old_job_id=?", (job_id,),
            ).fetchone() for job_id in incoming):
                raise ValueError("superseded audited job cannot be reactivated")
            if existing is not None and (
                history is not None
                or len(incoming) <= len(old_jobs)
                or any(incoming.get(job_id) != encoded for job_id, encoded in old_jobs.items())
            ):
                raise ValueError("audited import increment must preserve every existing job")
            connection.execute(
                "INSERT INTO audited_import_manifest_history VALUES (?, ?, ?, ?)",
                binding,
            )
            connection.execute(
                "INSERT INTO audited_import_manifests VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET manifest_id=excluded.manifest_id, "
                "manifest_sha256=excluded.manifest_sha256, jobs_sha256=excluded.jobs_sha256, "
                "manifest_json=excluded.manifest_json",
                binding,
            )
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM audited_import_files"
            ).fetchone()[0]
            job_index = connection.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 FROM audited_import_jobs"
            ).fetchone()[0]
            for job in manifest["jobs"]:
                if job["job_id"] in old_jobs:
                    continue
                connection.execute(
                    "INSERT INTO audited_import_jobs (job_id, ordinal, manifest_id, job_json) "
                    "VALUES (?, ?, ?, ?)",
                    (job["job_id"], job_index, manifest["manifest_id"], _json(job)),
                )
                job_index += 1
                for target in job["files"]:
                    file_id = hashlib.sha256(
                        (job["job_id"] + "\x00" + target["path"]).encode("utf-8")
                    ).hexdigest()
                    connection.execute(
                        "INSERT INTO audited_import_files (file_id, job_id, ordinal, file_json) "
                        "VALUES (?, ?, ?, ?)",
                        (file_id, job["job_id"], ordinal, _json(target)),
                    )
                    ordinal += 1

    @staticmethod
    def _replacement_targets(job: dict) -> set[tuple]:
        targets = set()
        for target in job["files"]:
            if target["kind"] == "movie":
                targets.add(("movie", target["native_movie_id"]))
            else:
                targets.update(
                    ("episode", target["native_series_id"], native, canonical, scope)
                    for native, canonical, scope in zip(
                        target["native_episode_ids"], target["canonical_episode_ids"],
                        target["scopes"], strict=True,
                    )
                )
        return targets

    @staticmethod
    def _replacement_binding(connection, job: dict, *, new: bool) -> dict:
        """Prove the persisted whole-payload admission, without issuing permits."""
        from homeserver_control.domain.torrent_bytes import inspect_torrent

        row = connection.execute(
            "SELECT p.*,r.media_key,r.state AS reservation_state,q.state AS request_state,"
            "q.media_key AS request_media_key FROM gateway_permits p "
            "JOIN reservations r ON r.id=p.reservation_id JOIN requests q ON q.id=r.request_id "
            "WHERE p.permit_id=?", (job["permit_id"],),
        ).fetchone()
        if row is None:
            raise ValueError("audited replacement permit is missing")
        permit = dict(row)
        if (
            permit["state"] not in ({"authorized", "confirmed"} if new else {"confirmed"})
            or permit["reservation_id"] != job["reservation_id"]
            or permit["scope_key"] is not None or permit["probe_parent_id"] is not None
            or permit["category"] != job["category"]
            or permit["metadata_sha256"] != job["metadata_sha256"]
            or permit["budget_bytes"] != job["expected_payload_bytes"]
            or permit["reservation_state"] not in {"reserved", "downloading", "waiting_episodes"}
            or permit["request_state"] in {"cancel_requested", "cancelled", "superseded"}
            or permit["request_media_key"] != permit["media_key"]
            or permit["pool_id"] not in {"ssd", "hdd", "hdd2", "hdd3"}
            or not _text(permit["filesystem_id"])
            or permit["destination"] != "/data/torrents/.placements/" + permit["permit_id"]
        ):
            raise ValueError("audited replacement admission changed")
        expected_key = "collection:audited:" + job["candidate_id"]
        allowed_keys = {expected_key}
        if not new and job["category"] == "radarr":
            allowed_keys.add(f"movie:tmdb:{job['tmdb_id']}")
        if permit["media_key"] not in allowed_keys:
            raise ValueError("audited replacement reservation identity changed")
        if permit["state"] == "authorized":
            expiry = datetime.fromisoformat(permit["expires_at"])
            if expiry.tzinfo is None or expiry <= datetime.now(UTC):
                raise ValueError("audited replacement permit has expired")
        if connection.execute(
            "SELECT COUNT(*) FROM gateway_permits WHERE infohash=? AND state IN "
            "('authorized','dispatching','unknown','confirmed')", (permit["infohash"],),
        ).fetchone()[0] != 1:
            raise ValueError("audited replacement source ownership is ambiguous")
        artifact = connection.execute(
            "SELECT infohash,metadata_sha256,content FROM torrent_artifacts WHERE permit_id=?",
            (permit["permit_id"],),
        ).fetchone()
        if artifact is None:
            raise ValueError("audited replacement descriptor is missing")
        inspected = inspect_torrent(artifact["content"])
        if (
            (artifact["infohash"], artifact["metadata_sha256"])
            != (permit["infohash"], permit["metadata_sha256"])
            or inspected.infohash != permit["infohash"]
            or inspected.metadata_sha256 != permit["metadata_sha256"]
            or inspected.total_bytes != job["expected_payload_bytes"]
            or inspected.padding_bytes != job["padding_bytes"]
            or [{"path": file.path, "size_bytes": file.length} for file in inspected.files]
            != job["payload_files"]
            or json.loads(permit["selected_files_json"]) != [file.path for file in inspected.files]
            or new and job["candidate_id"] != hashlib.sha256(
                ("torrent:" + inspected.infohash).encode()).hexdigest()
        ):
            raise ValueError("audited replacement descriptor binding changed")
        keys = {permit["media_key"], f"movie:tmdb:{job['tmdb_id']}"}
        if job["category"] == "sonarr":
            keys = {permit["media_key"], f"series:tmdb:{job['tmdb_id']}"}
            for target in job["files"]:
                for scope in target["scopes"]:
                    season = int(_SCOPE.fullmatch(scope)[0].split("E")[0][1:])
                    keys.update({f"season:tmdb:{job['tmdb_id']}:{season}",
                                 f"episode:tmdb:{job['tmdb_id']}:{scope}"})
        if any(connection.execute("SELECT 1 FROM tombstones WHERE media_key=?", (key,)).fetchone()
               for key in keys):
            raise ValueError("audited replacement media was deleted")
        if connection.execute(
            "SELECT 1 FROM movie_imports WHERE reservation_id=?", (permit["reservation_id"],),
        ).fetchone() or connection.execute(
            "SELECT 1 FROM episode_imports WHERE permit_id=? OR permit_id LIKE ?",
            (permit["permit_id"], permit["permit_id"] + ":S%"),
        ).fetchone():
            raise ValueError("audited replacement import has started")
        if connection.execute(
            "SELECT 1 FROM source_handovers WHERE state='pending' AND "
            "(parent_id=? OR candidate_id=?)", (permit["permit_id"], permit["permit_id"]),
        ).fetchone() or connection.execute(
            "SELECT 1 FROM gateway_permits WHERE probe_parent_id=? AND state IN "
            "('authorized','dispatching','unknown','confirmed')", (permit["permit_id"],),
        ).fetchone():
            raise ValueError("audited replacement source transition is pending")
        return {"job_id": job["job_id"], **{key: permit[key] for key in (
            "permit_id", "reservation_id", "infohash", "metadata_sha256", "pool_id",
            "filesystem_id", "destination", "category", "budget_bytes",
        )}}

    @staticmethod
    def _stopped_replacement_receipt(binding: dict, receipt: dict) -> dict:
        if not isinstance(receipt, dict):
            raise ValueError("audited replacement native evidence is missing")
        receipt = json.loads(_json(receipt))
        now = time.time()
        if (
            any(receipt.get(key) != value or type(receipt.get(key)) is not type(value)
                for key, value in binding.items())
            or receipt.get("state") not in {"stoppedDL", "pausedDL"}
            or receipt.get("payload_retained") is not True
            or not isinstance(receipt.get("native_id"), str)
            or re.fullmatch(r"[0-9a-f]{40}", receipt["native_id"]) is None
            or type(receipt.get("progress")) not in {int, float}
            or not math.isfinite(receipt["progress"]) or not 0 <= receipt["progress"] < 1
            or type(receipt.get("amount_left")) is not int
            or not 0 < receipt["amount_left"] <= binding["budget_bytes"]
            or type(receipt.get("total_size")) is not int
            or receipt["total_size"] != binding["budget_bytes"]
            or type(receipt.get("checked_at")) not in {int, float}
            or not math.isfinite(receipt["checked_at"])
            or not 0 <= now - receipt["checked_at"] <= 15
        ):
            raise ValueError("audited replacement native evidence is stale or changed")
        return receipt

    def install_replacement_manifest(
        self, manifest: dict, *, expected_parent_manifest_id: str, replacements: dict,
        stopped_source_validator,
    ) -> list[dict]:
        """Retire exact unimported sources; never delete payloads or issue admissions.

        The operator must quiesce source/import controllers and provide a fresh,
        read-only native check. New permits/descriptors already passed ordinary
        admission. Historical job/file JSON remains immutable; only this explicit
        transaction may remove retired jobs from the active manifest.
        """
        manifest = validate_manifest(manifest)
        if (
            not _text(expected_parent_manifest_id) or not isinstance(replacements, dict)
            or not replacements or not callable(stopped_source_validator)
            or any(not _text(old) or not isinstance(new, list) or not new
                   or any(not _text(job) for job in new) or len(set(new)) != len(new)
                   for old, new in replacements.items())
        ):
            raise ValueError("exact audited replacement scope is required")
        targets = [job for group in replacements.values() for job in group]
        if len(set(targets)) != len(targets) or set(targets) & replacements.keys():
            raise ValueError("audited replacement groups overlap")
        encoded = _json(manifest)
        digest = hashlib.sha256(_json(manifest["jobs"]).encode()).hexdigest()
        if manifest["manifest_sha256"] != digest:
            raise ValueError("audited replacement manifest seal changed")
        binding = (manifest["manifest_id"], manifest["manifest_sha256"], digest, encoded)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT manifest_id,manifest_sha256,jobs_sha256,manifest_json "
                "FROM audited_import_manifests WHERE singleton=1",
            ).fetchone()
            if existing is not None and tuple(existing) == binding:
                rows = connection.execute(
                    "SELECT * FROM audited_import_supersessions WHERE replacement_manifest_id=?",
                    (manifest["manifest_id"],),
                ).fetchall()
                if {row["old_job_id"]: json.loads(row["new_job_ids_json"]) for row in rows} != (
                    replacements
                ) or any(row["parent_manifest_id"] != expected_parent_manifest_id for row in rows):
                    raise ValueError("audited replacement retry scope changed")
                return [json.loads(row["receipt_json"]) for row in rows]
            if existing is None or existing["manifest_id"] != expected_parent_manifest_id:
                raise ValueError("audited replacement parent manifest changed")
            if connection.execute(
                "SELECT 1 FROM audited_import_manifest_history WHERE manifest_id=?",
                (manifest["manifest_id"],),
            ).fetchone():
                raise ValueError("audited replacement manifest identity was already used")
            current = validate_manifest(json.loads(existing["manifest_json"]))
            old_jobs = {job["job_id"]: job for job in current["jobs"]}
            incoming = {job["job_id"]: job for job in manifest["jobs"]}
            if (
                not replacements.keys() <= old_jobs.keys()
                or set(incoming) != (set(old_jobs) - replacements.keys()) | set(targets)
                or any(incoming.get(job_id) != job for job_id, job in old_jobs.items()
                       if job_id not in replacements)
            ):
                raise ValueError("audited replacement changed unrelated jobs")
            stored = {row["job_id"]: row for row in connection.execute(
                "SELECT * FROM audited_import_jobs",
            )}
            retired = {row[0] for row in connection.execute(
                "SELECT old_job_id FROM audited_import_supersessions",
            )}
            if (
                set(stored) - retired != set(old_jobs)
                or any(stored[job_id]["job_json"] != _json(job)
                       for job_id, job in old_jobs.items())
                or set(targets) & stored.keys()
            ):
                raise ValueError("audited replacement historical job binding changed")
            all_old_reservations = {job["reservation_id"] for job in old_jobs.values()}
            new_bindings, old_bindings = {}, {}
            for old_id, new_ids in replacements.items():
                old = old_jobs[old_id]
                if stored[old_id]["state"] != "planned" or connection.execute(
                    "SELECT 1 FROM audited_import_supersessions WHERE old_job_id=?", (old_id,),
                ).fetchone():
                    raise ValueError("audited replacement job is no longer pending")
                files = connection.execute(
                    "SELECT * FROM audited_import_files WHERE job_id=?", (old_id,),
                ).fetchall()
                expected_files = {
                    hashlib.sha256(
                        (old_id + "\x00" + file["path"]).encode(),
                    ).hexdigest(): _json(file)
                    for file in old["files"]
                }
                if {row["file_id"]: row["file_json"] for row in files} != expected_files or any(
                    row["state"] != "planned" or any(row[key] is not None for key in (
                        "command_id", "imported_path", "probe_json", "subtitle_language",
                        "subtitle_content")) for row in files
                ):
                    raise ValueError("audited replacement file already has effects")
                if connection.execute(
                    "SELECT 1 FROM audited_derivations WHERE permit_id=? AND "
                    "(state!='reserved' OR receipt_json IS NOT NULL)", (old["permit_id"],),
                ).fetchone():
                    raise ValueError("audited replacement derivation has effects")
                old_bindings[old_id] = self._replacement_binding(connection, old, new=False)
                coverage = set()
                for new_id in new_ids:
                    new = incoming[new_id]
                    if any(new.get(key) != old.get(key) for key in (
                        "entry_id", "category", "tmdb_id", "tvdb_id", "subtitle_policy",
                        "canonical_identity_basis",
                    )) or new["reservation_id"] in all_old_reservations:
                        raise ValueError("audited replacement canonical identity changed")
                    coverage.update(self._replacement_targets(new))
                    new_bindings[new_id] = self._replacement_binding(connection, new, new=True)
                    if new_bindings[new_id]["infohash"] == old_bindings[old_id]["infohash"]:
                        raise ValueError("audited replacement must select a different source")
                if coverage != self._replacement_targets(old):
                    raise ValueError("audited replacement canonical coverage changed")
            # No controller can acquire DB import intent between these native
            # observations and the retirement/publication transaction.
            receipts = {
                old_id: self._stopped_replacement_receipt(
                    old, stopped_source_validator(dict(old)),
                ) for old_id, old in old_bindings.items()
            }
            for old_id, receipt in receipts.items():
                self._stopped_replacement_receipt(old_bindings[old_id], receipt)
            connection.execute(
                "INSERT INTO audited_import_manifest_history VALUES (?,?,?,?)", binding,
            )
            connection.execute(
                "UPDATE audited_import_manifests SET manifest_id=?,manifest_sha256=?,"
                "jobs_sha256=?,manifest_json=? WHERE singleton=1", binding,
            )
            ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 FROM audited_import_jobs",
            ).fetchone()[0]
            file_ordinal = connection.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 FROM audited_import_files",
            ).fetchone()[0]
            for new_id in targets:
                job = incoming[new_id]
                connection.execute(
                    "INSERT INTO audited_import_jobs(job_id,ordinal,manifest_id,job_json) "
                    "VALUES (?,?,?,?)", (new_id, ordinal, manifest["manifest_id"], _json(job)),
                )
                ordinal += 1
                for target in job["files"]:
                    file_id = hashlib.sha256(
                        (new_id + "\x00" + target["path"]).encode(),
                    ).hexdigest()
                    connection.execute(
                        "INSERT INTO audited_import_files(file_id,job_id,ordinal,file_json) "
                        "VALUES (?,?,?,?)", (file_id, new_id, file_ordinal, _json(target)),
                    )
                    file_ordinal += 1
            for old_id, old in old_bindings.items():
                connection.execute(
                    "UPDATE gateway_permits SET state='superseded' WHERE permit_id=?",
                    (old["permit_id"],),
                )
                connection.execute("UPDATE reservations SET state='superseded' WHERE id=?",
                                   (old["reservation_id"],))
                connection.execute(
                    "INSERT INTO audited_import_supersessions VALUES (?,?,?,?,?,?)",
                    (old_id, _json(replacements[old_id]), expected_parent_manifest_id,
                     manifest["manifest_id"], _json(receipts[old_id]), time.time()),
                )
            return list(receipts.values())

    def pending_files(self, limit=4, now=None) -> list[dict]:
        if not _integer(limit, 1):
            raise ValueError("invalid audited import limit")
        now = time.time() if now is None else now
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("invalid audited import time")
        with self._connect() as connection:
            # Read both work classes from one snapshot. Each query stays bounded.
            connection.execute("BEGIN")
            eligible = (
                "SELECT f.*, j.job_json, j.manifest_id FROM audited_import_files f "
                "JOIN audited_import_jobs j USING (job_id) "
                "WHERE f.state != 'complete' AND f.next_attempt <= ? AND NOT EXISTS "
                "(SELECT 1 FROM audited_import_supersessions s WHERE s.old_job_id=j.job_id) "
            )
            import_states = "('import_unknown','import_requested','planned')"
            subtitles = connection.execute(
                eligible + "AND f.state NOT IN " + import_states
                + " ORDER BY f.next_attempt, f.last_attempt, f.ordinal LIMIT ?",
                (now, limit),
            ).fetchall()
            # Reserve one subtitle slot even when failed imports remain due on
            # every cycle. Recovery still precedes new work in all import slots.
            import_limit = limit - 1 if subtitles and limit >= 2 else limit
            rows = connection.execute(
                eligible + "AND f.state IN " + import_states
                + " ORDER BY CASE WHEN f.state = 'planned' THEN 1 ELSE 0 END, "
                "f.next_attempt, f.last_attempt, f.ordinal LIMIT ?",
                (now, import_limit),
            ).fetchall()
            rows.extend(subtitles[:limit - len(rows)])
        return [
            {
                "file_id": row["file_id"],
                "job_id": row["job_id"],
                "manifest_id": row["manifest_id"],
                "state": row["state"],
                "command_id": row["command_id"],
                "imported_path": row["imported_path"],
                "probe": json.loads(row["probe_json"]) if row["probe_json"] is not None else None,
                "subtitle_language": row["subtitle_language"],
                "subtitle_content": row["subtitle_content"],
                "reason": row["reason"],
                "job": json.loads(row["job_json"]),
                "file": json.loads(row["file_json"]),
            }
            for row in rows
        ]

    def claim_import(self, file_id) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "UPDATE audited_import_files SET state = 'import_unknown', last_attempt = ? "
                    "WHERE file_id = ? AND state = 'planned' AND NOT EXISTS "
                    "(SELECT 1 FROM audited_import_supersessions s "
                    "WHERE s.old_job_id=audited_import_files.job_id)",
                    (time.time(), file_id),
                ).rowcount
                == 1
            )

    def record_requested(self, file_id, command_id) -> None:
        if not _integer(command_id, 1):
            raise ValueError("invalid audited import command")
        with self._connect() as connection:
            updated = connection.execute(
                "UPDATE audited_import_files SET state = 'import_requested', command_id = ?, "
                "last_attempt = ? WHERE file_id = ? AND state = 'import_unknown'",
                (command_id, time.time(), file_id),
            ).rowcount
            if updated != 1:
                raise ValueError("audited import request has no unconfirmed intent")

    def _update(self, file_id, columns: dict, *, states=None) -> None:
        columns["last_attempt"] = time.time()
        assignments = ", ".join(key + " = ?" for key in columns)
        clause = " AND state IN (" + ",".join("?" for _ in states) + ")" if states else ""
        parameters = (*columns.values(), file_id, *(states or ()))
        with self._connect() as connection:
            if (
                connection.execute(
                    f"UPDATE audited_import_files SET {assignments} WHERE file_id = ? "
                    f"AND state != 'complete'{clause}",
                    parameters,
                ).rowcount
                != 1
            ):
                raise ValueError("audited import file unavailable for transition")

    def mark_imported(self, file_id, imported_path, probe=None) -> None:
        if not _text(imported_path) or not Path(imported_path).is_absolute():
            raise ValueError("invalid audited imported path")
        if probe is not None and not isinstance(probe, dict):
            raise ValueError("invalid audited import probe")
        self._update(
            file_id,
            {
                "state": "imported",
                "imported_path": imported_path,
                "probe_json": _json(probe) if probe is not None else None,
                "next_attempt": 0,
                "reason": None,
            },
        )

    @staticmethod
    def _retry(reason, retry_after):
        if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
            raise ValueError("invalid audited import reason code")
        if (
            type(retry_after) not in (int, float)
            or not math.isfinite(retry_after)
            or retry_after < 0
        ):
            raise ValueError("invalid audited import retry delay")
        return {"reason": reason, "next_attempt": time.time() + retry_after}

    def subtitle_pending(self, file_id, reason="subtitles_unavailable", retry_after=300) -> None:
        columns = self._retry(reason, retry_after) | {"state": "subtitle_pending"}
        self._update(file_id, columns, states=("imported", "subtitle_pending"))

    def defer(self, file_id, reason, retry_after=30) -> None:
        self._update(file_id, self._retry(reason, retry_after))

    def cache_subtitle(self, file_id, language, content) -> None:
        if (
            language not in ("BR_PT", "EN")
            or not isinstance(content, bytes)
            or not valid_srt(content)
        ):
            raise ValueError("invalid audited import subtitle")
        self._update(file_id, {"subtitle_language": language, "subtitle_content": content})

    def complete(self, file_id) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id, state FROM audited_import_files WHERE file_id = ?",
                (file_id,),
            ).fetchone()
            if row is None or row["state"] not in ("imported", "subtitle_pending", "complete"):
                raise ValueError("audited import file has not been imported")
            connection.execute(
                "UPDATE audited_import_files SET state = 'complete', reason = NULL, "
                "last_attempt = ? WHERE file_id = ?",
                (time.time(), file_id),
            )
            finished = connection.execute(
                "UPDATE audited_import_jobs SET state = 'complete' WHERE job_id = ? "
                "AND NOT EXISTS (SELECT 1 FROM audited_import_files "
                "WHERE job_id = ? AND state != 'complete')",
                (row["job_id"], row["job_id"]),
            ).rowcount
            if finished:
                self._finish_parent(connection, row["job_id"])

    @staticmethod
    def _finish_parent(connection, job_id):
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('reservations', 'requests')"
            )
        }
        if tables != {"reservations", "requests"}:
            return
        job = json.loads(
            connection.execute(
                "SELECT job_json FROM audited_import_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        )
        updated = connection.execute(
            "UPDATE reservations SET state='imported' WHERE id=? "
            "AND state IN ('reserved', 'downloading', 'waiting_episodes')",
            (job["reservation_id"],),
        ).rowcount
        if updated:
            connection.execute(
                "UPDATE requests SET state='available', updated_at=? "
                "WHERE id=(SELECT request_id FROM reservations WHERE id=?) "
                "AND state NOT IN ('cancelled', 'cancel_requested')",
                (datetime.now(UTC).isoformat().replace("+00:00", "Z"), job["reservation_id"]),
            )
