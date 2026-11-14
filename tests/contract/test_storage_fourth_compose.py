from pathlib import Path

import yaml

from homeserver_common import render


def test_fourth_pool_override_adds_only_guarded_physical_binds():
    extra = render.render_storage_fourth_override()
    assert set(extra['services']) == {
        'control-worker', 'control-api', 'download-gateway', 'telemetry',
        'host-metrics', 'init', 'operator',
    }
    for name, service in extra['services'].items():
        assert service['volumes'] == [{
            'type': 'bind', 'source': '/srv/external3', 'target': '/storage/hdd3',
            'read_only': name != 'control-worker', 'bind': {'create_host_path': False},
        }]
    root = Path(__file__).resolve().parents[2]
    assert yaml.safe_load((root / 'compose.storage.fourth.yaml').read_text()) == extra
    for earlier in (render.render_storage_override(), render.render_storage_extra_override()):
        assert all(volume['target'] != '/storage/hdd3'
                   for service in earlier['services'].values() for volume in service['volumes'])
