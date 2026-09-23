"""Persist verified torrent metadata for gateway dispatch."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent
from homeserver_control.gateway.permits import Permit

_MAX_METADATA = 16 * 1024 * 1024


class TorrentArtifactStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
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
            and set(permit.selected_files).issubset(
                {item.path for item in inspected.files}
            )
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
                "WHERE permit_id = ?", (permit.permit_id,),
            ).fetchone()
            if row != (permit.infohash, permit.metadata_sha256, content):
                raise ValueError("torrent metadata conflicts with an existing permit")

    def get(self, permit: Permit) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT infohash, metadata_sha256, content FROM torrent_artifacts "
                "WHERE permit_id = ?", (permit.permit_id,),
            ).fetchone()
        if row is None or row[0] != permit.infohash or row[1] != permit.metadata_sha256:
            return None
        return row[2] if self._valid(permit, row[2]) else None
