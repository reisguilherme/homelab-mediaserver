"""Keep verified external sidecars available across worker restarts."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from homeserver_control.domain.subtitle_content import valid_srt


class SubtitleArtifactStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def put(
        self, reservation_id: str, scope_key: str | None, infohash: str, content: bytes,
        *, language: str = "BR_PT",
    ) -> None:
        if language not in {"BR_PT", "EN"}:
            raise ValueError("unsupported subtitle language")
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", infohash):
            raise ValueError("invalid subtitle torrent hash")
        if not valid_srt(content):
            raise ValueError("invalid SRT")
        digest = hashlib.sha256(content).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """SELECT language, sha256, content FROM subtitle_artifacts
                WHERE reservation_id = ? AND scope_key = ? AND infohash = ?""",
                (reservation_id, scope_key or "", infohash.lower()),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO subtitle_artifacts
                    (reservation_id, scope_key, infohash, source, language, sha256, content)
                    VALUES (?, ?, ?, 'subdl', ?, ?, ?)""",
                    (reservation_id, scope_key or "", infohash.lower(), language, digest, content),
                )
            elif row[0] == "EN" and language == "BR_PT":
                connection.execute(
                    """UPDATE subtitle_artifacts SET language = ?, sha256 = ?, content = ?
                    WHERE reservation_id = ? AND scope_key = ? AND infohash = ?""",
                    (language, digest, content, reservation_id, scope_key or "", infohash.lower()),
                )
            elif row[0] == "BR_PT" and language == "EN":
                return
            elif row[0] != language or row[1] != digest or row[2] != content:
                raise ValueError("subtitle artifact conflicts with an existing permit")

    def get(
        self, reservation_id: str, scope_key: str | None, infohash: str,
        *, language: str = "BR_PT",
    ) -> bytes | None:
        if language not in {"BR_PT", "EN"}:
            raise ValueError("unsupported subtitle language")
        with self._connect() as connection:
            row = connection.execute(
                """SELECT sha256, content FROM subtitle_artifacts
                WHERE reservation_id = ? AND scope_key = ? AND infohash = ?
                AND source = 'subdl' AND language = ?""",
                (reservation_id, scope_key or "", infohash.lower(), language),
            ).fetchone()
        if row is None:
            return None
        content = row[1]
        if hashlib.sha256(content).hexdigest() != row[0] or not valid_srt(content):
            return None
        return content
