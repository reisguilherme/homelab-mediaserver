"""The focused hdd3 layout retains physical identity and placement protections."""
import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from test_storage_installation import installer

from homeserver_common import storage_startup
from homeserver_common.storage import (
    StorageRegistry,
    StorageRegistryError,
    StorageUnavailable,
    load_storage_registry,
)

CAPABILITIES = dict.fromkeys(
    ('hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement'), True,
)
OPTIONS = {
    'branches': '/srv/external3/homeserver=RW',
    'category.create': 'epff', 'ignorepponrename': 'true', 'moveonenospc': 'false',
    'link_cow': 'false', 'symlinkify': 'false', 'cache.files': 'auto-full',
    'dropcacheonclose': 'true',
}


def document(names=('hdd3',)):
    return {'version': 1, 'pools': [
        {'pool_id': name, 'filesystem_id': name + '-uuid', 'capabilities': CAPABILITIES}
        for name in names
    ]}


def load_document(tmp_path, value):
    path = tmp_path / 'storage.json'
    path.write_text(json.dumps(value))
    return load_storage_registry(path)


@pytest.fixture
def registry(tmp_path):
    loaded = load_document(tmp_path, document())
    root = tmp_path / 'hdd3'
    (root / 'torrents/.placements').mkdir(parents=True)
    (root / 'media').mkdir()
    pool = replace(loaded.pools['hdd3'], root=root)
    device = root.stat().st_dev
    major, minor = os.major(device), os.minor(device)
    host, local, uuids = tmp_path / 'host', tmp_path / 'local', tmp_path / 'uuids'
    uuids.mkdir()
    (uuids / 'hdd3-uuid').symlink_to('/dev/sdz1')
    host.write_text(f'1 0 {major}:{minor} / /srv/external3 rw - ext4 /dev/sdz1 rw\n')
    local.write_text(f'1 0 {major}:{minor} /homeserver {root} rw - ext4 /dev/sdz1 rw\n')
    return StorageRegistry(
        {'hdd3': pool}, host_mountinfo=host, mountinfo=local, uuid_dir=uuids,
        placement_claims=tmp_path / 'claims', download_uid=os.getuid() + 1,
        download_gid=os.getgid() + 1, set_owner=lambda *args: None,
    )


@pytest.mark.parametrize('names', [
    ('hdd3',), ('ssd', 'hdd'), ('ssd', 'hdd', 'hdd2'),
    ('ssd', 'hdd', 'hdd3'), ('ssd', 'hdd', 'hdd2', 'hdd3'),
])
def test_primary_identity_preserves_ssd_priority_and_singleton_paths(tmp_path, names):
    registry = load_document(tmp_path, document(tuple(reversed(names))))
    assert registry.pool_ids == names
    expected = 'hdd3' if names == ('hdd3',) else 'ssd'
    assert registry.primary_pool_id == expected
    assert registry.primary_pool is registry.pools[expected]
    if names == ('hdd3',):
        assert registry.primary_pool.root == Path('/storage/hdd3/homeserver')
        assert registry.primary_pool.host_mount == Path('/srv/external3')
        assert registry.primary_pool.host_root == Path('/srv/external3/homeserver')


@pytest.mark.parametrize('names', [
    (), ('ssd',), ('hdd',), ('hdd2',), ('ssd', 'hdd3'),
    ('hdd', 'hdd3'), ('hdd2', 'hdd3'), ('ssd', 'hdd2', 'hdd3'),
])
def test_singleton_does_not_permit_other_partial_layouts(tmp_path, names):
    with pytest.raises(StorageRegistryError):
        load_document(tmp_path, document(names))


@pytest.mark.parametrize('change', ['uuid', 'unknown-capability', 'capability-type', 'extra-field'])
def test_singleton_keeps_strict_registry_validation(tmp_path, change):
    value = document()
    entry = value['pools'][0]
    if change == 'uuid':
        entry['filesystem_id'] = '../hdd3'
    elif change == 'unknown-capability':
        entry['capabilities'] = {**CAPABILITIES, 'unsafe': True}
    elif change == 'capability-type':
        entry['capabilities'] = {**CAPABILITIES, 'hardlink': 1}
    else:
        entry['root'] = '/arbitrary'
    with pytest.raises(StorageRegistryError):
        load_document(tmp_path, value)


def test_singleton_prepares_claim_only_on_registered_pool(registry):
    logical = registry.prepare_destination(registry.primary_pool_id, 'singleton-permit')
    assert logical == '/data/torrents/.placements/singleton-permit'
    assert registry.resolve('hdd3', logical).is_dir()
    assert json.loads((registry.placement_claims / 'singleton-permit.json').read_text()) == {
        'pool_id': 'hdd3', 'filesystem_id': 'hdd3-uuid',
    }
    assert registry.validate_destination('hdd3', 'singleton-permit') == logical


@pytest.mark.parametrize('change', [
    'missing', 'foreign-uuid', 'host-ro', 'bind-ro', 'wrong-subtree', 'capabilities', 'guard',
])
def test_singleton_cannot_write_to_unavailable_or_unsafe_pool(registry, change):
    if change == 'missing':
        registry.host_mountinfo.write_text('')
    elif change == 'foreign-uuid':
        link = registry.uuid_dir / 'hdd3-uuid'
        link.unlink()
        link.symlink_to('/dev/foreign')
    elif change == 'host-ro':
        registry.host_mountinfo.write_text(
            registry.host_mountinfo.read_text().replace(' rw ', ' ro '))
    elif change == 'bind-ro':
        registry.mountinfo.write_text(registry.mountinfo.read_text().replace(' rw ', ' ro '))
    elif change == 'wrong-subtree':
        registry.mountinfo.write_text(
            registry.mountinfo.read_text().replace('/homeserver ', '/old '))
    elif change == 'capabilities':
        registry.pools['hdd3'] = replace(registry.primary_pool, capabilities={})
    else:
        (registry.primary_pool.root / 'torrents/.placements').chmod(0o777)
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('hdd3', 'must-not-exist')
    assert not (registry.primary_pool.root / 'torrents/.placements/must-not-exist').exists()


def startup_fixture(registry, tmp_path, monkeypatch, attrs):
    logical = tmp_path / 'logical'
    logical.mkdir()
    device = logical.stat().st_dev
    entry = f'2 0 {os.major(device)}:{os.minor(device)} / '
    with registry.host_mountinfo.open('a') as stream:
        stream.write(entry + '/srv/media-view rw - fuse.mergerfs hdd3 rw\n')
    with registry.mountinfo.open('a') as stream:
        stream.write(entry + f'{logical} rw - fuse.mergerfs hdd3 rw\n')
    monkeypatch.setattr(storage_startup, 'load_storage_registry', lambda *args: registry)
    monkeypatch.setattr(storage_startup.os, 'getxattr',
                        lambda path, key: attrs[key.removeprefix('user.mergerfs.')].encode())
    return logical


def test_singleton_startup_needs_only_its_registered_primary(registry, tmp_path, monkeypatch):
    logical = startup_fixture(registry, tmp_path, monkeypatch, OPTIONS)
    assert storage_startup.verify_storage_startup(tmp_path / 'storage.json', logical)


@pytest.mark.parametrize('branch', [
    '/srv/data=RW:/srv/external3/homeserver=RW', '/srv/external3/homeserver=RO',
    '/srv/external3/homeserver=RW:/srv/external/homeserver=RW', '',
])
def test_singleton_startup_rejects_old_extra_or_readonly_union_branches(
    registry, tmp_path, monkeypatch, branch,
):
    logical = startup_fixture(registry, tmp_path, monkeypatch, {**OPTIONS, 'branches': branch})
    with pytest.raises(StorageUnavailable, match='branches'):
        storage_startup.verify_storage_startup(tmp_path / 'storage.json', logical)


@pytest.mark.parametrize('change', ['missing', 'host-ro', 'foreign', 'guard'])
def test_singleton_startup_requires_current_primary_and_guard(
    registry, tmp_path, monkeypatch, change,
):
    logical = startup_fixture(registry, tmp_path, monkeypatch, OPTIONS)
    if change == 'missing':
        registry.host_mountinfo.write_text(
            registry.host_mountinfo.read_text().splitlines()[1] + '\n')
    elif change == 'host-ro':
        registry.host_mountinfo.write_text(registry.host_mountinfo.read_text().replace(
            '/srv/external3 rw', '/srv/external3 ro'))
    elif change == 'foreign':
        (registry.uuid_dir / 'hdd3-uuid').unlink()
    else:
        (registry.primary_pool.root / 'torrents/.placements').chmod(0o777)
    with pytest.raises(StorageUnavailable):
        storage_startup.verify_storage_startup(tmp_path / 'storage.json', logical)


def test_installer_singleton_check_uses_only_target_without_writes(tmp_path, monkeypatch):
    module = installer()
    checked, branches, guarded = [], [], []
    monkeypatch.setattr(module, 'verify_physical', lambda root, uuid: checked.append((root, uuid)))
    monkeypatch.setattr(module, 'verify_view', lambda view, roots: branches.extend(roots))
    monkeypatch.setattr(module, 'guard', guarded.append)
    monkeypatch.setattr(module, 'project_directories',
                        lambda *args: pytest.fail('check wrote folders'))
    output = tmp_path / 'not-written.json'
    assert module.main(['--hdd3-uuid', 'target-uuid', '--check', '--output', str(output)]) == 0
    assert checked == [(Path('/srv/external3'), 'target-uuid')]
    assert branches == [Path('/srv/external3/homeserver')]
    assert guarded == [Path('/srv/external3/homeserver/torrents/.placements')]
    assert not output.exists()
    assert not (tmp_path / 'storage-native.identity').exists()


def test_installer_singleton_probes_then_rechecks_before_publishing(tmp_path, monkeypatch):
    module = installer()
    operations = []
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module, 'verify_physical',
                        lambda root, uuid: operations.append(('physical', root, uuid)))
    monkeypatch.setattr(module, 'verify_view',
                        lambda view, roots: operations.append(('view', view, roots)))
    monkeypatch.setattr(module, 'project_directories',
                        lambda root: operations.append(('directories', root)))

    def probe(root, others, view):
        operations.append(('probe', root, others, view))
        return CAPABILITIES

    monkeypatch.setattr(module, 'probe', probe)
    output = tmp_path / 'storage.json'
    assert module.main(['--hdd3-uuid', 'target-uuid', '--output', str(output)]) == 0
    assert operations == [
        ('physical', Path('/srv/external3'), 'target-uuid'),
        ('view', Path('/srv/media-view'), (Path('/srv/external3/homeserver'),)),
        ('directories', Path('/srv/external3/homeserver')),
        ('probe', Path('/srv/external3/homeserver'), [], Path('/srv/media-view')),
        ('physical', Path('/srv/external3'), 'target-uuid'),
        ('view', Path('/srv/media-view'), (Path('/srv/external3/homeserver'),)),
    ]
    loaded = load_storage_registry(output)
    assert loaded.primary_pool_id == 'hdd3'
    assert loaded.primary_pool.filesystem_id == 'target-uuid'
    assert output.stat().st_mode & 0o777 == 0o600
    native = tmp_path / 'storage-native.identity'
    assert native.read_text() == 'homeserver-single-media-v1 hdd3 target-uuid\n'
    assert native.stat().st_mode & 0o777 == 0o644
    assert (native.stat().st_uid, native.stat().st_gid) == (1000, 1000)


def test_failed_singleton_recheck_does_not_publish_native_attestation(tmp_path, monkeypatch):
    module = installer()
    checks = 0

    def verify_physical(root, uuid):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise ValueError('mount changed during probe')

    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module, 'verify_physical', verify_physical)
    monkeypatch.setattr(module, 'verify_view', lambda *args: None)
    monkeypatch.setattr(module, 'project_directories', lambda *args: None)
    monkeypatch.setattr(module, 'probe', lambda *args: CAPABILITIES)
    output = tmp_path / 'storage.pending.json'
    assert module.main(['--hdd3-uuid', 'target-uuid', '--output', str(output)]) == 2
    assert not output.exists()
    assert not (tmp_path / 'storage-native.identity').exists()


def test_legacy_preparation_does_not_publish_singleton_attestation(tmp_path, monkeypatch):
    module = installer()
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(module, 'verify_physical', lambda *args: None)
    monkeypatch.setattr(module, 'verify_view', lambda *args: None)
    monkeypatch.setattr(module, 'project_directories', lambda *args: None)
    monkeypatch.setattr(module, 'probe', lambda *args: CAPABILITIES)
    output = tmp_path / 'storage.pending.json'
    assert module.main(['--ssd-uuid', 'first', '--hdd-uuid', 'second',
                        '--output', str(output)]) == 0
    assert load_storage_registry(output).pool_ids == ('ssd', 'hdd')
    assert not (tmp_path / 'storage-native.identity').exists()


@pytest.mark.parametrize('arguments', [
    [], ['--ssd-uuid', 'a'], ['--hdd-uuid', 'a'], ['--hdd2-uuid', 'a'],
    ['--ssd-uuid', 'a', '--hdd3-uuid', 'b'], ['--hdd-uuid', 'a', '--hdd3-uuid', 'b'],
    ['--hdd2-uuid', 'a', '--hdd3-uuid', 'b'], ['--hdd3-uuid', '../bad'],
])
def test_installer_rejects_partial_layouts_before_mount_checks(monkeypatch, arguments):
    module = installer()
    monkeypatch.setattr(module, 'verify_physical',
                        lambda *args: pytest.fail('inspected invalid layout'))
    assert module.main(arguments + ['--check']) == 2


@pytest.mark.skipif(os.geteuid() != 0, reason='requires real root to UID1000 fixture')
def test_singleton_probe_checks_real_uid_mmap_links_rename_and_guard_and_cleans(monkeypatch):
    module = installer()
    with tempfile.TemporaryDirectory(prefix='homeserver-single-pool-', dir='/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        pool = root / 'hdd3'
        (pool / 'torrents/.placements').mkdir(parents=True, mode=0o755)
        (pool / 'media').mkdir(mode=0o755)
        sentinel = pool / 'media/keep'
        sentinel.write_bytes(b'existing fixture media')
        observed = root / 'observed'
        observed.touch(mode=0o600)
        os.chown(observed, 1000, 1000)
        original = module.probe_mmap

        def probe_mmap(path):
            observed.write_text(str(os.geteuid()))
            return original(path)

        monkeypatch.setattr(module, 'probe_mmap', probe_mmap)
        assert module.probe(pool, [], pool) == CAPABILITIES
        assert observed.read_text() == '1000'
        assert list((pool / 'torrents/.placements').iterdir()) == []
        assert list((pool / 'media').iterdir()) == [sentinel]
        assert sentinel.read_bytes() == b'existing fixture media'
