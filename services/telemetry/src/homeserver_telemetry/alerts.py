from __future__ import annotations

import json
import re
import sqlite3
from typing import Any


def _sanitize(value: Any, key: str | None = None) -> Any:
    if key and key.lower() in {"token", "password", "secret", "authorization", "api_key"}:
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, str):
        return re.sub(
            r"([?&])(?:token|key|secret|password)=[^&\s]+", r"\1<redacted>", value, flags=re.I
        )
    return value


class NotificationOutbox:
    def __init__(self, path: str) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_outbox (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_type TEXT NOT NULL,
              object_id TEXT NOT NULL,
              generation TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0,
              available_at REAL NOT NULL DEFAULT 0,
              UNIQUE(event_type, object_id, generation)
            )
            """
        )
        self.connection.commit()

    def enqueue(
        self, *, event_type: str, object_id: str, generation: str, payload: dict[str, Any]
    ) -> int:
        safe = json.dumps(_sanitize(payload), sort_keys=True, separators=(",", ":"))
        self.connection.execute(
            """
            INSERT OR IGNORE INTO notification_outbox(
              event_type, object_id, generation, payload_json
            )
            VALUES (?, ?, ?, ?)
            """,
            (event_type, object_id, generation, safe),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT id FROM notification_outbox "
            "WHERE event_type = ? AND object_id = ? AND generation = ?",
            (event_type, object_id, generation),
        ).fetchone()
        assert row is not None
        return int(row["id"])

    def pending(self, *, now: float = 0) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM notification_outbox
            WHERE state IN ('pending', 'failed') AND available_at <= ?
            ORDER BY id
            """,
            (now,),
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_failed(self, item_id: int, *, retry_at: float) -> None:
        self.connection.execute(
            "UPDATE notification_outbox "
            "SET state = 'failed', attempts = attempts + 1, available_at = ? WHERE id = ?",
            (retry_at, item_id),
        )
        self.connection.commit()

    def mark_sent(self, item_id: int) -> None:
        self.connection.execute(
            "UPDATE notification_outbox SET state = 'sent' WHERE id = ?", (item_id,)
        )
        self.connection.commit()
