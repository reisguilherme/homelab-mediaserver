import subprocess
from pathlib import Path

import yaml

from homeserver_common.render import render_storage_override


def test_optional_override_keeps_one_logical_data_mount_and_no_shadowing():
    services = render_storage_override()['services']
    for name in ('sonarr', 'radarr', 'qbittorrent', 'control-worker'):
        binds = services[name]['volumes']
        assert [(v['source'], v['target']) for v in binds if v['target'].startswith('/data')] == [
            ('/srv/media-view', '/data')]
    for name in ('control-api', 'download-gateway', 'host-metrics', 'telemetry', 'init'):
        physical = [v for v in services[name]['volumes'] if v['target'].startswith('/storage')]
        assert len(physical) == 2 and all(v['read_only'] for v in physical)
        assert any(v['target'] == '/run/host-mountinfo' and v['read_only']
                   for v in services[name]['volumes'])
    worker = services['control-worker']
    assert worker['user'] == '0:0'
    assert worker['cap_drop'] == ['ALL']
    assert set(worker['cap_add']) == {'CHOWN', 'DAC_OVERRIDE', 'SETUID', 'SETGID'}
    assert all(v['bind']['create_host_path'] is False
               for spec in services.values() for v in spec['volumes'])


def test_checked_in_override_matches_renderer_and_contains_no_uuid_or_env_knob():
    root = Path(__file__).resolve().parents[2]
    data = yaml.safe_load((root / 'compose.storage.yaml').read_text())
    assert data == render_storage_override()
    assert all('environment' not in spec for spec in data['services'].values())


def test_native_entrypoint_rejects_ordinary_bind_and_execs_only_mergerfs(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = (root / 'scripts/storage-entrypoint.sh').read_text()
    table = tmp_path / 'mountinfo'
    executable = tmp_path / 'entrypoint.sh'
    executable.write_text(script.replace('/proc/self/mountinfo', str(table)))
    command = ['sh', str(executable), '/data', 'sh', '-c', 'printf executed']
    table.write_text('1 0 8:1 / /data rw - ext4 /dev/sda1 rw\n')
    refused = subprocess.run(command, capture_output=True, text=True)
    assert refused.returncode == 2 and 'executed' not in refused.stdout
    table.write_text('1 0 0:99 / /data rw - fuse.mergerfs data:external/homeserver rw\n')
    accepted = subprocess.run(command, capture_output=True, text=True)
    assert accepted.returncode == 0 and accepted.stdout == 'executed'
