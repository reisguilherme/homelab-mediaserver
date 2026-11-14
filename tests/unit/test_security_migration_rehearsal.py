import importlib.util
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).parents[2] / 'scripts/security-migration-rehearsal.py'
SUCCESS_LOG = (
    '[INF] Main: Migrations have been applied, optimizing the database... '
    'This might take a while\n'
    '[INF] Main: Preparing the database for shutdown...\n'
)


@pytest.fixture
def rehearsal():
    spec = importlib.util.spec_from_file_location('rehearsal', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_rehearsal_refuses_real_configuration_mount(rehearsal):
    with pytest.raises(RuntimeError, match='fixture'):
        rehearsal.container_command('jellyfin', Path('/srv/appdata/jellyfin'), 'fixture')


def test_rehearsal_has_no_network_media_ports_or_extra_mounts(rehearsal):
    path = rehearsal.ROOT / '20261019T021511Z' / 'jellyfin'
    command = rehearsal.container_command('jellyfin', path, 'fixture')
    assert command[command.index('--network') + 1] == 'none'
    assert command.count('--mount') == 1
    assert command[command.index('--mount') + 1] == f'type=bind,src={path},dst=/config'
    assert command[-2:] == ['--mode', 'MigrateSystem']
    assert not {'--publish', '-p', '--privileged', '--device', '--env-file'} & set(command)
    assert '--read-only' in command and '--init' in command


@pytest.mark.parametrize('service,cache_size', [('jellyfin', '3g'), ('seerr', '256m')])
def test_rehearsal_cache_has_native_headroom_without_raising_container_memory(
    rehearsal, service, cache_size,
):
    path = rehearsal.ROOT / '20261019T021511Z' / service
    command = rehearsal.container_command(service, path, 'fixture')
    tmpfs = [command[index + 1] for index, value in enumerate(command) if value == '--tmpfs']
    assert tmpfs == [
        '/tmp:rw,nosuid,nodev,uid=1000,gid=1000,size=256m',
        '/cache:rw,nosuid,nodev,uid=1000,gid=1000,size=' + cache_size,
    ]
    assert command[command.index('--memory') + 1] == '4g'
    assert command[command.index('--network') + 1] == 'none'
    assert command.count('--mount') == 1


@pytest.mark.parametrize('returncode', [0, 1])
def test_seerr_readiness_disables_remote_update_lookup(rehearsal, monkeypatch, returncode):
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(rehearsal.subprocess, 'run', run)
    assert rehearsal.seerr_ready('fixture-seerr') is (returncode == 0)
    command, options = seen[0]
    assert command[:5] == ['docker', 'exec', 'fixture-seerr', 'node', '-e']
    url = urlsplit(command[5].split('fetch("', 1)[1].split('"', 1)[0])
    assert (url.scheme, url.netloc, url.path) == (
        'http', '127.0.0.1:5055', '/api/v1/status')
    assert parse_qs(url.query) == {'checkUpdateAvailable': ['false']}
    assert 'AbortSignal.timeout(4000)' in command[5]
    assert options == {'capture_output': True, 'timeout': 10}


def test_rehearsal_refuses_symlink_mount_to_real_configuration(rehearsal, tmp_path, monkeypatch):
    root = tmp_path / 'fixtures'
    config = root / 'timestamp' / 'jellyfin'
    config.parent.mkdir(parents=True)
    real = tmp_path / 'real-config'
    real.mkdir()
    config.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(rehearsal, 'ROOT', root)
    with pytest.raises(RuntimeError, match='fixture'):
        rehearsal.container_command('jellyfin', config, 'fixture')


def fixture_database(path, username='fixture-viewer'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute('CREATE TABLE Users (Id TEXT, Username TEXT)')
        connection.execute('INSERT INTO Users VALUES (?, ?)', ('fixture-id', username))


def test_snapshot_keeps_active_and_backup_database_core_separate(rehearsal, tmp_path):
    fixture_database(tmp_path / 'data/jellyfin.db')
    fixture_database(tmp_path / 'backup/jellyfin.db')
    before = rehearsal.snapshot(tmp_path)
    with closing(sqlite3.connect(tmp_path / 'data/jellyfin.db')) as connection, connection:
        connection.execute("UPDATE Users SET Username='changed-fixture-viewer'")
    after = rehearsal.snapshot(tmp_path)
    assert set(before['core']) == {
        'data/jellyfin.db:users:id,username', 'backup/jellyfin.db:users:id,username'}
    assert before['core']['data/jellyfin.db:users:id,username'] != (
        after['core']['data/jellyfin.db:users:id,username'])
    assert before['core']['backup/jellyfin.db:users:id,username'] == (
        after['core']['backup/jellyfin.db:users:id,username'])


@pytest.mark.parametrize('log,exit_code,accepted', [
    (SUCCESS_LOG, 0, True),
    (SUCCESS_LOG, 1, False),
    ('[INF] Main: Preparing the database for shutdown...\n', 0, False),
    (SUCCESS_LOG.splitlines()[0], 0, False),
    ('[INF] Main: Preparing the database for shutdown...\n' + SUCCESS_LOG.splitlines()[0],
     0, False),
    (SUCCESS_LOG + '[FTL] Main: Error while starting server\n', 0, False),
    (SUCCESS_LOG + '[FTL] Main: Unhandled Exception\n', 0, False),
    (SUCCESS_LOG + '[ERR] Failed to apply migrations\n', 0, False),
    (SUCCESS_LOG + '[ERR] Migration fixture-step failed\n', 0, False),
    (SUCCESS_LOG + '[ERR] Main: Kestrel failed to start!\n', 0, False),
])
def test_jellyfin_completion_needs_positive_ordered_proof_and_no_fatal(
    rehearsal, tmp_path, log, exit_code, accepted,
):
    path = tmp_path / 'fixture.private.log'
    path.write_text(log)
    if accepted:
        rehearsal.verify_jellyfin_completion(path, exit_code)
    else:
        with pytest.raises(RuntimeError, match='completion'):
            rehearsal.verify_jellyfin_completion(path, exit_code)


@pytest.mark.parametrize('log,change_core,accepted', [
    (SUCCESS_LOG, False, True),
    (SUCCESS_LOG, True, False),
    ('[FTL] Main: Error while starting server\n', False, False),
])
def test_rehearsal_never_reports_completed_for_exit_zero_failure_or_changed_core(
    rehearsal, tmp_path, monkeypatch, capsys, log, change_core, accepted,
):
    checkpoint = tmp_path / 'checkpoint'
    fixture_database(checkpoint / 'jellyfin/data/jellyfin.db')
    (checkpoint / 'manifest.private.json').write_text(json.dumps({
        'complete': True, 'original_services_ready': True}))
    root = tmp_path / 'fixtures'
    monkeypatch.setattr(rehearsal, 'CHECKPOINT', checkpoint)
    monkeypatch.setattr(rehearsal, 'ROOT', root)
    monkeypatch.setattr(rehearsal.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(rehearsal.os, 'umask', lambda value: None)

    def run(*command, **kwargs):
        if command[0] == 'cp':
            shutil.copytree(command[-2], command[-1])
        else:
            assert command[:3] == ('docker', 'image', 'inspect')

    class Process:
        def __init__(self, command, *, stdout, **kwargs):
            assert command[command.index('--network') + 1] == 'none'
            stdout.write(log.encode())
            stdout.flush()
            if change_core:
                database = next(root.glob('*/jellyfin/data/jellyfin.db'))
                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute("UPDATE Users SET Username='changed-fixture-viewer'")

        def wait(self, **kwargs):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(rehearsal, 'run', run)
    monkeypatch.setattr(rehearsal.subprocess, 'Popen', Process)
    if accepted:
        rehearsal.main(('jellyfin',))
        assert json.loads(capsys.readouterr().out)['official_migrations_completed'] is True
    else:
        with pytest.raises(RuntimeError):
            rehearsal.main(('jellyfin',))
        assert capsys.readouterr().out == ''
    report = json.loads(next(root.glob('*/report.private.json')).read_text())
    assert report['completed'] is accepted
