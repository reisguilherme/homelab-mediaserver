from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_playback_is_independent_readonly_and_bounded():
    config = yaml.safe_load((ROOT / 'compose.playback.yaml').read_text())
    assert set(config['services']) == {'jellyfin'}
    service = config['services']['jellyfin']
    assert 'depends_on' not in service
    assert service['user'] == '1000:1000'
    assert service['cap_drop'] == ['ALL']
    assert service['read_only'] is True
    assert service['mem_limit'] == '3g'
    assert service['pids_limit'] == 256
    assert service['stop_grace_period'] == '30s'
    mounts = {m['target']: m for m in service['volumes']}
    for target in ['/data/media', '/run/host-mountinfo', '/usr/local/bin/playback-entrypoint.sh']:
        assert mounts[target]['read_only'] is True
    assert mounts['/data/media']['source'] == '/srv/media-view/media'
    assert mounts['/run/host-mountinfo']['source'] == '/proc/1/mountinfo'
    assert mounts['/cache']['source'] == '/srv/appdata/jellyfin-cache'
    assert all(m['bind']['create_host_path'] is False for m in mounts.values())
    assert service['ports'][0]['host_ip'] == '${PLAYBACK_BIND_IP:?private VM address required}'
    assert service['logging']['options'] == {'max-size': '10m', 'max-file': '3'}
    assert not service.get('privileged')
    assert 'devices' not in service
    assert 'docker.sock' not in (ROOT / 'compose.playback.yaml').read_text()
    health = service['healthcheck']['test'][1]
    assert '/jellyfin/health' in health and '= Healthy' in health


def test_transcode_uses_dedicated_volume_without_fallback_or_ram():
    service = yaml.safe_load((ROOT / 'compose.playback.yaml').read_text())['services']['jellyfin']
    mounts = {m['target']: m for m in service['volumes']}
    assert mounts['/transcode']['source'] == '/srv/transcode/data'
    assert mounts['/transcode'].get('read_only', False) is False
    assert mounts['/transcode']['bind']['create_host_path'] is False
    marker = mounts['/run/transcode-volume-id']
    assert marker['source'] == '/srv/transcode/.homelab-volume-id'
    assert marker['read_only'] is True
    assert marker['bind']['create_host_path'] is False
    assert all(not value.startswith('/transcode:') for value in service['tmpfs'])
    assert service['environment']['PLAYBACK_TRANSCODE_ID'].startswith('${PLAYBACK_TRANSCODE_ID:?')


def test_admission_reads_atomic_replacements_and_the_guest_boot_privately():
    service = yaml.safe_load((ROOT / 'compose.playback.yaml').read_text())['services']['jellyfin']
    mounts = {m['target']: m for m in service['volumes']}
    for target, source in {
        '/run/storage-admission': '/var/lib/homelab-storage/admission',
        '/run/guest-boot-id': '/proc/sys/kernel/random/boot_id',
        '/usr/local/bin/playback-admission.sh': './scripts/playback-admission.sh',
    }.items():
        assert mounts[target]['source'] == source
        assert mounts[target]['read_only'] is True
        assert mounts[target]['bind']['create_host_path'] is False
    assert '/run/storage-admission/lease' not in mounts
