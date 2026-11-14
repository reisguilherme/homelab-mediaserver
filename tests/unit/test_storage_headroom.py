import json
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_storage_pools import registry as pool_registry

from homeserver_common.catalog import example_env
from homeserver_common.env import load_settings, parse_env
from homeserver_control.domain.capacity import available_bytes
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.__main__ import _publish_storage_queue
from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence


@pytest.fixture
def registry(tmp_path):
    return pool_registry.__wrapped__(tmp_path)


def test_headroom_is_editable_defaults_to_ten_gib_and_can_be_zero(tmp_path):
    path = tmp_path / '.env'
    path.write_text('')
    assert load_settings(path).storage_min_free_gib == 10
    assert parse_env(example_env())['HOMESERVER_STORAGE_MIN_FREE_GIB'] == '10'
    path.write_text('HOMESERVER_STORAGE_MIN_FREE_GIB=0\n')
    assert load_settings(path).storage_min_free_gib == 0
    path.write_text('HOMESERVER_STORAGE_MIN_FREE_GIB=-1\n')
    with pytest.raises(ValueError, match='HOMESERVER_STORAGE_MIN_FREE_GIB'):
        load_settings(path)


def test_domain_headroom_does_not_release_or_double_count_commitments():
    assert available_bytes(100, 200, [30, 20], min_free_bytes=10) == 40
    assert available_bytes(5, 200, [], min_free_bytes=10) == 0


@pytest.mark.parametrize('value', [-1, True, 1.5])
def test_invalid_headroom_is_rejected(value):
    with pytest.raises(ValueError, match='minimum free'):
        CapacityEvidence(100, {}, min_free_bytes=value)
    with pytest.raises(ValueError, match='minimum free'):
        PoolCapacityEvidence('ssd', 'uuid', 100, min_free_bytes=value)


def test_registered_reader_preserves_physical_free_and_propagates_each_pool(
    tmp_path, registry,
):
    from homeserver_control.worker.capacity_evidence import capacity_from_queue

    path = tmp_path / 'capacity.json'
    path.write_text(json.dumps({'pools': [registry.inspect(name).as_dict()
                                        for name in registry.pool_ids]}))
    evidence = capacity_from_queue(snapshot_path=path, data_root=tmp_path, payload=[],
                                   storage_registry=registry, min_free_bytes=100)
    for name in ('ssd', 'hdd'):
        assert evidence.pool(name).free_bytes == 650
        assert evidence.pool(name).min_free_bytes == 100
        assert evidence.pool(name).usable_free_bytes == 550


def test_legacy_reader_applies_margin_without_changing_physical_free(tmp_path, monkeypatch):
    from homeserver_control.worker.capacity_evidence import capacity_from_queue

    path = tmp_path / 'capacity.json'
    path.write_text(json.dumps({'filesystem_id': 'uuid', 'measured_at': time.time()}))
    monkeypatch.setattr('os.path.ismount', lambda _: True)
    monkeypatch.setattr('os.statvfs', lambda _: SimpleNamespace(f_bavail=30, f_frsize=100))
    evidence = capacity_from_queue(snapshot_path=path, data_root=tmp_path, payload=[],
                                   min_free_bytes=1000)
    assert evidence.free_bytes == 3000
    assert evidence.usable_free_bytes == 2000


def test_pool_selection_falls_back_without_borrowing_another_disks_margin(tmp_path, registry):
    database = tmp_path / 'db.sqlite'
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(request_id='seerr:1', source_id='1', media_key='movie:tmdb:1',
                               filesystem_id='ssd-uuid', budget_bytes=0,
                               free_bytes=650, total_bytes=1000)
    permits = PermitRegistry(database, storage_registry=registry)
    evidence = CapacityEvidence(650, {}, pools=(
        PoolCapacityEvidence('ssd', 'ssd-uuid', 650, min_free_bytes=600),
        PoolCapacityEvidence('hdd', 'hdd-uuid', 650, min_free_bytes=100),
    ))
    chosen = permits.issue(infohash='a' * 40, destination='/data/torrents', category='radarr',
                           reservation_id=reservation.reservation_id,
                           metadata_sha256='b' * 64, selected_files=('movie.mkv',),
                           budget_bytes=500, expires_at=datetime.now(UTC)+timedelta(minutes=5),
                           capacity=evidence)
    assert chosen.pool_id == 'hdd'
    assert permits.available_bytes(evidence) == 50


def test_queue_snapshot_keeps_stopped_commitments_and_deducts_margin_once(tmp_path):
    database = tmp_path / 'db.sqlite'
    ReservationRepository(database).initialize()
    permits = PermitRegistry(database)
    evidence = CapacityEvidence(100, {}, pools=(PoolCapacityEvidence(
        'ssd', 'uuid', 100, {'a' * 40: 20, 'b' * 40: 10},
        paused_hashes=frozenset({'b' * 40}), min_free_bytes=15,
    ),))
    path = tmp_path / 'queue.json'
    _publish_storage_queue(path, evidence, permits)
    pool = json.loads(path.read_text())['pools'][0]
    assert pool['pending_bytes'] == 30
    assert pool['stopped_pending_bytes'] == 10
    assert pool['min_free_bytes'] == 15
    assert pool['available_bytes'] == 55
    assert evidence.pool('ssd').free_bytes == 100


def test_gateway_rechecks_margin_before_ordinary_native_add():
    permits = PermitRegistry()
    permit = permits.issue(infohash='a' * 40, destination='/data/torrents',
                           metadata_sha256='b' * 64, budget_bytes=90,
                           expires_at=datetime.now(UTC)+timedelta(minutes=5))
    effects = []
    upstream = SimpleNamespace(add_torrent=lambda _: effects.append('native_add') or {})
    capacity = CapacityEvidence(100, {}, min_free_bytes=20)
    permits.pending_bytes = lambda *_args, **_kwargs: 90
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token='fixture',
                                   capacity_provider=lambda: capacity))
    result = client.post('/api/v2/torrents/add', json={
        'infohash': permit.infohash, 'savepath': permit.destination,
        'metadata_sha256': permit.metadata_sha256,
    }, headers={'X-Arr-Token': 'fixture', 'X-Admission-Permit': permit.token})
    assert result.status_code == 403
    assert not effects


def test_configured_gateway_uses_the_explicit_per_disk_preference(tmp_path, monkeypatch):
    from homeserver_control.gateway import app as gateway

    path = tmp_path / 'capacity.json'
    path.write_text(json.dumps({'filesystem_id': 'uuid', 'measured_at': time.time()}))
    monkeypatch.setattr('os.path.ismount', lambda _: True)
    monkeypatch.setattr('os.statvfs', lambda _: SimpleNamespace(f_bavail=100, f_frsize=1024**3))
    monkeypatch.setenv('HOMESERVER_CAPACITY_SNAPSHOT', str(path))
    monkeypatch.setenv('HOMESERVER_STORAGE_MIN_FREE_GIB', '10')
    monkeypatch.setattr(gateway, '_production_upstream', SimpleNamespace(read=lambda _: []))
    monkeypatch.setattr(gateway, '_production_permits', PermitRegistry())
    capacity = gateway._configured_capacity()
    assert capacity.free_bytes == 100 * 1024**3
    assert capacity.usable_free_bytes == 90 * 1024**3
    monkeypatch.setenv('HOMESERVER_STORAGE_MIN_FREE_GIB', '3')
    assert gateway._configured_capacity().usable_free_bytes == 97 * 1024**3


def test_replacement_and_renewal_keep_the_margin_including_old_source_bytes(tmp_path):
    database = tmp_path / 'db.sqlite'
    repository = ReservationRepository(database)
    repository.initialize()
    reservation = repository.reserve(request_id='seerr:1', source_id='1', media_key='movie:tmdb:1',
                                     filesystem_id='uuid', budget_bytes=0,
                                     free_bytes=100, total_bytes=200)
    permits = PermitRegistry(database)
    old = permits.issue(infohash='a' * 40, destination='/data/torrents', category='radarr',
                        reservation_id=reservation.reservation_id,
                        metadata_sha256='b' * 64, selected_files=('old.mkv',), budget_bytes=20,
                        expires_at=datetime.now(UTC)+timedelta(minutes=5),
                        capacity=CapacityEvidence(100, {}))
    permits.authorize(token=old.token, infohash=old.infohash, destination=old.destination,
                       metadata_sha256=old.metadata_sha256, effect=lambda _: {'accepted': True})
    evidence = CapacityEvidence(100, {old.infohash: 20},
                                paused_hashes=frozenset({old.infohash}), min_free_bytes=15)
    options = dict(infohash='c' * 40, metadata_sha256='d' * 64, selected_files=('new.mkv',),
                   capacity=evidence, expires_at=datetime.now(UTC)+timedelta(minutes=5))
    with pytest.raises(PermissionError, match='waiting_space'):
        permits.replace_confirmed(old.token, budget_bytes=90, **options)
    assert permits.get(old.token).state == 'confirmed'
    new = permits.replace_confirmed(old.token, budget_bytes=60, **options)
    # The old stopped payload remains a physical commitment in registered mode;
    # this legacy fixture also proves renewals cannot consume the safety margin.
    renewal = CapacityEvidence(100, {old.infohash: 20},
                               paused_hashes=evidence.paused_hashes, min_free_bytes=50)
    with pytest.raises(PermissionError, match='waiting_space'):
        permits.renew_replacement_authorized(new.token, capacity=renewal,
                                             expires_at=datetime.now(UTC)+timedelta(minutes=5))
    assert permits.get(new.token).state == 'authorized'


@pytest.mark.asyncio
async def test_copy_fallback_cannot_spend_the_download_margin(tmp_path):
    from homeserver_control.worker.finalization import MovieFinalizer

    source = tmp_path / 'source.mkv'
    source.write_bytes(b'x' * 20)
    finalizer = object.__new__(MovieFinalizer)
    finalizer.storage_registry = None
    finalizer.permits = SimpleNamespace(pending_bytes=lambda _: 30)

    async def capacity():
        return CapacityEvidence(60, {}, min_free_bytes=15)

    finalizer.capacity_provider = capacity
    assert not await finalizer._copy_fallback_fits([source])
