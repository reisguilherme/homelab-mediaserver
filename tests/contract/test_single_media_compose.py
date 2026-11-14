"""The singleton override has no dependency on retired media devices."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from homeserver_common import render


def test_singleton_override_is_standalone_and_has_only_target_storage():
    data = render.render_storage_single_override()
    for name in ('control-worker', 'control-api', 'download-gateway', 'telemetry',
                 'host-metrics', 'init', 'operator'):
        physical = [v for v in data['services'][name]['volumes']
                    if v['target'].startswith('/storage/')]
        assert [(v['source'], v['target']) for v in physical] == [
            ('/srv/external3', '/storage/hdd3')]
        assert physical[0]['read_only'] == (name != 'control-worker')
    for name in ('sonarr', 'radarr', 'qbittorrent', 'jellyfin', 'bazarr'):
        service = data['services'][name]
        assert service['environment']['HOMESERVER_STORAGE_SINGLE_GUARD'] == '1'
        volumes = {v['target']: v for v in service['volumes']}
        assert volumes['/run/storage-physical']['source'] == '/srv/external3'
        assert volumes['/run/storage-native.identity']['read_only']
        assert volumes['/run/host-mountinfo']['read_only']
        assert volumes['/run/host-uuids']['read_only']


def test_singleton_checked_override_matches_renderer():
    assert yaml.safe_load(Path('compose.storage.single.yaml').read_text()) == (
        render.render_storage_single_override())


@pytest.mark.parametrize('fault', [None, 'missing', 'foreign', 'readonly', 'missing-identity',
                                 'stale-logical', 'wrong-logical-root', 'readonly-logical'])
@pytest.mark.parametrize(('target', 'correct_root'), [('/data', '/'), ('/data/media', '/media')])
def test_singleton_native_guard_checks_physical_disk_even_with_fuse(
    tmp_path, fault, target, correct_root,
):
    mountinfo = tmp_path / 'mountinfo'
    hostinfo = tmp_path / 'hostinfo'
    uuids = tmp_path / 'uuids'
    identity = tmp_path / 'storage-native.identity'
    physical = tmp_path / 'physical'
    uuids.mkdir()
    physical.mkdir()
    uuid = 'fixture-target-uuid'
    (uuids / uuid).symlink_to('../../' + ('wrong1' if fault == 'foreign' else 'sdz1'))
    if fault != 'missing-identity':
        identity.write_text(f'homeserver-single-media-v1 hdd3 {uuid}\n')
    logical_root = '/wrong' if fault == 'wrong-logical-root' else correct_root
    logical_mode = 'ro' if fault == 'readonly-logical' else 'rw'
    mountinfo.write_text(
        f'1 0 0:99 {logical_root} {target} {logical_mode} - fuse.mergerfs hdd3/homeserver rw\n'
        f'2 0 8:33 / {physical} ro - ext4 /dev/sdz1 rw\n')
    union_device = '0:100' if fault == 'stale-logical' else '0:99'
    hostinfo.write_text('' if fault == 'missing' else (
        '2 0 8:33 / /srv/external3 ' + ('ro' if fault == 'readonly' else 'rw')
        + ' - ext4 /dev/sdz1 ' + ('ro' if fault == 'readonly' else 'rw') + '\n'
        + f'3 0 {union_device} / /srv/media-view rw - fuse.mergerfs hdd3/homeserver rw\n'))
    content = Path('scripts/storage-entrypoint.sh').read_text()
    for old, new in [('/proc/self/mountinfo', mountinfo), ('/run/host-mountinfo', hostinfo),
                     ('/run/host-uuids', uuids), ('/run/storage-native.identity', identity),
                     ('/run/storage-physical', physical)]:
        content = content.replace(old, str(new))
    entrypoint = tmp_path / 'entrypoint.sh'
    entrypoint.write_text(content)
    result = subprocess.run(['sh', str(entrypoint), target, 'sh', '-c', 'printf executed'],
                            env=dict(os.environ, HOMESERVER_STORAGE_SINGLE_GUARD='1'),
                            capture_output=True, text=True)
    if fault in (None, 'readonly-logical'):
        assert result.returncode == 0 and result.stdout == 'executed'
    else:
        assert result.returncode == 2 and 'executed' not in result.stdout
