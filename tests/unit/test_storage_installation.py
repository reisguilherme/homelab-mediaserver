from __future__ import annotations

import errno
import importlib.util
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def installer():
    path = Path(__file__).resolve().parents[2] / 'scripts/prepare-storage.py'
    spec = importlib.util.spec_from_file_location('prepare_storage', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ordinary_directory_is_never_mount_evidence(tmp_path):
    module = installer()
    table = tmp_path / 'mountinfo'
    table.write_text('1 0 8:1 / / rw - ext4 /dev/sda1 rw\n')
    with pytest.raises(ValueError, match='physical mount'):
        module.verify_physical(tmp_path, 'expected', mountinfo=table, uuid_dir=tmp_path)


def test_wrong_uuid_refuses_before_creating_project_directories(tmp_path):
    module = installer()
    table = tmp_path / 'mountinfo'
    major, minor = module.os.major(tmp_path.stat().st_dev), module.os.minor(tmp_path.stat().st_dev)
    table.write_text(f'1 0 {major}:{minor} / {tmp_path} rw - ext4 /dev/sda1 rw\n')
    (tmp_path / 'expected').symlink_to('/dev/sdb1')
    with pytest.raises(ValueError, match='UUID'):
        module.verify_physical(tmp_path, 'expected', mountinfo=table, uuid_dir=tmp_path)
    assert not (tmp_path / 'torrents').exists()


def test_registry_publish_is_private_atomic_and_contains_only_proven_capabilities(tmp_path):
    module = installer()
    output = tmp_path / 'storage.pending.json'
    caps = dict.fromkeys(module.CAPABILITIES, True)
    module.publish_registry(output, {'ssd': 'first', 'hdd': 'second'}, {'ssd': caps, 'hdd': caps})
    content = json.loads(output.read_text())
    assert content['version'] == 1
    assert {p['filesystem_id'] for p in content['pools']} == {'first', 'second'}
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='probe'):
        module.publish_registry(output, {'ssd': 'first', 'hdd': 'second'}, {'ssd': caps, 'hdd': {}})
    assert json.loads(output.read_text()) == content


def test_symlink_output_parent_is_refused(tmp_path):
    module = installer()
    (tmp_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
    caps = dict.fromkeys(module.CAPABILITIES, True)
    with pytest.raises(ValueError, match='symlink'):
        module.publish_registry(tmp_path / 'alias/storage.json', {'ssd': 'a', 'hdd': 'b'},
                                {'ssd': caps, 'hdd': caps})


def test_view_must_be_actual_mergerfs_with_expected_branches(tmp_path):
    module = installer()
    table = tmp_path / 'mountinfo'
    table.write_text(f'1 0 8:1 / {tmp_path} rw - ext4 /dev/sda1 rw\n')
    with pytest.raises(ValueError, match='mergerfs'):
        module.verify_view(tmp_path, (Path('/srv/data'), Path('/srv/external/homeserver')),
                           mountinfo=table)


def test_registered_init_refuses_missing_view_before_mkdir(tmp_path, monkeypatch):
    from homeserver_common import host
    from homeserver_common.env import load_settings

    env = tmp_path / '.env'
    env.write_text(f'HOMESERVER_RUN_ROOT={tmp_path}/run\n'
                   f'HOMESERVER_APPDATA_ROOT={tmp_path}/appdata\n'
                   f'HOMESERVER_MEDIA_ROOT={tmp_path}/media\n'
                   f'HOMESERVER_TRANSCODE_ROOT={tmp_path}/transcode\n')
    caps = dict.fromkeys(installer().CAPABILITIES, True)
    (tmp_path / 'run').mkdir()
    installer().publish_registry(tmp_path / 'run/storage.json', {'ssd': 'a', 'hdd': 'b'},
                                 {'ssd': caps, 'hdd': caps})
    monkeypatch.setattr(host, 'preflight', lambda *a, **kw: [])
    with pytest.raises(RuntimeError):
        host.prepare_runtime(load_settings(env, mode='dev'))
    assert not (tmp_path / 'appdata').exists()
    assert not (tmp_path / 'media').exists()


def test_worker_startup_wrapper_refuses_before_exec(monkeypatch):
    from homeserver_common import storage_startup
    from homeserver_common.storage import StorageUnavailable

    def unavailable(*args):
        raise StorageUnavailable('view absent')

    monkeypatch.setattr(storage_startup, 'verify_storage_startup', unavailable)
    monkeypatch.setattr(storage_startup.os, 'execvp', lambda *a: pytest.fail('must not exec'))
    assert storage_startup.main(['python', '-m', 'homeserver_common.runtime']) == 2


def test_startup_refuses_hdd_eio_instead_of_skipping_guard(monkeypatch, tmp_path):
    from homeserver_common import storage_startup
    from homeserver_common.storage import StorageUnavailable

    def guard(pool):
        if pool == 'hdd':
            raise OSError(errno.EIO, 'stale USB bind')

    registry = SimpleNamespace(pools={'ssd': 'ssd', 'hdd': 'hdd'},
                               inspect=lambda *args: None, _guard_parent=guard)
    monkeypatch.setattr(storage_startup, 'load_storage_registry', lambda *args: registry)
    with pytest.raises(StorageUnavailable, match='startup evidence unavailable'):
        storage_startup.verify_storage_startup(tmp_path / 'registry', tmp_path)


def test_mmap_probe_performs_real_shared_write_flush_and_readback(tmp_path, monkeypatch):
    module = installer()
    path = tmp_path / 'payload'
    path.write_bytes(bytes(4096))
    calls = []
    real_mmap = module.mmap.mmap

    def observed_mmap(*args, **kwargs):
        calls.append(kwargs.get('access'))
        return real_mmap(*args, **kwargs)

    monkeypatch.setattr(module.mmap, 'mmap', observed_mmap)
    module.probe_mmap(path)
    assert calls == [module.mmap.ACCESS_WRITE]
    assert path.read_bytes().startswith(b'HomeServer mmap fixture\n')


@pytest.mark.skipif(os.geteuid() != 0, reason='requires real root to UID1000 fixture')
def test_installer_refuses_enodev_mmap_and_cleans_only_fixture(monkeypatch):
    module = installer()

    def unavailable(*args, **kwargs):
        identity_file.write_text(str(os.geteuid()))
        raise OSError(errno.ENODEV, 'mmap unavailable on uncached FUSE')

    monkeypatch.setattr(module.mmap, 'mmap', unavailable)
    with tempfile.TemporaryDirectory(prefix='homeserver-mmap-', dir='/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        identity_file = root / 'observed-uid'
        identity_file.touch(mode=0o600)
        os.chown(identity_file, 1000, 1000)
        pool, other = root / 'ssd', root / 'hdd'
        for branch in (pool, other):
            (branch / 'torrents/.placements').mkdir(parents=True, mode=0o755)
            (branch / 'media').mkdir(mode=0o755)
        with pytest.raises(ValueError, match='UID 1000.*probe failed'):
            module.probe(pool, other, pool)
        assert list((pool / 'torrents/.placements').iterdir()) == []
        assert list((pool / 'media').iterdir()) == []
        assert not (root / 'storage.json').exists()
        assert identity_file.read_text() == '1000'


@pytest.mark.parametrize('cache', ['off', 'auto-full'])
def test_installer_view_requires_mmap_compatible_cache(tmp_path, monkeypatch, cache):
    module = installer()
    table = tmp_path / 'mountinfo'
    device = tmp_path.stat().st_dev
    table.write_text(f'1 0 {os.major(device)}:{os.minor(device)} / {tmp_path} rw '
                     '- fuse.mergerfs data:external/homeserver rw\n')
    attrs = {'branches': '/srv/data=RW:/srv/external/homeserver=RW',
             'category.create': 'epff', 'ignorepponrename': 'true', 'moveonenospc': 'false',
             'link_cow': 'false', 'symlinkify': 'false', 'cache.files': cache,
             'dropcacheonclose': 'true'}
    monkeypatch.setattr(module.os, 'getxattr',
                        lambda path, key: attrs[key.removeprefix('user.mergerfs.')].encode())
    branches = (Path('/srv/data'), Path('/srv/external/homeserver'))
    if cache == 'off':
        with pytest.raises(ValueError, match='cache.files'):
            module.verify_view(tmp_path, branches, mountinfo=table)
    else:
        module.verify_view(tmp_path, branches, mountinfo=table)


@pytest.mark.parametrize('cache', ['off', 'auto-full'])
def test_worker_startup_refuses_cache_off(tmp_path, monkeypatch, cache):
    from homeserver_common import storage_startup
    from homeserver_common.storage import StorageUnavailable

    registry = SimpleNamespace(pools={}, inspect=lambda *a: None,
                               host_mountinfo=tmp_path / 'host', mountinfo=tmp_path / 'local')
    device = tmp_path.stat().st_dev
    host = SimpleNamespace(target=Path('/srv/media-view'), filesystem='fuse.mergerfs',
                           root=Path('/'), device=device)
    local = SimpleNamespace(target=tmp_path, filesystem='fuse.mergerfs',
                            root=Path('/'), device=device)
    monkeypatch.setattr(storage_startup, 'load_storage_registry', lambda *a: registry)
    monkeypatch.setattr(storage_startup, '_mounts',
                        lambda path: [host] if path == registry.host_mountinfo else [local])
    attrs = {'branches': '/srv/data=RW:/srv/external/homeserver=RW',
             'category.create': 'epff', 'ignorepponrename': 'true', 'moveonenospc': 'false',
             'link_cow': 'false', 'symlinkify': 'false', 'cache.files': cache,
             'dropcacheonclose': 'true'}
    monkeypatch.setattr(storage_startup.os, 'getxattr',
                        lambda path, key: attrs[key.removeprefix('user.mergerfs.')].encode())
    if cache == 'off':
        with pytest.raises(StorageUnavailable, match='cache.files'):
            storage_startup.verify_storage_startup(tmp_path / 'registry', tmp_path)
    else:
        assert storage_startup.verify_storage_startup(tmp_path / 'registry', tmp_path)
