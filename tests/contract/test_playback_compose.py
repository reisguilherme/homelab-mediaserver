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
