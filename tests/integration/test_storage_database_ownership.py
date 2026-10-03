from __future__ import annotations

import gc
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from homeserver_common import host
from homeserver_common.env import load_settings


def test_existing_database_and_permissions_are_preserved(tmp_path):
    path = tmp_path / 'control.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE retained(value)')
        connection.execute("INSERT INTO retained VALUES ('existing')")
    path.chmod(0o600)
    original = path.read_bytes()
    host.prepare_controller_database(path, uid=os.geteuid(), gid=os.getegid())
    assert path.read_bytes() == original
    assert path.stat().st_mode & 0o777 == 0o600


def test_database_symlink_is_refused_without_touching_target(tmp_path):
    target = tmp_path / 'target'
    target.write_text('preserve')
    (tmp_path / 'control.sqlite').symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        host.prepare_controller_database(tmp_path / 'control.sqlite',
                                         uid=os.geteuid(), gid=os.getegid())
    assert target.read_text() == 'preserve'


@pytest.mark.skipif(os.geteuid() != 0, reason='requires Linux root and real UID 1000 fixture')
@pytest.mark.parametrize('journal_mode', ['WAL', 'DELETE'])
def test_fresh_expansion_init_root_worker_and_uid1000_writer_share_sqlite(
    journal_mode, monkeypatch,
):
    with tempfile.TemporaryDirectory(prefix='homeserver-db-ownership-', dir='/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        env = root / '.env'
        env.write_text(f'HOMESERVER_APPDATA_ROOT={root}/appdata\n'
                       f'HOMESERVER_MEDIA_ROOT={root}/media\n'
                       f'HOMESERVER_TRANSCODE_ROOT={root}/transcode\n'
                       f'HOMESERVER_RUN_ROOT={root}/run\n')
        monkeypatch.setattr(host, 'verify_storage_startup', lambda *args: True)
        host.prepare_runtime(load_settings(env, mode='dev'))
        database = root / 'appdata/control/control.sqlite'
        assert database.exists(), 'init must create database before root worker'
        assert (database.stat().st_uid, database.stat().st_gid) == (1000, 1000)
        from homeserver_control.persistence.db import ReservationRepository

        ReservationRepository(database).initialize()  # Actual root worker schema/connection.
        # Repository context managers commit but Python owns final connection
        # disposal; release those references before testing a different mode.
        gc.collect()
        connection = sqlite3.connect(database, isolation_level=None)
        try:
            mode = connection.execute(f'PRAGMA journal_mode={journal_mode}').fetchone()[0]
            assert mode.upper() == journal_mode
            connection.execute('CREATE TABLE ownership_fixture(value TEXT)')
            connection.execute('BEGIN IMMEDIATE')
            connection.execute("INSERT INTO ownership_fixture VALUES ('root')")
            suffixes = ('-wal', '-shm') if journal_mode == 'WAL' else ('-journal',)
            for suffix in suffixes:
                info = Path(str(database) + suffix).stat()
                assert (info.st_uid, info.st_gid) == (1000, 1000)
                assert info.st_mode & 0o600 == 0o600
            child_code = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=5, isolation_level=None)
print('connected', flush=True)
connection.execute('BEGIN IMMEDIATE')
connection.execute("INSERT INTO ownership_fixture VALUES ('uid1000')")
connection.commit()
connection.close()
"""
            child = subprocess.Popen([sys.executable, '-c', child_code, str(database)],
                                     user=1000, group=1000, extra_groups=(),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == 'connected'
            connection.commit()  # UID1000 writer waits on root transaction, then mutates.
            stdout, stderr = child.communicate(timeout=10)
            assert child.returncode == 0, (stdout, stderr)
            rows = connection.execute(
                'SELECT value FROM ownership_fixture ORDER BY value').fetchall()
            assert rows == [('root',), ('uid1000',)]
        finally:
            connection.close()


@pytest.mark.skipif(os.geteuid() != 0, reason='requires Linux root')
def test_existing_wrong_owner_is_refused_without_repair(tmp_path):
    database = tmp_path / 'control.sqlite'
    database.write_bytes(b'preserve')
    with pytest.raises(ValueError, match='owner'):
        host.prepare_controller_database(database, uid=1000, gid=1000)
    assert database.read_bytes() == b'preserve'
    assert database.stat().st_uid == 0
