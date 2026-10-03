import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from homeserver_common.storage import (
    StorageRegistry,
    StorageRegistryError,
    StorageUnavailable,
    load_storage_registry,
)

CAPABILITIES = dict.fromkeys(
    ('hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement'), True
)


def document():
    return {'version': 1, 'pools': [
        {'pool_id': name, 'filesystem_id': f'{name}-uuid', 'capabilities': CAPABILITIES}
        for name in ('ssd', 'hdd')
    ]}


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(document()))
    loaded = load_storage_registry(path)
    pools = {}
    host_lines, local_lines = [], []
    uuids = tmp_path / 'uuids'
    uuids.mkdir()
    for index, (name, pool) in enumerate(loaded.pools.items(), 1):
        root = tmp_path / name
        (root / 'torrents' / '.placements').mkdir(parents=True)
        (root / 'media').mkdir()
        pools[name] = replace(pool, root=root)
        (uuids / pool.filesystem_id).symlink_to(f'../../sd{index}')
        host_lines.append(f'{index} 0 8:{index} / {pool.host_mount} rw - ext4 /dev/sd{index} rw')
        subtree = '/' if name == 'ssd' else '/homeserver'
        local_lines.append(f'{index} 0 8:{index} {subtree} {root} rw - ext4 /dev/sd{index} rw')
    host = tmp_path / 'host-mountinfo'
    local = tmp_path / 'mountinfo'
    host.write_text('\n'.join(host_lines))
    local.write_text('\n'.join(local_lines))
    return StorageRegistry(
        pools, host_mountinfo=host, mountinfo=local, uuid_dir=uuids,
        placement_claims=tmp_path / 'claims',
        device_id=lambda path: os.makedev(8, 1 if path == pools['ssd'].root else 2),
        usage=lambda path: SimpleNamespace(total=1000, used=300, free=650),
        # Tests run as an unprivileged user; service UID is distinct from fixture owner.
        download_uid=os.getuid() + 1, download_gid=os.getgid() + 1,
        set_owner=lambda path, uid, gid: None,
    )


def test_missing_registry_preserves_legacy(tmp_path):
    assert load_storage_registry(tmp_path / 'absent') is None


def test_broken_registry_symlink_is_not_legacy(tmp_path):
    path = tmp_path / 'storage.json'
    path.symlink_to(tmp_path / 'missing')
    with pytest.raises(StorageRegistryError):
        load_storage_registry(path)


@pytest.mark.parametrize('value', [{}, {'version': 2}, {'version': 1, 'pools': []}, 'bad'])
def test_bad_registry_fails_closed(tmp_path, value):
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(value))
    with pytest.raises(StorageRegistryError):
        load_storage_registry(path)


def test_inspection_reports_physical_available_bytes(registry):
    sample = registry.inspect('hdd', writable=True)
    assert (sample.filesystem_id, sample.total_bytes, sample.used_bytes, sample.free_bytes) == (
        'hdd-uuid', 1000, 300, 650)
    assert sample.as_dict()['state'] == 'ready'


@pytest.mark.parametrize('change', ['absent', 'ro', 'subdirectory', 'wrong-device'])
def test_host_mount_evidence_cannot_be_replaced_by_directory(registry, change):
    text = registry.host_mountinfo.read_text()
    if change == 'absent':
        text = text.splitlines()[0]
    elif change == 'ro':
        text = text.replace('/srv/external rw', '/srv/external ro')
    elif change == 'subdirectory':
        text = text.replace('8:2 / ', '8:2 /ordinary ')
    else:
        text = text.replace('8:2', '8:9')
    registry.host_mountinfo.write_text(text)
    with pytest.raises(StorageUnavailable):
        registry.inspect('hdd')


def test_wrong_uuid_is_unavailable(registry):
    link = registry.uuid_dir / 'hdd-uuid'
    link.unlink()
    link.symlink_to('../../wrong')
    with pytest.raises(StorageUnavailable):
        registry.inspect('hdd')


def test_readonly_collector_can_read_but_not_admit(registry):
    registry.mountinfo.write_text(registry.mountinfo.read_text().replace(' rw ', ' ro '))
    assert registry.inspect('hdd').free_bytes == 650
    with pytest.raises(StorageUnavailable):
        registry.inspect('hdd', writable=True)


def test_unproven_pool_cannot_admit(registry):
    registry.pools['hdd'] = replace(registry.pools['hdd'], capabilities={})
    with pytest.raises(StorageUnavailable):
        registry.inspect('hdd', writable=True)


def test_shadow_mount_cannot_redirect_resolution(registry):
    root = registry.pools['ssd'].root
    with registry.mountinfo.open('a') as stream:
        stream.write(f'\n3 1 8:9 / {root}/media rw - ext4 /dev/wrong rw')
    with pytest.raises(StorageUnavailable):
        registry.resolve('ssd', '/data/media/movies/file.mkv')


def test_wrong_bind_subtree_is_unavailable(registry):
    registry.mountinfo.write_text(registry.mountinfo.read_text().replace('/homeserver ', '/wrong '))
    with pytest.raises(StorageUnavailable):
        registry.inspect('hdd')


@pytest.mark.parametrize('path', [
    '/data/../etc/passwd', '/data/torrents/../../etc/passwd', '/etc/passwd',
    'torrents/a', '/data/torrents/a/../b', '/data/torrents\\escape', '/data/control/a',
])
def test_unsafe_paths_are_rejected(registry, path):
    with pytest.raises(StorageUnavailable):
        registry.resolve('ssd', path)


def test_symlink_escape_is_rejected(registry, tmp_path):
    (registry.pools['ssd'].root / 'torrents' / 'escape').symlink_to(tmp_path)
    with pytest.raises(StorageUnavailable):
        registry.resolve('ssd', '/data/torrents/escape/anything')


def test_safe_nonexistent_descendant_resolves(registry):
    assert registry.resolve('hdd', '/data/media/tv/new.mkv') == (
        registry.pools['hdd'].root / 'media/tv/new.mkv')


def test_exclusive_destination_is_created_only_on_selected_pool(registry):
    logical = registry.prepare_destination('hdd', 'permit-1')
    assert logical == '/data/torrents/.placements/permit-1'
    assert registry.resolve('hdd', logical).is_dir()
    assert not registry.resolve('ssd', logical).exists()
    assert registry.prepare_destination('hdd', 'permit-1') == logical
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('ssd', 'permit-1')


def test_validation_uses_readonly_bind_and_requires_persisted_claim(registry):
    logical = registry.prepare_destination('hdd', 'permit-1')
    registry.mountinfo.write_text(registry.mountinfo.read_text().replace(' rw ', ' ro '))
    assert registry.validate_destination('hdd', 'permit-1') == logical
    (registry.placement_claims / 'permit-1.json').unlink()
    with pytest.raises(StorageUnavailable):
        registry.validate_destination('hdd', 'permit-1')


def test_claim_is_readable_by_unprivileged_gateway_but_not_writable(registry):
    registry.prepare_destination('hdd', 'permit-1')
    claim = registry.placement_claims / 'permit-1.json'
    assert registry.placement_claims.stat().st_mode & 0o777 == 0o755
    assert claim.stat().st_mode & 0o777 == 0o644


def test_unclaimed_preexisting_destination_is_not_adopted(registry):
    (registry.pools['hdd'].root / 'torrents/.placements/permit-1').mkdir()
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('hdd', 'permit-1')


def test_claim_prevents_recreation_when_original_pool_disappears(registry):
    registry.prepare_destination('hdd', 'permit-1')
    registry.host_mountinfo.write_text(registry.host_mountinfo.read_text().splitlines()[0])
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('ssd', 'permit-1')
    assert not (registry.pools['ssd'].root / 'torrents/.placements/permit-1').exists()
    assert registry.prepare_destination('ssd', 'permit-2').endswith('/permit-2')


def test_existing_counterpart_blocks_new_destination(registry):
    (registry.pools['ssd'].root / 'torrents/.placements/permit-1').mkdir()
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('hdd', 'permit-1')


def test_writable_parent_on_other_pool_blocks_destination(registry):
    (registry.pools['ssd'].root / 'torrents/.placements').chmod(0o777)
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('hdd', 'permit-1')


def test_prepare_does_not_create_missing_parent(registry):
    (registry.pools['hdd'].root / 'torrents/.placements').rmdir()
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('hdd', 'permit-1')


@pytest.mark.parametrize('permit_id', ['../escape', '', 'a/b', 'a\\b', '.', '..'])
def test_invalid_permit_id_never_creates_paths(registry, permit_id):
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('ssd', permit_id)
