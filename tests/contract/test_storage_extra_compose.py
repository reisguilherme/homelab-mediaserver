from pathlib import Path

import yaml

from homeserver_common import render


def test_second_hd_override_binds_each_storage_consumer_without_shadowing_data():
    extra = render.render_storage_extra_override()
    for name in ('control-worker', 'control-api', 'download-gateway', 'telemetry',
                 'host-metrics', 'init', 'operator'):
        binds = extra['services'][name]['volumes']
        assert len(binds) == 1
        assert binds[0]['source'] == '/srv/external2'
        assert binds[0]['target'] == '/storage/hdd2'
        assert binds[0]['read_only'] is (name != 'control-worker')
        assert binds[0]['bind']['create_host_path'] is False
    standard = render.render_storage_override()
    assert all(volume['target'] != '/storage/hdd2'
               for service in standard['services'].values() for volume in service['volumes'])
    root = Path(__file__).resolve().parents[2]
    assert yaml.safe_load((root / 'compose.storage.extra.yaml').read_text()) == extra
