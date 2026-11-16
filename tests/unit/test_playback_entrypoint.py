"""Fail closed before Jellyfin sees an empty or foreign media directory."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/playback-entrypoint.sh'
SOURCE = '192.0.2.10:/'
TRANSCODE_ID = 'b2256ce1-b29c-4608-a227-c78a50ea169e'
BOOT_ID = 'ca9a5731-8b28-4400-8ee9-138e1485de61'
GENERATION = '21ea0f809f77421a9ce2a040376a43da'


def mount(target, *, device='0:55', source=SOURCE, options='ro', root='/', fs='nfs4'):
    return f'100 1 {device} {root} {target} {options} - {fs} {source} ro,vers=4.2,hard,proto=tcp\n'


def transcode_mount(target, *, device='8:16', root='/', options='rw', fs='ext4', mount_id=200):
    return (
        f'{mount_id} 1 {device} {root} {target} {options},nodev,nosuid,noexec '
        f'- {fs} /dev/sdb rw,relatime\n'
    )


def transcode_host():
    return transcode_mount('/srv/transcode')


def transcode_local():
    return transcode_mount('/transcode', root='/data', mount_id=201) + transcode_mount(
        '/run/transcode-volume-id', root='/.homelab-volume-id', options='ro', mount_id=202,
    )


def check(tmp_path, host, local, *, transcode=None, marker=None, expected=TRANSCODE_ID,
          admission=None):
    host_extra, local_extra = transcode or (transcode_host(), transcode_local())
    host += host_extra
    local += local_extra
    paths = [tmp_path / 'host', tmp_path / 'local']
    for path, text in zip(paths, [host, local], strict=True):
        path.write_text(text)
    marker_path = tmp_path / 'marker'
    marker_path.write_text(TRANSCODE_ID + '\n' if marker is None else marker)
    values = admission or (
        f'version=1\nstate=ready\nboot_id={BOOT_ID}\ngeneration={GENERATION}\n'
        'expires_uptime_seconds=145\n', BOOT_ID + '\n', '100.00 90.00\n',
    )
    admission_paths = [tmp_path / name for name in ['lease', 'boot', 'uptime']]
    for path, value in zip(admission_paths, values, strict=True):
        path.write_text(value)
    return subprocess.run(
        ['sh', str(SCRIPT), '--check', *map(str, paths), str(marker_path),
         *map(str, admission_paths)],
        env={**os.environ, 'PLAYBACK_NFS_SOURCE': SOURCE, 'PLAYBACK_TRANSCODE_ID': expected},
        text=True, capture_output=True, timeout=5,
    )


def test_exact_ro_nfs_mounts_are_accepted(tmp_path):
    result = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f'100:0:55:100:0:55:200:8:16:201:8:16:202:8:16:{BOOT_ID}:{GENERATION}'
    )


@pytest.mark.parametrize('change', [
    'missing-host', 'missing-local', 'foreign-host', 'foreign-local', 'host-rw', 'local-rw',
    'subtree', 'device', 'ext4', 'duplicate', 'soft', 'udp', 'nested-local', 'nested-host',
])
def test_unsafe_or_replaced_mounts_refuse_start(tmp_path, change):
    host, local = mount('/srv/media-view/media'), mount('/data/media')
    if change == 'missing-host':
        host = ''
    elif change == 'missing-local':
        local = ''
    elif change == 'foreign-host':
        host = mount('/srv/media-view/media', source='192.0.2.11:/')
    elif change == 'foreign-local':
        local = mount('/data/media', source='192.0.2.11:/')
    elif change == 'host-rw':
        host = mount('/srv/media-view/media', options='rw')
    elif change == 'local-rw':
        local = mount('/data/media', options='rw')
    elif change == 'subtree':
        local = mount('/data/media', root='/other')
    elif change == 'device':
        local = mount('/data/media', device='0:99')
    elif change == 'ext4':
        local = mount('/data/media', fs='ext4')
    elif change == 'duplicate':
        local += local
    elif change in ('soft', 'udp'):
        local = local.replace('hard', 'soft') if change == 'soft' else local.replace('tcp', 'udp')
    elif change == 'nested-local':
        local += mount('/data/media/movies', fs='ext4')
    elif change == 'nested-host':
        host += mount('/srv/media-view/media/movies', fs='ext4')
    result = check(tmp_path, host, local)
    assert result.returncode != 0


def test_missing_expected_source_is_rejected(tmp_path):
    result = subprocess.run(
        ['sh', str(SCRIPT), '--check', '/dev/null', '/dev/null'],
        env={key: value for key, value in os.environ.items() if key != 'PLAYBACK_NFS_SOURCE'},
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0


@pytest.mark.parametrize('change', [
    'missing-host', 'missing-data', 'missing-marker', 'host-ro', 'data-ro', 'marker-rw',
    'host-tmpfs', 'data-tmpfs', 'marker-tmpfs', 'host-subtree', 'data-root', 'marker-root',
    'foreign-data', 'foreign-marker', 'duplicate-host', 'duplicate-data', 'duplicate-marker',
    'nested-host', 'nested-data', 'host-super-ro', 'unsafe-host', 'unsafe-data', 'root-disk',
])
def test_transcode_unsafe_or_missing_volume_refuses_start(tmp_path, change):
    host, local = transcode_host(), transcode_local()
    if change == 'missing-host':
        host = ''
    elif change == 'missing-data':
        local = local.splitlines(keepends=True)[1]
    elif change == 'missing-marker':
        local = local.splitlines(keepends=True)[0]
    elif change == 'host-ro':
        host = transcode_mount('/srv/transcode', options='ro')
    elif change == 'data-ro':
        local = local.replace('/transcode rw,', '/transcode ro,')
    elif change == 'marker-rw':
        local = local.replace('/run/transcode-volume-id ro,', '/run/transcode-volume-id rw,')
    elif change == 'host-tmpfs':
        host = host.replace('ext4', 'tmpfs')
    elif change == 'data-tmpfs':
        local = local.replace('ext4', 'tmpfs', 1)
    elif change == 'marker-tmpfs':
        local = local.rsplit('ext4', 1)[0] + 'tmpfs' + local.rsplit('ext4', 1)[1]
    elif change == 'host-subtree':
        host = transcode_mount('/srv/transcode', root='/other')
    elif change == 'data-root':
        local = local.replace(' /data /transcode ', ' / /transcode ')
    elif change == 'marker-root':
        local = local.replace(' /.homelab-volume-id ', ' /another-marker ')
    elif change == 'foreign-data':
        local = local.replace('8:16', '8:32', 1)
    elif change == 'foreign-marker':
        local = local.replace('202 1 8:16', '202 1 8:32')
    elif change == 'duplicate-host':
        host += host
    elif change == 'duplicate-data':
        local += local.splitlines(keepends=True)[0]
    elif change == 'duplicate-marker':
        local += local.splitlines(keepends=True)[1]
    elif change == 'nested-host':
        host += transcode_mount('/srv/transcode/data')
    elif change == 'nested-data':
        local += transcode_mount('/transcode/nested')
    elif change == 'host-super-ro':
        host = host.replace('/dev/sdb rw,', '/dev/sdb ro,')
    elif change == 'unsafe-host':
        host = host.replace(',noexec', '')
    elif change == 'unsafe-data':
        local = local.replace(',nosuid', '', 1)
    elif change == 'root-disk':
        host += transcode_mount('/', device='8:16', mount_id=99)
    result = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'),
                   transcode=(host, local))
    assert result.returncode != 0


@pytest.mark.parametrize('marker', ['', 'wrong-id\n', TRANSCODE_ID + '\n\n',
                                   TRANSCODE_ID + '\nother\n', TRANSCODE_ID + ' \n'])
def test_transcode_marker_must_match_single_expected_uuid(tmp_path, marker):
    result = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'), marker=marker)
    assert result.returncode != 0


@pytest.mark.parametrize('expected', ['', 'not-a-uuid', 'b2256ce1-b29c-4608-a227-c78a50ea169z'])
def test_transcode_expected_uuid_is_required(tmp_path, expected):
    result = check(
        tmp_path, mount('/srv/media-view/media'), mount('/data/media'), expected=expected,
    )
    assert result.returncode != 0


@pytest.mark.parametrize('old, new', [('200 1', '203 1'), ('201 1', '204 1'), ('202 1', '205 1')])
def test_each_transcode_mount_identity_is_in_runtime_snapshot(tmp_path, old, new):
    before = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'))
    after = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'), transcode=(
        transcode_host().replace(old, new), transcode_local().replace(old, new),
    ))
    assert before.returncode == after.returncode == 0
    assert before.stdout != after.stdout


@pytest.mark.parametrize('state, expiry, boot', [
    ('hold', 145, BOOT_ID), ('ready', 100, BOOT_ID), ('ready', 146, BOOT_ID),
    ('ready', 145, 'ca9a5731-8b28-4400-8ee9-138e1485de62'),
])
def test_valid_mounts_without_live_admission_still_refuse_start(tmp_path, state, expiry, boot):
    result = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'), admission=(
        f'version=1\nstate={state}\nboot_id={boot}\ngeneration={GENERATION}\n'
        f'expires_uptime_seconds={expiry}\n', BOOT_ID + '\n', '100.00 90.00\n',
    ))
    assert result.returncode != 0
    assert result.stdout == ''


def test_admission_generation_is_in_full_runtime_snapshot(tmp_path):
    before = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'))
    after = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'), admission=(
        f'version=1\nstate=ready\nboot_id={BOOT_ID}\n'
        'generation=bc7e260afda04822a3a6f14a33a36970\nexpires_uptime_seconds=145\n',
        BOOT_ID + '\n', '100.00 90.00\n',
    ))
    assert before.returncode == after.returncode == 0
    assert before.stdout != after.stdout
