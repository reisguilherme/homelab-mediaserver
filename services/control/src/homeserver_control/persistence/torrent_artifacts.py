"""Persist verified torrent metadata for gateway dispatch."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent
from homeserver_control.gateway.permits import Permit

_MAX_METADATA = 16 * 1024 * 1024


class TorrentArtifactStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        self._identity_inspections = {}
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS torrent_artifacts (
                    permit_id TEXT PRIMARY KEY,
                    infohash TEXT NOT NULL,
                    metadata_sha256 TEXT NOT NULL,
                    content BLOB NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _valid(permit: Permit, content: bytes) -> bool:
        if not isinstance(content, bytes) or not 0 < len(content) <= _MAX_METADATA:
            return False
        try:
            inspected = inspect_torrent(content)
        except TorrentBytesError:
            return False
        return (
            inspected.infohash == permit.infohash
            and inspected.metadata_sha256 == permit.metadata_sha256
            and permit.budget_bytes is not None
            and inspected.total_bytes <= permit.budget_bytes
            and bool(permit.selected_files)
            and set(permit.selected_files).issubset({item.path for item in inspected.files})
        )

    def put(self, permit: Permit, content: bytes) -> None:
        if not self._valid(permit, content):
            raise ValueError("torrent metadata does not match admitted permit")
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO torrent_artifacts
                (permit_id, infohash, metadata_sha256, content) VALUES (?, ?, ?, ?)""",
                (permit.permit_id, permit.infohash, permit.metadata_sha256, content),
            )
            row = connection.execute(
                "SELECT infohash, metadata_sha256, content FROM torrent_artifacts "
                "WHERE permit_id = ?",
                (permit.permit_id,),
            ).fetchone()
            if row != (permit.infohash, permit.metadata_sha256, content):
                raise ValueError("torrent metadata conflicts with an existing permit")

    def get(self, permit: Permit) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT infohash, metadata_sha256, content FROM torrent_artifacts "
                "WHERE permit_id = ?",
                (permit.permit_id,),
            ).fetchone()
        if row is None or row[0] != permit.infohash or row[1] != permit.metadata_sha256:
            return None
        return row[2] if self._valid(permit, row[2]) else None

    def verified_for_infohash(self, infohash: str) -> bytes | None:
        """Resolve identity only; authority remains in fresh gateway permit guards."""
        if not isinstance(infohash, str) or not re.fullmatch(r"[0-9a-f]{40}", infohash):
            raise ValueError("invalid canonical torrent identity")
        try:
            with closing(
                sqlite3.connect(
                    Path(self.database).resolve().as_uri() + "?mode=ro",
                    uri=True,
                    timeout=5,
                )
            ) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN")
                rows = connection.execute(
                    "SELECT a.content,a.metadata_sha256,a.infohash,p.infohash AS permit_infohash,"
                    "p.metadata_sha256 AS permit_digest,p.budget_bytes,p.selected_files_json "
                    "FROM torrent_artifacts a LEFT JOIN gateway_permits p "
                    "ON p.permit_id=a.permit_id WHERE a.infohash=?",
                    (infohash,),
                ).fetchall()
            if not rows:
                return None
            unique = set()
            for row in rows:
                content = row["content"]
                if not isinstance(content, bytes) or not 0 < len(content) <= _MAX_METADATA:
                    raise ValueError("sealed descriptor is invalid")
                digest = sha256(content).hexdigest()
                if (
                    digest != row["metadata_sha256"]
                    or digest != row["permit_digest"]
                    or row["permit_infohash"] != infohash
                ):
                    raise ValueError("sealed descriptor binding changed")
                inspected = self._identity_inspections.get(digest)
                if inspected is None:
                    inspected = inspect_torrent(content)
                    if len(self._identity_inspections) >= 1024:
                        self._identity_inspections.clear()
                    self._identity_inspections[digest] = inspected
                selected = json.loads(row["selected_files_json"])
                if (
                    inspected.infohash != infohash
                    or type(row["budget_bytes"]) is not int
                    or inspected.total_bytes > row["budget_bytes"]
                    or not isinstance(selected, list)
                    or not selected
                    or any(not isinstance(value, str) for value in selected)
                    or not set(selected).issubset({file.path for file in inspected.files})
                ):
                    raise ValueError("sealed descriptor files or budget changed")
                unique.add(content)
            if len(unique) != 1:
                raise ValueError("canonical torrent has ambiguous sealed descriptors")
            return unique.pop()
        except (sqlite3.Error, OSError, TypeError, TorrentBytesError) as error:
            raise ValueError("sealed descriptor identity unavailable") from error
