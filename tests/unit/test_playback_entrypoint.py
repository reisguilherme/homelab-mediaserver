"""Fail closed before Jellyfin sees an empty or foreign media directory."""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/playback-entrypoint.sh'
SOURCE = '192.0.2.10:/'


def mount(target, *, device='0:55', source=SOURCE, options='ro', root='/', fs='nfs4'):
    return f'100 1 {device} {root} {target} {options} - {fs} {source} ro,vers=4.2,hard,proto=tcp\n'


def check(tmp_path, host, local):
    paths = [tmp_path / 'host', tmp_path / 'local']
    for path, text in zip(paths, [host, local], strict=True):
        path.write_text(text)
    return subprocess.run(
        ['sh', str(SCRIPT), '--check', *map(str, paths)],
        env={**os.environ, 'PLAYBACK_NFS_SOURCE': SOURCE},
        text=True, capture_output=True, timeout=5,
    )


def test_exact_ro_nfs_mounts_are_accepted(tmp_path):
    result = check(tmp_path, mount('/srv/media-view/media'), mount('/data/media'))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '100:0:55:100:0:55'


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
