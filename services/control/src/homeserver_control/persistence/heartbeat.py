"""Small durable runtime diagnostic; never stores credentials or exception messages."""

import math
import re
import sqlite3
from pathlib import Path


def _valid_deadline(value):
    return type(value) in {int, float} and math.isfinite(value) and value > 0


class WorkerHeartbeatStore:
    def __init__(self, db_path):
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            # Healthchecks and the worker may initialize an old database together.
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("""CREATE TABLE IF NOT EXISTS worker_heartbeat (
                id INTEGER PRIMARY KEY CHECK(id=1), last_seen_at REAL NOT NULL,
                last_success_at REAL, initialized INTEGER NOT NULL,
                state TEXT NOT NULL, last_error TEXT, cycle_deadline_at REAL)""")
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(worker_heartbeat)")
            }
            if "cycle_deadline_at" not in columns:
                connection.execute("ALTER TABLE worker_heartbeat ADD COLUMN cycle_deadline_at REAL")

    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def write(
        self, *, now, initialized, state, success=False, error=None,
        cycle_deadline_at: float | None = None,
    ):
        if (
            not math.isfinite(now)
            or now < 0
            or state not in {"starting", "running", "failed", "maintenance"}
        ):
            raise ValueError("invalid heartbeat")
        if cycle_deadline_at is not None and not _valid_deadline(cycle_deadline_at):
            raise ValueError("invalid cycle deadline")
        if error and not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*Error", error):
            error = "OperationalError"
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO worker_heartbeat
                (id,last_seen_at,last_success_at,initialized,state,last_error,cycle_deadline_at)
                VALUES(1,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET last_seen_at=excluded.last_seen_at,
                last_success_at=COALESCE(excluded.last_success_at,worker_heartbeat.last_success_at),
                initialized=excluded.initialized,state=excluded.state,last_error=excluded.last_error,
                cycle_deadline_at=excluded.cycle_deadline_at""",
                (now, now if success else None, int(initialized), state, error, cycle_deadline_at),
            )

    def snapshot(self):
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM worker_heartbeat WHERE id=1").fetchone()
        return dict(row) if row is not None else {}

    def ready(self, *, now, max_age):
        row = self.snapshot()
        if not (
            row
            and row["initialized"]
            and row["state"] == "running"
            and row["last_error"] is None
            and 0 <= now - row["last_seen_at"] <= max_age
        ):
            return False
        deadline = row["cycle_deadline_at"]
        if deadline is not None:
            # A pulse cannot extend this fixed deadline or conceal its expiry.
            return _valid_deadline(deadline) and now < deadline
        return bool(
            row["last_success_at"] is not None
            and 0 <= now - row["last_success_at"] <= max_age
        )
