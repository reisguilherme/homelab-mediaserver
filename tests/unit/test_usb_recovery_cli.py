import fcntl
import importlib.util
import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def cli(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[2] / 'scripts/usb-recovery.py'
    spec = importlib.util.spec_from_file_location('usb_recovery_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    installed = tmp_path / 'installed'
    installed.mkdir()
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({
        'version': 1, 'project_dir': '/srv/project', 'compose_project': 'homeserver',
        'actor_name': 'homeserver-new-batch-io', 'installed_dir': str(installed),
        'minimum_free_bytes': 10, 'pools': [
            {'pool_id': p, 'filesystem_id': p + '-fixture', 'serial': p + '-serial',
             'size_bytes': 100, 'disk_size_bytes': 120, 'partition_number': n}
            for p, n in [('ssd', 0), ('hdd', 3), ('hdd2', 1)]],
    }))
    config.chmod(0o600)
    monkeypatch.setattr(module, 'STATE', state)
    monkeypatch.setattr(module, 'INSTALLED', installed)
    monkeypatch.setattr(module, 'CONFIG', config)
    monkeypatch.setattr(module, 'trusted', lambda *a, **kw: None)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module.os, 'readlink', lambda p: 'host-namespace')
    monkeypatch.setattr(module.syslog, 'syslog', lambda *a: None)
    return module


def test_nonroot_cannot_operate(cli, monkeypatch):
    monkeypatch.setattr(cli.os, 'geteuid', lambda: 1000)
    with pytest.raises(ValueError, match='requires_host_root'):
        cli.main(['--once'])
    assert not (cli.STATE / 'lock').exists()


def test_cli_can_select_registered_third_usb(cli, monkeypatch, capsys):
    config = json.loads(cli.CONFIG.read_text())
    config['pools'].append(dict(config['pools'][-1], pool_id='hdd3',
                                filesystem_id='hdd3-fixture', serial='hdd3-serial'))
    cli.CONFIG.write_text(json.dumps(config))
    calls = []

    class Controller:
        def __init__(self, backend, path):
            calls.append(backend.config)

        def tick(self, force_pools):
            calls.append(force_pools)
            return {'phase': 'ready'}

    monkeypatch.setattr('homeserver_common.usb_recovery.RecoveryController', Controller)
    assert cli.main(['--once', '--recover', 'hdd3']) == 0
    assert calls[-1] == ('hdd3',)
    assert json.loads(capsys.readouterr().out)['phase'] == 'ready'


def test_held_lock_skips_execution(cli, capsys):
    fd = os.open(cli.STATE / 'lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert cli.main(['--once']) == 0
        assert json.loads(capsys.readouterr().out) == {'phase': 'busy'}
        assert not (cli.STATE / 'state.json').exists()
    finally:
        os.close(fd)


def test_status_only_exposes_summary_and_disable_preserves_event(cli, capsys):
    document = {'phase': 'waiting', 'reason': 'devices-unavailable', 'updated_at': 10,
                'event': {'snapshot': [{'private': 'hidden-native-evidence'}]}}
    state = cli.STATE / 'state.json'
    state.write_text(json.dumps(document))
    state.chmod(0o600)
    assert cli.main(['--status']) == 0
    assert json.loads(capsys.readouterr().out) == {
        'phase': 'waiting', 'reason': 'devices-unavailable', 'updated_at': 10, 'enabled': True}
    assert cli.main(['--disable']) == 0
    assert json.loads(capsys.readouterr().out) == {'enabled': False}
    assert cli.main(['--once']) == 0
    assert json.loads(capsys.readouterr().out) == {'phase': 'disabled'}
    assert json.loads(state.read_text()) == document
    assert cli.main(['--enable']) == 0
    assert json.loads(capsys.readouterr().out) == {'enabled': True}
    assert json.loads(state.read_text()) == document


def test_cli_rejects_group_readable_private_state(cli):
    state = cli.STATE / 'state.json'
    state.write_text('{}')
    state.chmod(0o640)
    with pytest.raises(ValueError, match='private_state_permissions'):
        cli.main(['--status'])
