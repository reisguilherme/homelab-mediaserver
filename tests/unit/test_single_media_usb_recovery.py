"""A registered hdd3 alone recovers without an SSD or old union branches."""

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_usb_recovery as legacy
import test_usb_recovery_install as legacy_install

import homeserver_common.usb_recovery as recovery
from homeserver_common.storage import _parse_mounts


@pytest.fixture
def installer():
    return legacy_install.installer.__wrapped__()


def config():
    value = legacy.config()
    value['pools'] = [dict(
        pool_id='hdd3', filesystem_id='large-fixture-uuid', serial='large-fixture-bridge',
        size_bytes=17_999_000_000_000, disk_size_bytes=18_000_000_000_000,
        partition_number=1,
    )]
    return value


def observed():
    capabilities = dict.fromkeys(
        ('hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement'), True,
    )
    registry = {'version': 1, 'pools': [dict(
        pool_id='hdd3', filesystem_id='large-fixture-uuid', capabilities=capabilities,
    )]}
    blocks = {'blockdevices': [
        dict(path='/dev/sdy', type='disk', serial='large-fixture-bridge',
             size=18_000_000_000_000, ro=False),
        dict(path='/dev/sdy1', type='part', fstype='ext4', uuid='large-fixture-uuid',
             size=17_999_000_000_000, ro=False, pkname='sdy', partition_number=1),
    ]}
    return registry, blocks


def setup(tmp_path):
    controller, backend, now, state_path = legacy.setup(tmp_path)
    backend.pools = {'hdd3': dict(status='ready', generation='large-one', device='/dev/sdy1')}
    return controller, backend, now, state_path


def test_singleton_config_preserves_registered_partition_identity():
    value = config()
    assert recovery.validate_config(value) is value
    backend = recovery.HostBackend(value)
    assert backend.pool_ids == ('hdd3',)
    assert backend.usb_pool_ids == ('hdd3',)


@pytest.mark.parametrize('name', ['ssd', 'hdd', 'hdd2'])
def test_other_singleton_recovery_layouts_stay_rejected(name):
    value = config()
    value['pools'][0].update(pool_id=name, partition_number=recovery.PARTITIONS[name])
    with pytest.raises(ValueError):
        recovery.validate_config(value)


@pytest.mark.parametrize(('field', 'value'), [
    ('partition_number', 2), ('filesystem_id', '../foreign'), ('serial', ''),
    ('size_bytes', 0), ('disk_size_bytes', 0),
])
def test_singleton_config_rejects_unproven_identity(field, value):
    document = config()
    document['pools'][0][field] = value
    with pytest.raises(ValueError):
        recovery.validate_config(document)


def test_singleton_healthy_tick_has_no_recovery_effects(tmp_path):
    controller, backend, _, path = setup(tmp_path)
    assert controller.tick()['phase'] == 'ready'
    assert backend.effects == []
    assert json.loads(path.read_text())['healthy']['generations'] == {'hdd3': 'large-one'}


def test_singleton_coldboot_restores_cached_active_ids_without_ssd(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    assert controller.tick()['phase'] == 'ready'
    backend.boot_id = 'boot-two'
    backend.snapshot_unavailable = True
    for item in backend.containers.values():
        item.update(running=False, boot_restart=True)
    backend.pools['hdd3']['status'] = 'absent'
    assert controller.tick()['phase'] == 'waiting'
    backend.pools['hdd3']['status'] = 'stale'
    assert legacy.recover(controller, now)['phase'] == 'complete'
    assert backend.containers['qbittorrent']['running']
    assert backend.containers['actor']['running']
    assert not backend.containers['jellyfin']['running']


@pytest.mark.parametrize('status', ['absent', 'invalid'])
def test_singleton_unavailable_device_holds_all_consumers(tmp_path, status):
    controller, backend, now, _ = setup(tmp_path)
    backend.pools['hdd3']['status'] = status
    result = legacy.recover(controller, now, limit=4)
    assert result['reason'] == 'devices-unavailable'
    assert 'mount' not in backend.effects
    assert not any(effect.startswith('start:') for effect in backend.effects)
    assert not backend.containers['actor']['running']


def test_singleton_reconnect_generation_recovers_once(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    assert controller.tick()['phase'] == 'ready'
    backend.pools['hdd3']['generation'] = 'large-two'
    assert legacy.recover(controller, now)['phase'] == 'complete'
    assert backend.effects.count('mount') == 1
    effects = list(backend.effects)
    assert controller.tick()['phase'] == 'ready'
    assert backend.effects == effects


@pytest.mark.parametrize(('mutation', 'status'), [
    ('missing', 'absent'), ('uuid', 'absent'), ('serial', 'invalid'),
    ('size', 'invalid'), ('readonly', 'invalid'), ('partition', 'invalid'),
])
def test_singleton_device_identity_requires_real_partition(tmp_path, mutation, status):
    _, blocks = observed()
    disk, partition = blocks['blockdevices']
    sys = tmp_path / 'sys'
    (sys / 'sdy1').mkdir(parents=True)
    (sys / 'sdy1/partition').write_text('2' if mutation == 'partition' else '1')
    if mutation == 'missing':
        blocks['blockdevices'] = []
    elif mutation == 'uuid':
        partition['uuid'] = 'foreign'
    elif mutation == 'serial':
        disk['serial'] = 'foreign'
    elif mutation == 'size':
        partition['size'] -= 1
    elif mutation == 'readonly':
        partition['ro'] = True
    result = recovery.identify_devices(blocks, config()['pools'], sys)
    assert result['hdd3']['status'] == status


def fstab(branch='/srv/external3/homeserver'):
    return ('UUID=large-fixture-uuid /srv/external3 ext4 defaults 0 2\n'
            f'{branch} /srv/media-view fuse.mergerfs defaults 0 0\n')


@pytest.mark.parametrize('branch', [
    '/srv/data:/srv/external3/homeserver', '/srv/external3/homeserver:/srv/external/homeserver',
    '/srv/external3/homeserver=RO', '/srv/external3/homeserver:/srv/external3/homeserver',
])
def test_singleton_fstab_rejects_any_unregistered_union_branch(branch):
    with pytest.raises(recovery.RecoveryHold, match='fstab-union'):
        recovery.attest_fstab(fstab(branch), config()['pools'])


def test_singleton_fstab_accepts_only_registered_partition_and_branch():
    result = recovery.attest_fstab(fstab(), config()['pools'])
    assert set(result) == {'hdd3', 'union'}
    assert result['union']['source'] == '/srv/external3/homeserver'
    with pytest.raises(recovery.RecoveryHold, match='fstab-identity'):
        recovery.attest_fstab(fstab().replace('large-fixture-uuid', 'foreign'), config()['pools'])


def mounted_backend(monkeypatch):
    backend = recovery.HostBackend(config())
    mounted = {'hdd3': False, 'union': False}
    effects = []
    generations = ['large-one']
    roots = {
        'srv-external3.mount': ('/srv/external3', 'UUID=large-fixture-uuid', 'ext4'),
        'srv-media\\x2dview.mount': (
            '/srv/media-view', '/srv/external3/homeserver', 'fuse.mergerfs'),
    }

    def devices():
        return {'hdd3': dict(status='ready' if mounted['hdd3'] else 'stale',
                            generation=generations[0], device='/dev/sdy1')}

    def mounts(path):
        rows = []
        if mounted['hdd3']:
            rows.append('10 1 8:81 / /srv/external3 rw - ext4 /dev/sdy1 rw')
        if mounted['union']:
            rows.append('11 1 0:77 / /srv/media-view rw - fuse.mergerfs mergerfs rw')
        return _parse_mounts('\n'.join(rows))

    def trusted(path):
        if path == Path('/etc/fstab'):
            return fstab()
        target, source, filesystem = roots[path.name]
        return f'What={source}\nWhere={target}\nType={filesystem}\n'

    def command(argv, **kwargs):
        if argv[:2] == ['systemctl', 'show']:
            target, source, filesystem = roots[argv[2]]
            active = mounted['hdd3' if argv[2] == 'srv-external3.mount' else 'union']
            return (f'FragmentPath=/run/systemd/generator/{argv[2]}\nSourcePath=/etc/fstab\n'
                    f'DropInPaths=\nWhere={target}\nType={filesystem}\nWhat={source}\n'
                    f'ActiveState={"active" if active else "inactive"}\n')
        effects.append(argv)
        if argv[0] == 'systemctl':
            name = 'hdd3' if argv[2] == 'srv-external3.mount' else 'union'
            mounted[name] = argv[1] == 'start'
        return ''

    original_stat = Path.stat

    def path_stat(path, *args, **kwargs):
        if path == Path('/srv/media-view'):
            return SimpleNamespace(st_dev=os.makedev(0, 77))
        return original_stat(path, *args, **kwargs)

    attributes = {
        'branches': '/srv/external3/homeserver=RW', 'category.create': 'epff',
        'ignorepponrename': 'true', 'moveonenospc': 'false', 'link_cow': 'false',
        'symlinkify': 'false', 'cache.files': 'auto-full', 'dropcacheonclose': 'true',
    }
    monkeypatch.setattr(backend, '_devices', devices)
    monkeypatch.setattr(backend, '_current', lambda _: {})
    monkeypatch.setattr(backend, '_run', command)
    monkeypatch.setattr(recovery, '_mounts', mounts)
    monkeypatch.setattr(recovery, '_trusted_text', trusted)
    monkeypatch.setattr(Path, 'stat', path_stat)
    monkeypatch.setattr(recovery.os, 'getxattr',
                        lambda path, key: attributes[key.removeprefix('user.mergerfs.')].encode())
    return backend, mounted, effects, generations, attributes


def test_singleton_restore_only_controls_existing_registered_fstab_units(monkeypatch):
    backend, mounted, effects, _, _ = mounted_backend(monkeypatch)
    backend.restore_mounts({'desired': {}, 'affected': ['hdd3']})
    assert mounted == {'hdd3': True, 'union': True}
    assert effects == [
        ['systemctl', 'stop', 'srv-media\\x2dview.mount'],
        ['systemctl', 'stop', 'srv-external3.mount'],
        ['systemctl', 'start', 'srv-external3.mount'],
        ['systemctl', 'start', 'srv-media\\x2dview.mount'],
        ['python3', '/usr/local/lib/homeserver-usb-recovery/prepare_storage.py',
         '--check', '--hdd3-uuid', 'large-fixture-uuid'],
    ]


def test_singleton_restore_rechecks_generation_after_preparation(monkeypatch):
    backend, _, effects, generations, _ = mounted_backend(monkeypatch)
    original = backend._run

    def command(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0] == 'python3':
            generations[0] = 'reconnected-during-check'
        return result

    monkeypatch.setattr(backend, '_run', command)
    with pytest.raises(recovery.RecoveryHold, match='device-generation-changed'):
        backend.restore_mounts({'desired': {}, 'affected': ['hdd3']})
    assert effects[-1][-2:] == ['--hdd3-uuid', 'large-fixture-uuid']


def test_singleton_union_rejects_retired_runtime_branch(monkeypatch):
    backend, mounted, _, _, attributes = mounted_backend(monkeypatch)
    mounted['union'] = True
    assert backend._union_ok()
    attributes['branches'] = '/srv/data=RW:/srv/external3/homeserver=RW'
    assert not backend._union_ok()


def test_singleton_installer_pins_only_registered_18tb_partition(installer):
    result = installer.build_config(Path('/home/owner/HomeServer'), *observed())
    assert result['pools'] == config()['pools']
    assert '/dev/sdy' not in json.dumps(result)


@pytest.mark.parametrize('mutation', ['missing', 'partition', 'readonly', 'clone'])
def test_singleton_installer_rejects_unsuitable_target(installer, mutation):
    registry, blocks = observed()
    if mutation == 'missing':
        blocks['blockdevices'].pop()
    elif mutation == 'partition':
        blocks['blockdevices'][-1]['partition_number'] = 2
    elif mutation == 'readonly':
        blocks['blockdevices'][-1]['ro'] = True
    elif mutation == 'clone':
        blocks['blockdevices'].append(copy.deepcopy(blocks['blockdevices'][-1]))
    with pytest.raises(ValueError):
        installer.build_config(Path('/home/owner/HomeServer'), registry, blocks)


def test_singleton_install_uses_configured_margin_and_reports_one_usb(
    installer, monkeypatch, tmp_path, capsys,
):
    project = tmp_path / 'HomeServer'
    project.mkdir()
    (project / '.env').write_text('HOMESERVER_STORAGE_MIN_FREE_GIB="27"\n')
    registry, blocks = observed()
    original_read = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == Path('/srv/appdata/control/storage.json'):
            return json.dumps(registry)
        if path == Path('/sys/class/block/sdy1/partition'):
            return '1'
        return original_read(path, *args, **kwargs)

    def run(argv, **kwargs):
        if argv[0] == 'systemctl':
            assert argv[1:] in [
                ['is-active', '--quiet', 'cron.service'],
                ['is-enabled', '--quiet', 'cron.service'],
            ]
        else:
            assert argv[0] == 'lsblk'
        return SimpleNamespace(stdout=json.dumps(blocks))

    installation = tmp_path / 'installation'
    publish = installer.write_installation
    monkeypatch.setattr(installer.sys, 'argv', [
        'install-usb-recovery.py', '--install', '--project', str(project),
    ])
    monkeypatch.setattr(installer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(installer.os, 'readlink', lambda _: 'fixture-mount-namespace')
    monkeypatch.setattr(Path, 'read_text', read_text)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    monkeypatch.setattr(recovery, 'HostBackend', lambda _: SimpleNamespace(observe=lambda: {
        'pools': {'hdd3': {'status': 'ready'}}, 'union_ok': True,
    }))
    monkeypatch.setattr(installer, 'collect_bundle', lambda _: {'usb-recovery.py': b'pass\n'})
    monkeypatch.setattr(installer, 'write_installation',
                        lambda prefix, value, bundle: publish(installation, value, bundle))
    installer.main()
    installed = json.loads((installation / 'etc/homeserver-usb-recovery.json').read_text())
    assert installed['minimum_free_bytes'] == 27 * 1024**3
    assert json.loads(capsys.readouterr().out) == {
        'installed': True, 'interval_seconds': 60, 'usb_pools': 1,
    }
