"""Persistent, bounded login counters, independent of the controller database.

SQLite IMMEDIATE transactions serialize admission across workers; expiring leases
bound simultaneous native logins. Keys are salted hashes, never credentials.
"""

from __future__ import annotations

import hashlib
import math
import secrets
import sqlite3
import unicodedata
from pathlib import Path


class LoginLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__('Login temporarily limited')


class LoginLimits:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (salt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS buckets (
                    key TEXT PRIMARY KEY, kind TEXT, window REAL, failures INTEGER,
                    strikes INTEGER, blocked REAL, seen REAL,
                    attempt_window REAL, attempts INTEGER
                );
                CREATE TABLE IF NOT EXISTS leases (
                    id TEXT, key TEXT, expires REAL, PRIMARY KEY(id, key)
                );
                CREATE INDEX IF NOT EXISTS lease_key ON leases(key);
            ''')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT salt FROM metadata LIMIT 1').fetchone()
            if row is None:
                db.execute('INSERT INTO metadata VALUES (?)', (secrets.token_hex(32),))
        path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=2)

    def reserve(self, service: str, ip: str, account: str, now: float) -> str:
        normalized = unicodedata.normalize('NFKC', account).strip().casefold()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            salt = db.execute('SELECT salt FROM metadata LIMIT 1').fetchone()[0]
            identities = [('ip', ip), ('account', service + ':' + normalized), ('global', 'all')]
            db.execute('DELETE FROM leases WHERE expires <= ?', (now,))
            db.execute('DELETE FROM buckets WHERE seen < ? AND blocked <= ?',
                       (now - 7 * 86400, now))
            if db.execute('SELECT COUNT(*) FROM buckets').fetchone()[0] > 10000:
                raise LoginLimited(60)
            lease = secrets.token_hex(16)
            for kind, value in identities:
                key = hashlib.sha256(f'{salt}:{kind}:{value}'.encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO buckets VALUES (?, ?, ?, 0, 0, 0, ?, ?, 0)',
                           (key, kind, now, now, now))
                row = db.execute('SELECT blocked, attempt_window, attempts FROM buckets '
                                 'WHERE key = ?', (key,)).fetchone()
                blocked, window, attempts = row
                if blocked > now:
                    raise LoginLimited(math.ceil(blocked - now))
                if now - window >= 60:
                    window, attempts = now, 0
                if attempts >= {'ip': 20, 'account': 10, 'global': 60}[kind]:
                    raise LoginLimited(math.ceil(window + 60 - now))
                pending = db.execute('SELECT COUNT(*) FROM leases WHERE key = ?',
                                     (key,)).fetchone()[0]
                if pending >= {'ip': 2, 'account': 1, 'global': 8}[kind]:
                    raise LoginLimited(2)
                db.execute('UPDATE buckets SET attempt_window=?, attempts=? WHERE key=?',
                           (window, attempts + 1, key))
                db.execute('INSERT INTO leases VALUES (?, ?, ?)', (lease, key, now + 30))
            return lease

    def finish(self, lease: str, failed: bool, now: float) -> None:
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT b.key, b.kind, b.window, b.failures, b.strikes, b.seen '
                              'FROM buckets b JOIN leases l ON b.key=l.key WHERE l.id=?',
                              (lease,)).fetchall()
            for key, kind, window, failures, strikes, seen in rows:
                if not failed:
                    continue
                if now - window >= 600:
                    window, failures = now, 0
                if now - seen >= 86400:
                    strikes = 0
                failures += 1
                threshold = 100 if kind == 'global' else 5
                blocked = 0.0
                if failures >= threshold:
                    delays = (30, 120, 900) if kind == 'account' else (900, 3600, 86400)
                    blocked = now + delays[min(strikes, 2)]
                    strikes += 1
                    failures, window = 0, now
                db.execute('UPDATE buckets SET window=?, failures=?, strikes=?, '
                           'blocked=MAX(blocked,?), seen=? WHERE key=?',
                           (window, failures, strikes, blocked, now, key))
            db.execute('DELETE FROM leases WHERE id=?', (lease,))
