import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / 'scripts/security-migration-checkpoint.py'


@pytest.fixture
def checkpoint():
    spec = importlib.util.spec_from_file_location('checkpoint', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mount_validation_rejects_unexpected_source(checkpoint):
    container = {'Mounts': [{'Type': 'bind', 'Source': '/srv/data', 'Destination': '/config'}]}
    with pytest.raises(RuntimeError, match='mount'):
        checkpoint.require_mount(container, '/config', Path('/srv/appdata/jellyfin'))


def test_maintenance_requires_fresh_idle_heartbeat(checkpoint, tmp_path):
    db = tmp_path / 'control.sqlite'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE worker_heartbeat (state TEXT, last_seen_at REAL, '
                           'cycle_deadline_at REAL, initialized INTEGER, id INTEGER)')
        connection.execute("INSERT INTO worker_heartbeat VALUES ('maintenance',100,NULL,1,1)")
    assert checkpoint.worker_idle(db, 101)
    assert not checkpoint.worker_idle(db, 200)
    with sqlite3.connect(db) as connection:
        connection.execute('UPDATE worker_heartbeat SET cycle_deadline_at=120')
    assert not checkpoint.worker_idle(db, 101)


def test_database_verification_reports_counts_without_rows(checkpoint, tmp_path):
    db = tmp_path / 'db.sqlite3'
    with sqlite3.connect(db) as connection:
        connection.execute('CREATE TABLE user (id INTEGER, secret TEXT)')
        connection.execute("INSERT INTO user VALUES (42,'never-output-secret')")
    result = checkpoint.verify_sqlite_tree(tmp_path)
    assert result == {'db.sqlite3': {'integrity': 'ok', 'tables': {'user': 1}}}
    assert 'never-output-secret' not in str(result)
