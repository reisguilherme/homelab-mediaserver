import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def installer():
    path = Path(__file__).resolve().parents[2] / 'scripts/install-usb-recovery.py'
    if not path.exists():
        pytest.fail('USB recovery installer does not exist')
    spec = importlib.util.spec_from_file_location('usb_recovery_install', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def observed():
    capabilities = dict.fromkeys(
        ['hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement'], True,
    )
    registry = {'version': 1, 'pools': [
        {'pool_id': name, 'filesystem_id': f'{name}-fixture', 'capabilities': capabilities}
        for name in ['ssd', 'hdd', 'hdd2']
    ]}
    devices = [
        {'path': '/dev/nvme8n1', 'type': 'disk', 'fstype': 'ext4',
         'uuid': 'ssd-fixture', 'serial': 'ssd-serial', 'size': 123000, 'ro': False},
        {'path': '/dev/sdf', 'type': 'disk', 'serial': 'hd1-bridge', 'size': 456000,
         'ro': False},
        {'path': '/dev/sdf3', 'type': 'part', 'fstype': 'ext4', 'uuid': 'hdd-fixture',
         'size': 450000, 'ro': False, 'pkname': 'sdf', 'partition_number': 3},
        {'path': '/dev/sdx', 'type': 'disk', 'serial': 'hd2-bridge', 'size': 789000,
         'ro': False},
        {'path': '/dev/sdx1', 'type': 'part', 'fstype': 'ext4', 'uuid': 'hdd2-fixture',
         'size': 780000, 'ro': False, 'pkname': 'sdx', 'partition_number': 1},
    ]
    return registry, {'blockdevices': devices}


def test_config_pins_both_usb_partitions_without_pinning_kernel_names(installer, observed):
    config = installer.build_config(Path('/home/owner/HomeServer'), *observed)
    pools = {p['pool_id']: p for p in config['pools']}
    assert pools['hdd']['serial'] == 'hd1-bridge'
    assert pools['hdd']['partition_number'] == 3
    assert pools['hdd']['size_bytes'] == 450000
    assert pools['hdd2']['partition_number'] == 1
    assert pools['hdd2']['disk_size_bytes'] == 789000
    assert '/dev/sdf' not in json.dumps(config)


def test_config_optionally_pins_third_usb_and_rejects_wrong_partition(installer, observed):
    registry, blocks = observed
    registry['pools'].append(dict(registry['pools'][-1], pool_id='hdd3',
                                  filesystem_id='hdd3-fixture'))
    blocks['blockdevices'].extend([
        {'path': '/dev/sdy', 'type': 'disk', 'serial': 'hd3-bridge',
         'size': 18000000, 'ro': False},
        {'path': '/dev/sdy1', 'type': 'part', 'fstype': 'ext4', 'uuid': 'hdd3-fixture',
         'size': 17990000, 'ro': False, 'pkname': 'sdy', 'partition_number': 1},
    ])
    result = installer.build_config(Path('/home/owner/HomeServer'), registry, blocks)
    assert result['version'] == 1
    assert result['pools'][-1] == dict(pool_id='hdd3', filesystem_id='hdd3-fixture',
                                      serial='hd3-bridge', size_bytes=17990000,
                                      disk_size_bytes=18000000, partition_number=1)
    blocks['blockdevices'][-1]['partition_number'] = 2
    with pytest.raises(ValueError, match='partition'):
        installer.build_config(Path('/home/owner/HomeServer'), registry, blocks)


def test_clone_with_same_uuid_is_not_adopted(installer, observed):
    registry, blocks = observed
    blocks['blockdevices'].append(dict(blocks['blockdevices'][-1], path='/dev/sdy1'))
    with pytest.raises(ValueError, match='identity'):
        installer.build_config(Path('/home/owner/HomeServer'), registry, blocks)


def test_wrong_usb_partition_is_rejected(installer, observed):
    registry, blocks = observed
    blocks['blockdevices'][-1]['partition_number'] = 2
    with pytest.raises(ValueError, match='partition'):
        installer.build_config(Path('/home/owner/HomeServer'), registry, blocks)


def test_install_uses_existing_cron_and_preserves_state_and_other_host_files(
    installer, observed, tmp_path,
):
    config = installer.build_config(Path('/home/owner/HomeServer'), *observed)
    (tmp_path / 'etc').mkdir()
    fstab = tmp_path / 'etc/fstab'
    fstab.write_text('existing mounts\n')
    units = tmp_path / 'etc/systemd/system'
    units.mkdir(parents=True)
    sentinel = units / 'lenovo.service'
    sentinel.write_text('existing Lenovo\n')
    state_dir = tmp_path / 'var/lib/homeserver-usb-recovery'
    state_dir.mkdir(parents=True)
    state = state_dir / 'state.json'
    state.write_text('{"phase":"waiting"}')
    installer.write_installation(tmp_path, config, {'usb-recovery.py': b'print("ok")\n'})
    installer.write_installation(tmp_path, config, {'usb-recovery.py': b'print("ok")\n'})
    assert fstab.read_text() == 'existing mounts\n'
    assert sentinel.read_text() == 'existing Lenovo\n'
    assert list(units.iterdir()) == [sentinel]
    assert state.read_text() == '{"phase":"waiting"}'
    cron = (tmp_path / 'etc/cron.d/homeserver-usb-recovery').read_text()
    assert cron.count('* * * * * root') == 1
    assert 'MAILTO=""' in cron
    assert '--once' in cron
    assert (tmp_path / 'etc/homeserver-usb-recovery.json').stat().st_mode & 0o777 == 0o600


def test_failed_reinstall_remains_disabled_and_preserves_manual_disable(
    installer, observed, tmp_path,
):
    config = installer.build_config(Path('/home/owner/HomeServer'), *observed)
    installer.write_installation(tmp_path, config, {'usb-recovery.py': b'print("ok")\n'})
    state = tmp_path / 'var/lib/homeserver-usb-recovery'
    with pytest.raises(SyntaxError):
        installer.write_installation(tmp_path, config, {'usb-recovery.py': b'if:\n'})
    assert (state / 'disabled').exists()
    installer.write_installation(tmp_path, config, {'usb-recovery.py': b'print("ok")\n'})
    assert (state / 'disabled').exists()


def test_shared_host_directory_modes_are_preserved(installer, observed, tmp_path):
    config = installer.build_config(Path('/home/owner/HomeServer'), *observed)
    cron = tmp_path / 'etc/cron.d'
    cron.mkdir(parents=True, mode=0o750)
    installer.write_installation(tmp_path, config, {'usb-recovery.py': b''})
    assert cron.stat().st_mode & 0o777 == 0o750


def test_install_refuses_symlink_in_privileged_destination(installer, observed, tmp_path):
    config = installer.build_config(Path('/home/owner/HomeServer'), *observed)
    (tmp_path / 'etc').mkdir()
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (tmp_path / 'etc/cron.d').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        installer.write_installation(tmp_path, config, {'usb-recovery.py': b''})
    assert not list(outside.iterdir())
