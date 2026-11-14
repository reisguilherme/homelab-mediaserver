#!/usr/bin/env python3
"""Opt-in installation of scoped USB recovery into the existing host cron."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

INSTALLED = '/usr/local/lib/homeserver-usb-recovery'
LAUNCHER = '/usr/local/sbin/homeserver-usb-recovery'
CAPABILITIES = ('hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement')


def build_config(project, registry, blocks):
    project = Path(project)
    if not project.is_absolute() or '..' in project.parts:
        raise ValueError('project path must be absolute')
    if registry.get('version') != 1:
        raise ValueError('registry version')
    registered = registry.get('pools', [])
    names = {p.get('pool_id') for p in registered}
    if (names not in ({'hdd3'}, {'ssd', 'hdd', 'hdd2'}, {'ssd', 'hdd', 'hdd2', 'hdd3'})
            or len(registered) != len(names)):
        raise ValueError('registry pool identity')
    devices = blocks.get('blockdevices', [])
    pools = []
    for entry in registered:
        pool_id, identity = entry['pool_id'], entry.get('filesystem_id')
        if not identity or not all(entry.get('capabilities', {}).get(k) is True
                                   for k in CAPABILITIES):
            raise ValueError('registered identity or capability')
        matches = [d for d in devices if d.get('uuid') == identity]
        if len(matches) != 1:
            raise ValueError('filesystem identity absent or ambiguous')
        device = matches[0]
        number = {'ssd': 0, 'hdd': 3, 'hdd2': 1, 'hdd3': 1}[pool_id]
        if number:
            if device.get('type') != 'part' or device.get('partition_number') != number:
                raise ValueError('partition scope mismatch')
            parent = device.get('pkname')
            parents = [d for d in devices if Path(d.get('path', '')).name == parent
                       and d.get('type') == 'disk']
            if len(parents) != 1:
                raise ValueError('parent disk identity absent or ambiguous')
            disk = parents[0]
        else:
            if device.get('type') != 'disk':
                raise ValueError('SSD identity must be whole filesystem')
            disk = device
        if (device.get('fstype') != 'ext4' or device.get('ro') or disk.get('ro')
                or not isinstance(disk.get('serial'), str) or not disk['serial'].strip()
                or type(device.get('size')) is not int or device['size'] <= 0
                or type(disk.get('size')) is not int or disk['size'] < device['size']):
            raise ValueError('device identity or filesystem unsuitable')
        pools.append({'pool_id': pool_id, 'filesystem_id': identity,
                      'serial': disk['serial'].strip(), 'size_bytes': device['size'],
                      'disk_size_bytes': disk['size'], 'partition_number': number})
    if len({p['filesystem_id'] for p in pools}) != len(pools):
        raise ValueError('duplicate filesystem identity')
    return {'version': 1, 'project_dir': str(project), 'compose_project': 'homeserver',
            'pools': pools, 'actor_name': 'homeserver-new-batch-io',
            'minimum_free_bytes': 10 * 1024**3, 'installed_dir': INSTALLED}


def no_symlinks(path):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('symlink in privileged destination')


def _directory(path, mode):
    no_symlinks(path)
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if not path.is_dir():
        raise ValueError('privileged destination is not a directory')


def _write(path, content, mode):
    no_symlinks(path)
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError('privileged destination is not regular')
    descriptor, temporary = tempfile.mkstemp(prefix='.usb-recovery-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _publish(prefix, config, bundle):
    """Publish cron last. An offline prefix exists only for filesystem fixtures."""
    prefix = Path(prefix)
    if not prefix.is_absolute():
        raise ValueError('installation prefix must be absolute')
    targets = [prefix / 'etc/cron.d', prefix / INSTALLED.lstrip('/'),
               prefix / 'usr/local/sbin', prefix / 'var/lib/homeserver-usb-recovery']
    for target in targets:
        no_symlinks(target)
    for relative, content in bundle.items():
        name = Path(relative)
        if name.is_absolute() or '..' in name.parts or not isinstance(content, bytes):
            raise ValueError('unsafe bundle path')
        no_symlinks(targets[1] / name)
    _directory(targets[0], 0o755)
    _directory(targets[1], 0o755)
    _directory(targets[2], 0o755)
    _directory(targets[3], 0o700)
    targets[3].chmod(0o700)
    for relative, content in bundle.items():
        target = targets[1] / relative
        _directory(target.parent, 0o755)
        _write(target, content, 0o644)
    launcher = f'#!/bin/sh\nexec /usr/bin/python3 -I -B {INSTALLED}/usb-recovery.py "$@"\n'
    _write(prefix / LAUNCHER.lstrip('/'), launcher.encode(), 0o755)
    _write(prefix / 'etc/homeserver-usb-recovery.json',
           (json.dumps(config, indent=2) + '\n').encode(), 0o600)
    cron = ('SHELL=/bin/sh\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\nMAILTO=""\n'
            f'* * * * * root {LAUNCHER} --once >/dev/null 2>&1\n')
    _write(targets[0] / 'homeserver-usb-recovery', cron.encode(), 0o600)


def write_installation(prefix, config, bundle):
    """Serialize reinstalls with runtime execution, including Python imports."""
    prefix = Path(prefix)
    state = prefix / 'var/lib/homeserver-usb-recovery'
    no_symlinks(state)
    if prefix == Path('/'):
        for relative in ('etc', 'etc/cron.d', 'usr/local',
                         'usr/local/lib', 'usr/local/sbin', INSTALLED.lstrip('/'),
                         'var/lib', 'var/lib/homeserver-usb-recovery'):
            target = prefix / relative
            no_symlinks(target)
            if target.exists() and (target.stat().st_uid != 0 or target.stat().st_mode & 0o022):
                raise ValueError('untrusted installation destination')
    _directory(state, 0o700)
    state.chmod(0o700)
    lock = state / 'lock'
    no_symlinks(lock)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if prefix == Path('/') and os.fstat(fd).st_uid != 0:
            raise ValueError('untrusted installation lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        disabled = state / 'disabled'
        no_symlinks(disabled)
        was_disabled = disabled.exists()
        if not was_disabled:
            _write(disabled, b'install-in-progress\n', 0o600)
        # A failed/interrupted installation remains disabled. A successful
        # reinstall preserves an operator's existing maintenance latch.
        for relative, content in bundle.items():
            if relative.endswith('.py'):
                compile(content, relative, 'exec')
        _publish(prefix, config, bundle)
        if not was_disabled:
            disabled.unlink()
    finally:
        os.close(fd)


def collect_bundle(project):
    common = project / 'services/common/src/homeserver_common'
    control = project / 'services/control/src/homeserver_control'
    files = {'usb-recovery.py': project / 'scripts/usb-recovery.py',
             'prepare_storage.py': project / 'scripts/prepare-storage.py'}
    for name in ('usb_recovery', 'usb_recovery_sources', 'storage', 'env',
                 'settings', 'download_profiles'):
        files[f'homeserver_common/{name}.py'] = common / f'{name}.py'
    for name in ('magnet', 'torrent_manifest', 'torrent_bytes'):
        files[f'homeserver_control/domain/{name}.py'] = control / 'domain' / f'{name}.py'
    files['homeserver_control/gateway/probe_permits.py'] = control / 'gateway/probe_permits.py'
    bundle = {name: path.read_bytes() for name, path in files.items()}
    for package in ('homeserver_common', 'homeserver_control',
                    'homeserver_control/domain', 'homeserver_control/gateway'):
        bundle[f'{package}/__init__.py'] = b''
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', required=True)
    parser.add_argument('--project', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt'):
        raise ValueError('installation requires root in the host mount namespace')
    project = args.project.resolve(strict=True)
    if args.project != project:
        raise ValueError('project path must not traverse symlinks')
    for operation in ('is-active', 'is-enabled'):
        subprocess.run(['systemctl', operation, '--quiet', 'cron.service'], check=True)
    registry = json.loads(Path('/srv/appdata/control/storage.json').read_text())
    output = subprocess.run(['lsblk', '--json', '--list', '--bytes', '--output',
                             'PATH,TYPE,FSTYPE,UUID,SERIAL,SIZE,RO,PKNAME,MAJ:MIN'],
                            text=True, capture_output=True, check=True, timeout=20)
    blocks = json.loads(output.stdout)
    for device in blocks['blockdevices']:
        if device['type'] == 'part':
            device['partition_number'] = int((Path('/sys/class/block')
                                             / Path(device['path']).name / 'partition').read_text())
    config = build_config(project, registry, blocks)
    sys.path.insert(0, str(project / 'services/common/src'))
    sys.path.insert(0, str(project / 'services/control/src'))
    from homeserver_common.env import parse_env
    from homeserver_common.usb_recovery import HostBackend, validate_config
    values = parse_env((project / '.env').read_text())
    minimum = int(values.get('HOMESERVER_STORAGE_MIN_FREE_GIB', '10'))
    if minimum < 0:
        raise ValueError('invalid minimum free capacity')
    config['minimum_free_bytes'] = minimum * 1024**3
    validate_config(config)
    observation = HostBackend(config).observe()
    if (any(pool['status'] != 'ready' for pool in observation['pools'].values())
            or not observation['union_ok']):
        raise ValueError('registered storage must be healthy before opting in')
    bundle = collect_bundle(project)
    write_installation(Path('/'), config, bundle)
    print(json.dumps({'installed': True, 'interval_seconds': 60,
                      'usb_pools': sum(pool['pool_id'] != 'ssd' for pool in config['pools'])}))


if __name__ == '__main__':
    main()
