"""A third USB disk is an optional independent placement and capacity pool."""
import json
import os
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_third_storage_pool as third
from test_storage_installation import installer

from homeserver_common.storage import StorageRegistryError, load_storage_registry
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import (
    CapacityEvidence,
    PoolCapacityEvidence,
    capacity_from_queue,
)


def document():
    return {'version': 1, 'pools': [
        {'pool_id': name, 'filesystem_id': name + '-uuid', 'capabilities': third.CAPS}
        for name in ('hdd3', 'hdd2', 'hdd', 'ssd')
    ]}


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(third, 'document', document)
    return third.registry.__wrapped__(tmp_path)


@pytest.mark.parametrize('names', [
    ('ssd', 'hdd'), ('ssd', 'hdd', 'hdd2'), ('ssd', 'hdd', 'hdd2', 'hdd3'),
])
def test_two_three_and_four_pool_registries_keep_stable_paths(tmp_path, names):
    value = document()
    value['pools'] = [entry for entry in value['pools'] if entry['pool_id'] in names]
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(value))
    loaded = load_storage_registry(path)
    assert loaded.pool_ids == names
    if 'hdd3' in names:
        assert loaded.pools['hdd3'].root == Path('/storage/hdd3/homeserver')
        assert loaded.pools['hdd3'].host_mount == Path('/srv/external3')
        assert loaded.pools['hdd3'].host_root == Path('/srv/external3/homeserver')


def test_fourth_pool_cannot_reuse_any_existing_filesystem(tmp_path):
    value = document()
    value['pools'][0]['filesystem_id'] = 'hdd2-uuid'
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(value))
    with pytest.raises(StorageRegistryError, match='distinct physical identities'):
        load_storage_registry(path)


def test_fourth_pool_claim_and_counterpart_protect_existing_sources(registry):
    logical = registry.prepare_destination('hdd3', 'new-source')
    assert registry.resolve('hdd3', logical).is_dir()
    for other in ('ssd', 'hdd', 'hdd2'):
        assert not (registry.pools[other].root / 'torrents/.placements/new-source').exists()
        with pytest.raises(RuntimeError, match='counterpart|another physical pool'):
            registry.prepare_destination(other, 'new-source')
    registry.prepare_destination('hdd2', 'existing-source')
    with pytest.raises(RuntimeError, match='counterpart|another physical pool'):
        registry.prepare_destination('hdd3', 'existing-source')


def issue(tmp_path, registry, free, budget):
    database = tmp_path / 'control.sqlite'
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(
        request_id='seerr:fourth', source_id='1', media_key='movie:tmdb:1',
        filesystem_id='fixture', budget_bytes=1, free_bytes=10_000, total_bytes=20_000,
    )
    capacity = CapacityEvidence(free[0], {}, pools=tuple(
        PoolCapacityEvidence(name, name + '-uuid', size, min_free_bytes=100)
        for name, size in zip(('ssd', 'hdd', 'hdd2', 'hdd3'), free, strict=True)
    ))
    return PermitRegistry(database, storage_registry=registry).issue(
        infohash='a' * 40, destination='/data/torrents', category='radarr',
        reservation_id=reservation.reservation_id, budget_bytes=budget,
        capacity=capacity, expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.parametrize(('free', 'expected'), [
    ((1_000, 1_000, 1_000, 1_000), 'ssd'),
    ((200, 1_000, 1_000, 1_000), 'hdd'),
    ((200, 300, 1_000, 1_000), 'hdd2'),
    ((200, 300, 400, 1_000), 'hdd3'),
])
def test_fourth_pool_is_used_only_after_earlier_pools_cannot_fit(
    tmp_path, registry, free, expected,
):
    permit = issue(tmp_path, registry, free, 800)
    assert permit.pool_id == expected
    assert permit.filesystem_id == expected + '-uuid'


def test_four_small_pools_cannot_be_summed_for_admission(tmp_path, registry):
    with pytest.raises(PermissionError, match='waiting_space'):
        issue(tmp_path, registry, (400, 400, 400, 400), 800)


def test_absent_fourth_disk_never_uses_its_fallback_directory(tmp_path, registry):
    (registry.uuid_dir / 'hdd3-uuid').unlink()
    with pytest.raises(PermissionError, match='waiting_space'):
        issue(tmp_path, registry, (200, 300, 400, 1_000), 800)
    assert not list((registry.pools['hdd3'].root / 'torrents/.placements').iterdir())


def test_fourth_pool_queue_is_charged_only_to_its_own_filesystem(tmp_path, registry):
    snapshot = tmp_path / 'capacity.json'
    snapshot.write_text(json.dumps({'pools': [registry.inspect(name).as_dict()
                                            for name in registry.pool_ids]}))
    evidence = capacity_from_queue(
        snapshot_path=snapshot, data_root=tmp_path, storage_registry=registry,
        min_free_bytes=500, payload=[{
            'hash': 'b' * 40, 'pool_id': 'hdd3', 'filesystem_id': 'hdd3-uuid',
            'placement_verified': True, 'admitted': True, 'total_size': 1_000,
            'amount_left': 300, 'state': 'stoppedDL',
        }],
    )
    assert evidence.pool('hdd3').remaining_by_hash == {'b' * 40: 300}
    assert evidence.pool('hdd3').usable_free_bytes == 9_500
    assert all(not evidence.pool(name).remaining_by_hash for name in ('ssd', 'hdd', 'hdd2'))


def test_installer_stages_all_four_proven_filesystems(tmp_path):
    output = tmp_path / 'storage.json'
    installer().publish_registry(
        output, {'ssd': 'a', 'hdd': 'b', 'hdd2': 'c', 'hdd3': 'd'},
        dict.fromkeys(('ssd', 'hdd', 'hdd2', 'hdd3'), third.CAPS),
    )
    loaded = load_storage_registry(output)
    assert loaded.pool_ids == ('ssd', 'hdd', 'hdd2', 'hdd3')


def test_installer_four_pool_check_uses_all_mounts_without_writes(monkeypatch, tmp_path):
    module = installer()
    checked, branches, guarded = [], [], []
    monkeypatch.setattr(module, 'verify_physical', lambda root, uuid: checked.append((root, uuid)))
    monkeypatch.setattr(module, 'verify_view', lambda view, roots: branches.extend(roots))
    monkeypatch.setattr(module, 'guard', guarded.append)
    output = tmp_path / 'never-published.json'
    assert module.main([
        '--ssd-uuid', 'a', '--hdd-uuid', 'b', '--hdd2-uuid', 'c', '--hdd3-uuid', 'd',
        '--check', '--output', str(output),
    ]) == 0
    assert checked == [(Path('/srv/data'), 'a'), (Path('/srv/external'), 'b'),
                       (Path('/srv/external2'), 'c'), (Path('/srv/external3'), 'd')]
    assert branches == [Path('/srv/data'), Path('/srv/external/homeserver'),
                        Path('/srv/external2/homeserver'), Path('/srv/external3/homeserver')]
    assert len(guarded) == 4
    assert not output.exists()


@pytest.mark.parametrize('missing_fourth', [False, True])
def test_startup_requires_fourth_branch_and_every_placement_guard(
    monkeypatch, tmp_path, missing_fourth,
):
    from homeserver_common import storage_startup
    from homeserver_common.storage import StorageUnavailable

    roots = [Path('/srv/data'), Path('/srv/external/homeserver'),
             Path('/srv/external2/homeserver'), Path('/srv/external3/homeserver')]
    guarded = []
    registry = SimpleNamespace(
        primary_pool_id='ssd',
        pools={str(index): SimpleNamespace(host_root=root) for index, root in enumerate(roots)},
        inspect=lambda *_: None, _guard_parent=lambda pool: guarded.append(pool.host_root),
        host_mountinfo=tmp_path / 'host', mountinfo=tmp_path / 'local',
    )
    device = tmp_path.stat().st_dev
    host = SimpleNamespace(target=Path('/srv/media-view'), filesystem='fuse.mergerfs',
                           root=Path('/'), device=device)
    local = SimpleNamespace(target=tmp_path, filesystem='fuse.mergerfs',
                            root=Path('/'), device=device)
    monkeypatch.setattr(storage_startup, 'load_storage_registry', lambda *_: registry)
    monkeypatch.setattr(storage_startup, '_mounts',
                        lambda path: [host] if path == registry.host_mountinfo else [local])
    attrs = {'branches': ':'.join(str(root) + '=RW'
                                 for root in roots[:3 if missing_fourth else 4]),
             'category.create': 'epff', 'ignorepponrename': 'true', 'moveonenospc': 'false',
             'link_cow': 'false', 'symlinkify': 'false', 'cache.files': 'auto-full',
             'dropcacheonclose': 'true'}
    monkeypatch.setattr(storage_startup.os, 'getxattr',
                        lambda _, key: attrs[key.removeprefix('user.mergerfs.')].encode())
    if missing_fourth:
        with pytest.raises(StorageUnavailable, match='branches'):
            storage_startup.verify_storage_startup(tmp_path / 'registry', tmp_path)
    else:
        assert storage_startup.verify_storage_startup(tmp_path / 'registry', tmp_path)
    assert guarded == roots


@pytest.mark.skipif(os.geteuid() != 0, reason='requires real root to UID1000 fixture')
def test_installer_fourth_pool_probe_checks_all_other_pool_guards():
    with tempfile.TemporaryDirectory(prefix='homeserver-fourth-pool-', dir='/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        branches = [root / name for name in ('ssd', 'hdd', 'hdd2', 'hdd3')]
        for branch in branches:
            (branch / 'torrents/.placements').mkdir(parents=True, mode=0o755)
            (branch / 'media').mkdir(mode=0o755)
        assert installer().probe(branches[3], branches[:3], branches[3]) == third.CAPS
        assert all(not list((branch / 'torrents/.placements').iterdir()) for branch in branches)
        assert all(not list((branch / 'media').iterdir()) for branch in branches)


def test_telemetry_reports_fourth_disk_capacity_independently(tmp_path):
    from homeserver_telemetry.status import StatusProvider

    now = time.time()
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(document()))
    pools = [{'pool_id': name, 'filesystem_id': name + '-uuid', 'state': 'ready',
              'measured_at': now, 'total_bytes': 1_000, 'used_bytes': 300, 'free_bytes': 650}
             for name in ('ssd', 'hdd', 'hdd2', 'hdd3')]
    provider = StatusProvider(
        media_root=tmp_path, host_path=tmp_path / 'host.json',
        capacity_path=tmp_path / 'capacity.json', storage_registry_path=path,
    )
    actual = provider._pools({'pools': pools}, {'pools': pools}, now=now, host_state='ok')
    assert [pool['pool_id'] for pool in actual] == ['ssd', 'hdd', 'hdd2', 'hdd3']
    assert actual[3]['state'] == 'ready'
    assert actual[3]['free_bytes'] == 650
