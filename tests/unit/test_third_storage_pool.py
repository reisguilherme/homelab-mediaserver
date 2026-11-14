"""A second USB disk remains an independent, guarded capacity pool."""
import json
import os
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_storage_installation import installer

from homeserver_common.storage import StorageRegistry, StorageRegistryError, load_storage_registry
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import (
    CapacityEvidence,
    PoolCapacityEvidence,
    capacity_from_queue,
)

CAPS = dict.fromkeys(('hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement'), True)


def document():
    # Deliberately unordered: admission order is technical policy, not JSON order.
    return {'version': 1, 'pools': [
        {'pool_id': name, 'filesystem_id': name + '-uuid', 'capabilities': CAPS}
        for name in ('hdd2', 'hdd', 'ssd')
    ]}


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(document()))
    loaded = load_storage_registry(path)
    pools, devices, host_lines, local_lines = {}, {}, [], []
    uuids = tmp_path / 'uuids'
    uuids.mkdir()
    for index, (name, pool) in enumerate(loaded.pools.items(), 1):
        root = tmp_path / name
        (root / 'torrents/.placements').mkdir(parents=True)
        (root / 'media').mkdir()
        pools[name] = replace(pool, root=root)
        devices[root] = os.makedev(8, index)
        (uuids / pool.filesystem_id).symlink_to(f'/dev/sd{index}')
        host_lines.append(f'{index} 0 8:{index} / {pool.host_mount} rw - ext4 /dev/sd{index} rw')
        subtree = '/' if name == 'ssd' else '/homeserver'
        local_lines.append(f'{index} 0 8:{index} {subtree} {root} rw - ext4 /dev/sd{index} rw')
    host, local = tmp_path / 'host', tmp_path / 'local'
    host.write_text('\n'.join(host_lines))
    local.write_text('\n'.join(local_lines))
    return StorageRegistry(
        pools, host_mountinfo=host, mountinfo=local, uuid_dir=uuids,
        placement_claims=tmp_path / 'claims', device_id=devices.__getitem__,
        usage=lambda _: SimpleNamespace(total=20_000, used=10_000, free=10_000),
        download_uid=os.getuid() + 1, download_gid=os.getgid() + 1,
        set_owner=lambda *_: None,
    )


def test_optional_third_pool_loads_in_stable_order_with_independent_paths(tmp_path):
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(document()))
    loaded = load_storage_registry(path)
    assert loaded.pool_ids == ('ssd', 'hdd', 'hdd2')
    assert loaded.pools['hdd2'].root == Path('/storage/hdd2/homeserver')
    assert loaded.pools['hdd2'].host_mount == Path('/srv/external2')
    assert loaded.pools['hdd2'].host_root == Path('/srv/external2/homeserver')
    assert len({pool.host_root for pool in loaded.pools.values()}) == 3


def test_third_pool_cannot_reuse_a_physical_uuid(tmp_path):
    value = document()
    value['pools'][0]['filesystem_id'] = 'hdd-uuid'
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(value))
    with pytest.raises(StorageRegistryError, match='distinct physical identities'):
        load_storage_registry(path)


def test_third_pool_cannot_hide_an_existing_counterpart(registry):
    counterpart = registry.pools['hdd2'].root / 'torrents/.placements/permit-1'
    counterpart.mkdir()
    with pytest.raises(RuntimeError, match='counterpart'):
        registry.prepare_destination('ssd', 'permit-1')
    assert not (registry.pools['ssd'].root / 'torrents/.placements/permit-1').exists()


def test_third_pool_placement_is_exclusive_and_claimed(registry):
    logical = registry.prepare_destination('hdd2', 'permit-2')
    assert registry.resolve('hdd2', logical).is_dir()
    assert not (registry.pools['ssd'].root / 'torrents/.placements/permit-2').exists()
    assert not (registry.pools['hdd'].root / 'torrents/.placements/permit-2').exists()
    with pytest.raises(RuntimeError, match='another physical pool|counterpart'):
        registry.prepare_destination('hdd', 'permit-2')


def issue(tmp_path, registry, free, budget, *, margin=100):
    database = tmp_path / 'control.sqlite'
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(
        request_id='seerr:third', source_id='1', media_key='movie:tmdb:1',
        filesystem_id='fixture', budget_bytes=1, free_bytes=10_000, total_bytes=20_000,
    )
    permits = PermitRegistry(database, storage_registry=registry)
    capacity = CapacityEvidence(free[0], {}, pools=tuple(
        PoolCapacityEvidence(name, name + '-uuid', size, min_free_bytes=margin)
        for name, size in zip(('ssd', 'hdd', 'hdd2'), free, strict=True)
    ))
    return permits.issue(
        infohash='a' * 40, destination='/data/torrents', category='radarr',
        reservation_id=reservation.reservation_id, budget_bytes=budget,
        capacity=capacity, expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.parametrize(('free', 'expected'), [
    ((1_000, 1_000, 1_000), 'ssd'),
    ((200, 1_000, 1_000), 'hdd'),
    ((200, 300, 1_000), 'hdd2'),
])
def test_admission_prefers_ssd_then_first_hd_then_second_hd(tmp_path, registry, free, expected):
    permit = issue(tmp_path, registry, free, 800)
    assert permit.pool_id == expected
    assert permit.filesystem_id == expected + '-uuid'


def test_three_small_pools_cannot_be_summed_to_admit_a_torrent(tmp_path, registry):
    with pytest.raises(PermissionError, match='waiting_space'):
        issue(tmp_path, registry, (400, 400, 400), 800)


def test_absent_second_hd_never_admits_into_its_fallback(tmp_path, registry):
    (registry.uuid_dir / 'hdd2-uuid').unlink()
    with pytest.raises(PermissionError, match='waiting_space'):
        issue(tmp_path, registry, (200, 300, 1_000), 800)
    assert not list((registry.pools['hdd2'].root / 'torrents/.placements').iterdir())


def test_third_pool_requires_a_real_ext4_filesystem(registry):
    table = registry.host_mountinfo
    table.write_text(table.read_text().replace('ext4 /dev/sd3', 'xfs /dev/sd3'))
    with pytest.raises(RuntimeError, match='physical filesystem root'):
        registry.inspect('hdd2')


def test_distinct_pool_ids_cannot_share_a_local_root(registry):
    pools = dict(registry.pools)
    pools['hdd2'] = replace(pools['hdd2'], root=pools['hdd'].root)
    with pytest.raises(StorageRegistryError, match='distinct physical roots'):
        StorageRegistry(pools)


@pytest.mark.parametrize('missing_branch', [False, True])
def test_startup_requires_every_registered_branch(tmp_path, monkeypatch, missing_branch):
    from homeserver_common import storage_startup
    from homeserver_common.storage import StorageUnavailable

    pool_roots = {'ssd': '/srv/data', 'hdd': '/srv/external/homeserver',
                  'hdd2': '/srv/external2/homeserver'}
    guarded = []
    registry = SimpleNamespace(
        primary_pool_id='ssd',
        pools={name: SimpleNamespace(host_root=Path(root)) for name, root in pool_roots.items()},
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
    roots = list(pool_roots.values())[:2 if missing_branch else 3]
    attrs = {'branches': ':'.join(root + '=RW' for root in roots),
             'category.create': 'epff', 'ignorepponrename': 'true', 'moveonenospc': 'false',
             'link_cow': 'false', 'symlinkify': 'false', 'cache.files': 'auto-full',
             'dropcacheonclose': 'true'}
    monkeypatch.setattr(storage_startup.os, 'getxattr',
                        lambda _, key: attrs[key.removeprefix('user.mergerfs.')].encode())
    if missing_branch:
        with pytest.raises(StorageUnavailable, match='branches'):
            storage_startup.verify_storage_startup(tmp_path / 'registry', tmp_path)
    else:
        assert storage_startup.verify_storage_startup(tmp_path / 'registry', tmp_path)
    assert guarded == [Path(root) for root in pool_roots.values()]


def test_queue_charges_second_hd_only_and_applies_its_own_margin(tmp_path, registry):
    path = tmp_path / 'capacity.json'
    path.write_text(json.dumps({'pools': [registry.inspect(name).as_dict()
                                        for name in registry.pool_ids]}))
    payload = [{'hash': 'b' * 40, 'pool_id': 'hdd2', 'filesystem_id': 'hdd2-uuid',
                'placement_verified': True, 'admitted': True, 'total_size': 1_000,
                'amount_left': 300, 'state': 'stoppedDL'}]
    evidence = capacity_from_queue(
        snapshot_path=path, data_root=tmp_path, payload=payload,
        storage_registry=registry, min_free_bytes=500,
    )
    assert evidence.pool('hdd2').remaining_by_hash == {'b' * 40: 300}
    assert evidence.pool('ssd').remaining_by_hash == {}
    assert evidence.pool('hdd').remaining_by_hash == {}
    assert evidence.pool('hdd2').usable_free_bytes == 9_500


def test_installer_publishes_all_three_verified_identities(tmp_path):
    module = installer()
    output = tmp_path / 'storage.json'
    module.publish_registry(output, {'ssd': 'a', 'hdd': 'b', 'hdd2': 'c'},
                            {'ssd': CAPS, 'hdd': CAPS, 'hdd2': CAPS})
    assert [pool['pool_id'] for pool in json.loads(output.read_text())['pools']] == [
        'ssd', 'hdd', 'hdd2']
    previous = output.read_bytes()
    with pytest.raises(ValueError, match='probe'):
        module.publish_registry(output, {'ssd': 'a', 'hdd': 'b', 'hdd2': 'c'},
                                {'ssd': CAPS, 'hdd': CAPS, 'hdd2': {}})
    assert output.read_bytes() == previous


def test_installer_check_validates_optional_second_hd_without_writes(monkeypatch, tmp_path):
    module = installer()
    checked, branches, guarded = [], [], []
    monkeypatch.setattr(module, 'verify_physical', lambda root, uuid: checked.append((root, uuid)))
    monkeypatch.setattr(module, 'verify_view', lambda view, roots: branches.extend(roots))
    monkeypatch.setattr(module, 'guard', guarded.append)
    output = tmp_path / 'never-published.json'
    assert module.main(['--ssd-uuid', 'a', '--hdd-uuid', 'b', '--hdd2-uuid', 'c',
                        '--check', '--output', str(output)]) == 0
    assert checked == [(Path('/srv/data'), 'a'), (Path('/srv/external'), 'b'),
                       (Path('/srv/external2'), 'c')]
    assert branches == [Path('/srv/data'), Path('/srv/external/homeserver'),
                        Path('/srv/external2/homeserver')]
    assert len(guarded) == 3
    assert not output.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason='requires real root to UID1000 fixture')
def test_installer_probes_affinity_against_every_other_pool():
    module = installer()
    with tempfile.TemporaryDirectory(prefix='homeserver-three-pool-', dir='/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        branches = [root / name for name in ('ssd', 'hdd', 'hdd2')]
        for branch in branches:
            (branch / 'torrents/.placements').mkdir(parents=True, mode=0o755)
            (branch / 'media').mkdir(mode=0o755)
        assert module.probe(branches[2], branches[:2], branches[2]) == CAPS
        assert all(not list((branch / 'torrents/.placements').iterdir()) for branch in branches)
        assert all(not list((branch / 'media').iterdir()) for branch in branches)


@pytest.mark.skipif(os.geteuid() != 0, reason='requires real root to UID1000 fixture')
def test_installer_refuses_an_unsafe_parent_in_the_last_counterpart():
    module = installer()
    with tempfile.TemporaryDirectory(prefix='homeserver-three-pool-', dir='/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        branches = [root / name for name in ('ssd', 'hdd', 'hdd2')]
        for branch in branches:
            (branch / 'torrents/.placements').mkdir(parents=True, mode=0o755)
            (branch / 'media').mkdir(mode=0o755)
        (branches[2] / 'torrents/.placements').chmod(0o777)
        with pytest.raises(ValueError, match='capability probe failed'):
            module.probe(branches[0], branches[1:], branches[0])
        assert all(not list((branch / 'torrents/.placements').iterdir()) for branch in branches)


def test_telemetry_keeps_second_hd_visible_and_independent(tmp_path):
    from homeserver_telemetry.status import StatusProvider

    # Constructor names are taken from the existing collector boundary.
    now = time.time()
    registry_path = tmp_path / 'storage.json'
    registry_path.write_text(json.dumps(document()))
    pools = [{'pool_id': name, 'filesystem_id': name + '-uuid', 'state': 'ready',
              'measured_at': now, 'total_bytes': 1_000, 'used_bytes': 300, 'free_bytes': 650}
             for name in ('ssd', 'hdd', 'hdd2')]
    collector = StatusProvider(
        media_root=tmp_path, host_path=tmp_path / 'host.json',
        capacity_path=tmp_path / 'capacity.json', storage_registry_path=registry_path,
    )
    actual = collector._pools({'pools': pools}, {'pools': pools}, now=now, host_state='ok')
    assert [pool['pool_id'] for pool in actual] == ['ssd', 'hdd', 'hdd2']
    assert actual[2]['state'] == 'ready'
    assert actual[2]['free_bytes'] == 650
