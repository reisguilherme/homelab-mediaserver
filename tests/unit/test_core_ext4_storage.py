"""The core opts into one direct ext4 volume without weakening mergerfs."""
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from homeserver_common import storage_startup
from homeserver_common.storage import (
    StorageRegistry,
    StorageRegistryError,
    StorageUnavailable,
    load_storage_registry,
)
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.storage_paths import captured_identity, physical_path, physical_paths
from homeserver_control.worker.capacity_evidence import capacity_from_queue

UUID = '11111111-2222-3333-4444-555555555555'
CAPABILITIES = {
    'hardlink': True, 'rename': True, 'unlink': True,
    'ownership': True, 'exclusive_placement': True,
}


def document():
    return {'version': 2, 'backend': 'ext4-single', 'pools': [
        {'pool_id': 'hdd3', 'filesystem_id': UUID, 'capabilities': dict(CAPABILITIES)},
    ]}


def install(tmp_path, value):
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(value))
    return path


def test_explicit_ext4_maps_only_the_registered_hdd3_subtree(tmp_path):
    registry = load_storage_registry(install(tmp_path, document()))
    assert registry.backend == 'ext4-single'
    assert registry.pool_ids == ('hdd3',)
    assert registry.primary_pool.root == Path('/data')
    assert registry.primary_pool.host_mount == Path('/srv/external3')
    assert registry.primary_pool.host_root == Path('/srv/external3/homeserver')
    assert registry.primary_pool.filesystem_id == UUID


@pytest.mark.parametrize('change', [
    'backend-missing', 'backend-unknown', 'v1-backend', 'version-bool', 'version-float',
    'extra-root', 'extra-pool', 'other-pool', 'empty-pools', 'duplicate-pool',
    'invalid-uuid', 'caps-type', 'caps-unknown',
])
def test_ext4_registry_refuses_implicit_or_broader_layouts(tmp_path, change):
    value = document()
    entry = value['pools'][0]
    if change == 'backend-missing':
        value.pop('backend')
    elif change == 'backend-unknown':
        value['backend'] = 'auto'
    elif change == 'v1-backend':
        value['version'] = 1
    elif change == 'version-bool':
        value['version'] = True
    elif change == 'version-float':
        value['version'] = 2.0
    elif change == 'extra-root':
        value['root'] = '/arbitrary'
    elif change == 'extra-pool':
        entry['root'] = '/arbitrary'
    elif change == 'other-pool':
        entry['pool_id'] = 'ssd'
    elif change == 'empty-pools':
        value['pools'] = []
    elif change == 'duplicate-pool':
        value['pools'] *= 2
    elif change == 'invalid-uuid':
        entry['filesystem_id'] = 'hdd3-uuid'
    elif change == 'caps-type':
        entry['capabilities']['hardlink'] = 1
    else:
        entry['capabilities']['extra'] = True
    with pytest.raises(StorageRegistryError):
        load_storage_registry(install(tmp_path, value))


def test_core_can_require_ext4_without_missing_registry_fallback(tmp_path):
    missing = tmp_path / 'absent'
    assert load_storage_registry(missing) is None
    with pytest.raises(StorageRegistryError):
        load_storage_registry(missing, required_backend='ext4-single')
    value = document()
    value.pop('backend')
    value['version'] = 1
    path = install(tmp_path, value)
    legacy = load_storage_registry(path)
    assert legacy.backend == 'mergerfs'
    assert legacy.primary_pool.root == Path('/storage/hdd3/homeserver')
    with pytest.raises(StorageRegistryError):
        load_storage_registry(path, required_backend='ext4-single')
    path = install(tmp_path, document())
    assert load_storage_registry(path, required_backend='ext4-single').pool_ids == ('hdd3',)


def test_ext4_does_not_invent_capability_proof(tmp_path):
    value = document()
    value['pools'][0]['capabilities'] = {}
    registry = load_storage_registry(install(tmp_path, value))
    assert registry.primary_pool.capabilities == {}


@pytest.fixture
def direct(tmp_path):
    loaded = load_storage_registry(install(tmp_path, document()))
    root = tmp_path / 'data'
    (root / 'torrents/.placements').mkdir(parents=True)
    (root / 'media').mkdir()
    pool = replace(loaded.primary_pool, root=root)
    device = root.stat().st_dev
    dev = f'{os.major(device)}:{os.minor(device)}'
    host, local, uuids = tmp_path / 'host', tmp_path / 'local', tmp_path / 'uuids'
    uuids.mkdir()
    (uuids / UUID).symlink_to('/dev/sdz1')
    host.write_text(
        f'1 0 {os.major(device) + 1}:0 / / rw - ext4 /dev/root rw\n'
        f'2 1 {dev} / /srv/external3 rw,nosuid,nodev,noexec - ext4 /dev/sdz1 rw\n')
    local.write_text(
        f'3 0 {dev} /homeserver {root} rw,nosuid,nodev,noexec - ext4 /dev/sdz1 rw\n')
    return StorageRegistry(
        {'hdd3': pool}, backend=loaded.backend, host_mountinfo=host, mountinfo=local,
        uuid_dir=uuids, placement_claims=tmp_path / 'claims',
        download_uid=os.getuid() + 1, download_gid=os.getgid() + 1,
        set_owner=lambda *args: None,
    )


def test_direct_inspection_uses_real_file_device_and_preserves_ro_gateway(direct):
    sample = direct.inspect('hdd3', writable=True)
    assert sample.filesystem_id == UUID and sample.free_bytes > 0
    direct.mountinfo.write_text(direct.mountinfo.read_text().replace('rw,nosuid', 'ro,nosuid'))
    assert direct.inspect('hdd3').filesystem_id == UUID
    with pytest.raises(StorageUnavailable):
        direct.inspect('hdd3', writable=True)


@pytest.mark.parametrize('change', [
    'host-missing', 'host-duplicate', 'uuid-missing', 'uuid-foreign', 'host-ro',
    'wrong-root', 'wrong-subtree', 'wrong-filesystem', 'fallback-directory',
    'local-stacked', 'local-child', 'host-child', 'root-device', 'root-missing',
    'unsafe-host', 'unsafe-local', 'device-mismatch',
])
def test_direct_refuses_mount_evidence_that_could_redirect_io(direct, change):
    host, local = direct.host_mountinfo.read_text(), direct.mountinfo.read_text()
    if change == 'host-missing':
        host = host.splitlines()[0] + '\n'
    elif change == 'host-duplicate':
        host += host.splitlines()[1] + '\n'
    elif change.startswith('uuid-'):
        link = direct.uuid_dir / UUID
        link.unlink()
        if change == 'uuid-foreign':
            link.symlink_to('/dev/other')
    elif change == 'host-ro':
        host = host.replace('/srv/external3 rw,', '/srv/external3 ro,')
    elif change == 'wrong-root':
        host = host.replace(' / /srv/external3 ', ' /old /srv/external3 ')
    elif change == 'wrong-subtree':
        local = local.replace('/homeserver ', '/other ')
    elif change == 'wrong-filesystem':
        host = host.replace('ext4 /dev/sdz1', 'fuse.mergerfs /dev/sdz1')
    elif change == 'fallback-directory':
        local = local.replace(f'/homeserver {direct.primary_pool.root}',
                              f'/ {direct.primary_pool.root.parent}')
    elif change == 'local-stacked':
        local += local
    elif change == 'local-child':
        local += local.replace(str(direct.primary_pool.root),
                               str(direct.primary_pool.root / 'media/hidden'))
    elif change == 'host-child':
        host += host.splitlines()[1].replace('/srv/external3 ', '/srv/external3/hidden ') + '\n'
    elif change == 'root-device':
        fields = host.splitlines()[0].split()
        fields[2] = host.splitlines()[1].split()[2]
        host = ' '.join(fields) + '\n' + host.splitlines()[1] + '\n'
    elif change == 'root-missing':
        host = host.splitlines()[1] + '\n'
    elif change == 'unsafe-host':
        host = host.replace(',nodev', '')
    elif change == 'unsafe-local':
        local = local.replace(',noexec', '')
    elif change == 'device-mismatch':
        direct._device_id = lambda _: os.makedev(250, 250)
    direct.host_mountinfo.write_text(host)
    direct.mountinfo.write_text(local)
    with pytest.raises(StorageUnavailable):
        direct.inspect('hdd3')


def test_direct_startup_needs_no_union_and_checks_requested_writability(
    direct, tmp_path, monkeypatch,
):
    monkeypatch.setattr(storage_startup, 'load_storage_registry', lambda *a, **kw: direct)
    assert storage_startup.verify_storage_startup(tmp_path / 'storage.json',
                                                 direct.primary_pool.root, writable=True)
    direct.mountinfo.write_text(direct.mountinfo.read_text().replace('rw,nosuid', 'ro,nosuid'))
    assert storage_startup.verify_storage_startup(tmp_path / 'storage.json',
                                                 direct.primary_pool.root)
    with pytest.raises(StorageUnavailable):
        storage_startup.verify_storage_startup(tmp_path / 'storage.json',
                                               direct.primary_pool.root, writable=True)


@pytest.mark.parametrize('change', ['logical-alias', 'guard-writable', 'capabilities'])
def test_direct_startup_does_not_bypass_placement_protection(
    direct, tmp_path, monkeypatch, change,
):
    monkeypatch.setattr(storage_startup, 'load_storage_registry', lambda *a, **kw: direct)
    logical = direct.primary_pool.root
    if change == 'logical-alias':
        logical = tmp_path / 'alias'
        logical.symlink_to(direct.primary_pool.root)
    elif change == 'guard-writable':
        (logical / 'torrents/.placements').chmod(0o777)
    else:
        direct.pools['hdd3'] = replace(direct.primary_pool, capabilities={})
    with pytest.raises(StorageUnavailable):
        storage_startup.verify_storage_startup(tmp_path / 'storage.json', logical, writable=True)


def test_startup_can_require_direct_registry_before_any_application(tmp_path):
    with pytest.raises(StorageRegistryError):
        storage_startup.verify_storage_startup(tmp_path / 'missing', Path('/data'),
                                               required_backend='ext4-single')


def test_direct_paths_capture_real_hardlink_identity_without_xattrs(direct):
    video = direct.primary_pool.root / 'media/sample.mkv'
    video.write_bytes(b'fixture video')
    source = direct.primary_pool.root / 'torrents/source.mkv'
    os.link(video, source)
    assert physical_path(direct, '/data/media/sample.mkv') == ('hdd3', video)
    assert physical_paths(direct, '/data/media') == [('hdd3', video.parent)]
    identity = captured_identity(direct, '/data/media/sample.mkv', union_path=video)
    original = captured_identity(direct, '/data/torrents/source.mkv', union_path=source)
    assert identity == original
    assert identity == {
        'pool_id': 'hdd3', 'filesystem_id': UUID, 'device': video.stat().st_dev,
        'inode': video.stat().st_ino, 'size': 13, 'mtime_ns': video.stat().st_mtime_ns, 'nlink': 2,
    }


def test_mergerfs_backend_never_uses_direct_fallback(direct, tmp_path, monkeypatch):
    video = direct.primary_pool.root / 'media/sample'
    video.write_bytes(b'fixture')
    direct.backend = 'mergerfs'
    with pytest.raises(StorageUnavailable, match='mergerfs'):
        physical_path(direct, '/data/media/sample', union_path=video)
    monkeypatch.setattr(storage_startup, 'load_storage_registry', lambda *a: direct)
    with pytest.raises(StorageUnavailable, match='mergerfs'):
        storage_startup.verify_storage_startup(tmp_path / 'storage.json', direct.primary_pool.root)


@pytest.mark.parametrize('path', [
    '/data/media/../torrents/sample', '/data/media/./sample', '/data/media//sample',
    '/data/media/sample/', '/data/media/escape', '/data/media/missing',
    '/data/media/special', '/data/other/sample', '/outside/sample',
    '/data/media/sample\x00', '/data/media\\sample',
])
def test_direct_paths_refuse_unsafe_absent_and_special_objects(direct, path):
    root = direct.primary_pool.root
    (root / 'media/sample').write_bytes(b'fixture')
    (root / 'media/escape').symlink_to(root / 'media/sample')
    os.mkfifo(root / 'media/special')
    with pytest.raises(StorageUnavailable):
        physical_path(direct, path)


def test_direct_path_argument_cannot_redirect_to_another_alias(direct):
    video = direct.primary_pool.root / 'media/sample'
    video.write_bytes(b'fixture')
    other = direct.primary_pool.root / 'media/other'
    os.link(video, other)
    with pytest.raises(StorageUnavailable):
        physical_path(direct, '/data/media/sample', union_path=other)
    assert physical_path(direct, '/data/media/sample', union_path=video) == ('hdd3', video)


def test_direct_path_writes_require_current_writable_evidence(direct):
    video = direct.primary_pool.root / 'media/sample'
    video.write_bytes(b'fixture')
    direct.mountinfo.write_text(direct.mountinfo.read_text().replace('rw,nosuid', 'ro,nosuid'))
    assert physical_path(direct, '/data/media/sample')[1] == video
    with pytest.raises(StorageUnavailable):
        physical_path(direct, '/data/media/sample', writable=True)


def test_direct_read_snapshot_rejects_replaced_inode(direct):
    video = direct.primary_pool.root / 'media/sample'
    video.write_bytes(b'fixture')
    replacement = video.with_name('replacement')
    replacement.write_bytes(b'fixture')
    with pytest.raises(StorageUnavailable):
        with direct.read_snapshot() as snapshot:
            physical_path(snapshot, '/data/media/sample')
            replacement.replace(video)


def capacity(direct, tmp_path):
    path = tmp_path / 'capacity.json'
    sample = direct.inspect('hdd3')
    path.write_text(json.dumps({'pools': [sample.as_dict()]}))
    return capacity_from_queue(snapshot_path=path, data_root=direct.primary_pool.root,
                               payload=[], storage_registry=direct, min_free_bytes=1024)


def test_direct_worker_places_new_permit_but_ro_gateway_cannot_prepare(direct, tmp_path):
    database = tmp_path / 'control.sqlite'
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(request_id='fixture', source_id='fixture', media_key='fixture',
                               filesystem_id=UUID, budget_bytes=1,
                               free_bytes=10000, total_bytes=20000)
    worker = PermitRegistry(database, storage_registry=direct)
    permit = worker.issue(
        infohash='a' * 40, destination='/data/torrents', category='fixture', budget_bytes=128,
        reservation_id=reservation.reservation_id, capacity=capacity(direct, tmp_path),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert permit.pool_id == 'hdd3' and permit.filesystem_id == UUID
    assert permit.destination == f'/data/torrents/.placements/{permit.permit_id}'
    claim = direct.placement_claims / f'{permit.permit_id}.json'
    assert json.loads(claim.read_text()) == {'pool_id': 'hdd3', 'filesystem_id': UUID}
    direct.mountinfo.write_text(direct.mountinfo.read_text().replace('rw,nosuid', 'ro,nosuid'))
    gateway = PermitRegistry(database, storage_registry=direct)
    assert gateway.placement_valid(gateway.get(permit.token))
    assert not gateway.placement_valid(replace(permit, pool_id='ssd', destination='/data/torrents'))
    with pytest.raises(StorageUnavailable):
        direct.prepare_destination('hdd3', 'gateway-must-not-create')
    assert not (direct.primary_pool.root / 'torrents/.placements/gateway-must-not-create').exists()
    claim.write_text(json.dumps({'pool_id': 'ssd', 'filesystem_id': 'old-ssd-uuid'}))
    assert not gateway.placement_valid(permit)


@pytest.mark.parametrize('change', ['missing', 'old-uuid', 'stale', 'unattributed-queue'])
def test_direct_capacity_refuses_stale_or_unattributed_evidence(direct, tmp_path, change):
    sample = direct.inspect('hdd3').as_dict()
    entries = []
    path = tmp_path / 'capacity.json'
    if change == 'old-uuid':
        sample['filesystem_id'] = 'old-ssd-uuid'
    elif change == 'stale':
        sample['measured_at'] = 0
    elif change == 'unattributed-queue':
        entries = [{'hash': 'a' * 40, 'pool_id': 'ssd'}]
    if change != 'missing':
        path.write_text(json.dumps({'pools': [sample]}))
    with pytest.raises((ValueError, FileNotFoundError)):
        capacity_from_queue(snapshot_path=path, data_root=direct.primary_pool.root,
                            payload=entries, storage_registry=direct)


@pytest.mark.parametrize('change', ['missing-claim', 'unsafe-parent', 'unproven-capabilities'])
def test_direct_placement_never_adopts_unproved_existing_destination(direct, change):
    destination = direct.primary_pool.root / 'torrents/.placements/existing'
    if change == 'missing-claim':
        destination.mkdir()
    elif change == 'unsafe-parent':
        destination.parent.chmod(0o777)
    else:
        direct.pools['hdd3'] = replace(direct.primary_pool, capabilities={})
    existed = destination.exists()
    with pytest.raises(StorageUnavailable):
        direct.prepare_destination('hdd3', 'existing')
    assert destination.exists() is existed
    assert not (direct.placement_claims / 'existing.json').exists()
