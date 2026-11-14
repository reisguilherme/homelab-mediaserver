"""Media consumers use the registered primary disk without requiring an SSD pool."""

import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


class SingletonRegistry:
    pool_ids = ('hdd3',)
    primary_pool_id = 'hdd3'

    def __init__(self, root):
        self.pools = {'hdd3': SimpleNamespace(
            pool_id='hdd3', label='HD USB 3', filesystem_id='uuid-hdd3', root=root,
        )}
        self.offline = False

    @property
    def primary_pool(self):
        return self.pools[self.primary_pool_id]

    def inspect(self, pool_id, *, writable=False):
        from homeserver_common.storage import StorageUnavailable

        assert pool_id == 'hdd3'
        if self.offline:
            raise StorageUnavailable('offline')
        dto = dict(pool_id=pool_id, label='HD USB 3', filesystem_id='uuid-hdd3',
                   state='ready', reason=None, measured_at=100.0,
                   total_bytes=100_000, used_bytes=30_000, free_bytes=60_000)
        return SimpleNamespace(**dto, as_dict=lambda: dto.copy())


def script(name):
    spec = importlib.util.spec_from_file_location(name, Path('scripts') / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capacity_primary_is_the_only_registered_disk(tmp_path):
    result = script('capacity-snapshot').collect_pools(SingletonRegistry(tmp_path))
    assert result['filesystem_id'] == 'uuid-hdd3'
    assert result['free_bytes'] == 60_000
    assert len(result['pools']) == 1


def test_host_storage_and_categories_use_singleton_primary(tmp_path):
    media = tmp_path / 'media/movies'
    media.mkdir(parents=True)
    (media / 'fixture.mkv').write_bytes(b'x' * 4096)
    result = script('host-metrics').collect(registry=SingletonRegistry(tmp_path))
    assert result['storage']['filesystem_id'] == 'uuid-hdd3'
    assert result['storage']['storage']['movies_bytes'] == 4096
    assert len(result['pools']) == 1


def test_api_bootstraps_expected_primary_identity_without_ssd(tmp_path, monkeypatch):
    from homeserver_control.api import app as api

    monkeypatch.setattr(api, 'load_storage_registry', lambda: SingletonRegistry(tmp_path))
    for key in ('HOMESERVER_DB_PATH', 'HOMESERVER_MEDIA_ROOTS'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(api, 'filesystem_identity', lambda *_: None)
    captured = {}
    state_type = api.ControlState

    def state(**kwargs):
        captured.update(kwargs)
        return state_type(**kwargs)

    monkeypatch.setattr(api, 'ControlState', state)
    api.create_app()
    assert captured['expected_filesystem_id'] == 'uuid-hdd3'


def test_deletion_reservation_follows_primary_and_checks_its_mount(tmp_path):
    from homeserver_control.worker.deletion_coordinator import (
        DeletionCoordinator,
        DeletionRetryable,
    )

    registry = SingletonRegistry(tmp_path)
    coordinator = object.__new__(DeletionCoordinator)
    coordinator.storage_registry = registry
    coordinator.filesystem_id = 'uuid-hdd3'
    assert coordinator._reservation_identity_valid('uuid-hdd3')
    assert coordinator._reservation_identity_valid(f'device:{tmp_path.stat().st_dev}')
    assert not coordinator._reservation_identity_valid('uuid-ssd')
    registry.offline = True
    with pytest.raises(DeletionRetryable):
        coordinator._reservation_identity_valid('uuid-hdd3')


def test_host_command_publishes_singleton_capacity_without_legacy_root(tmp_path, monkeypatch):
    from homeserver_common import storage

    metrics = script('host-metrics')
    registry = SingletonRegistry(tmp_path)
    monkeypatch.setattr(storage, 'load_storage_registry', lambda _: registry)
    output = tmp_path / 'run/host.json'
    monkeypatch.setattr('sys.argv', ['host-metrics.py', '--output', str(output)])
    assert metrics.main() == 0
    capacity = json.loads((output.parent / 'capacity.json').read_text())
    assert capacity['filesystem_id'] == 'uuid-hdd3'
    assert capacity['free_bytes'] == 60_000
    assert [p['pool_id'] for p in capacity['pools']] == ['hdd3']


def test_registered_capacity_compatibility_fields_follow_primary(tmp_path, monkeypatch):
    from homeserver_control.worker import capacity_evidence

    monkeypatch.setattr(capacity_evidence.time, 'time', lambda: 100.0)
    registry = SingletonRegistry(tmp_path)
    path = tmp_path / 'capacity.json'
    path.write_text(json.dumps({'pools': [registry.inspect('hdd3').as_dict()]}))
    evidence = capacity_evidence.capacity_from_queue(
        snapshot_path=path, data_root=tmp_path, payload=[], storage_registry=registry,
    )
    assert evidence.free_bytes == 60_000
    assert evidence.pool('hdd3').free_bytes == 60_000


@pytest.mark.asyncio
async def test_worker_reserves_against_hdd3_and_rejects_stale_ssd_capacity(tmp_path, monkeypatch):
    from homeserver_control.worker import __main__ as worker

    for key in list(worker.os.environ):
        if key.startswith('HOMESERVER_'):
            monkeypatch.delenv(key)
    snapshot = tmp_path / 'capacity.json'
    payload = dict(filesystem_id='uuid-hdd3', measured_at=time.time(),
                   free_bytes=60_000, total_bytes=100_000)
    snapshot.write_text(json.dumps(payload))
    for key, value in {'SEERR_URL': 'http://seerr', 'SEERR_API_KEY': 'fixture',
                       'CAPACITY_SNAPSHOT': str(snapshot),
                       'RECOVERY_MODE': str(tmp_path / 'RECOVERY_MODE')}.items():
        monkeypatch.setenv('HOMESERVER_' + key, value)
    monkeypatch.setattr(worker, 'load_storage_registry', lambda: SingletonRegistry(tmp_path))
    cycle = worker._build_cycle(tmp_path / 'control.sqlite')
    try:
        assert cycle.scheduler.snapshot_provider().filesystem_id == 'uuid-hdd3'
        payload['filesystem_id'] = 'uuid-ssd'
        snapshot.write_text(json.dumps(payload))
        assert cycle.scheduler.snapshot_provider().filesystem_id is None
    finally:
        await worker._run_forever(cycle, should_run=lambda: False, interval=0)
