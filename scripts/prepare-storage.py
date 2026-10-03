#!/usr/bin/env python3
"""Verify mounted ext4 pools and mergerfs, probe UID 1000, stage private registry.

Never formats, mounts, edits fstab, installs packages, or touches existing media.
Only fixture files and project directories beneath verified roots are created.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from uuid import uuid4

CAPABILITIES = ('hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement')


def no_symlinks(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlink in storage path')


def mounts(path):
    def decode(value):
        return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)
    result = []
    for line in path.read_text().splitlines():
        before, after = line.split(' - ', 1)
        fields, extra = before.split(), after.split()
        major, minor = map(int, fields[2].split(':'))
        result.append({'target': Path(decode(fields[4])), 'root': decode(fields[3]),
                       'device': os.makedev(major, minor), 'options': fields[5].split(','),
                       'filesystem': extra[0], 'source': decode(extra[1]),
                       'super_options': extra[2].split(',')})
    return result


def exact_mount(target, mountinfo):
    found = [m for m in mounts(mountinfo) if m['target'] == target]
    if len(found) != 1:
        raise ValueError(f'physical mount absent or ambiguous: {target}')
    return found[0]


def verify_physical(target, filesystem_id, *, mountinfo=Path('/proc/self/mountinfo'),
                    uuid_dir=Path('/dev/disk/by-uuid')):
    no_symlinks(target)
    mount = exact_mount(target, mountinfo)
    if (mount['filesystem'] != 'ext4' or mount['root'] != '/'
            or not mount['source'].startswith('/dev/')):
        raise ValueError(f'physical mount must be an ext4 filesystem root: {target}')
    link = uuid_dir / filesystem_id
    if not link.is_symlink() or Path(os.readlink(link)).name != Path(mount['source']).name:
        raise ValueError(f'UUID does not match physical mount: {target}')
    if target.stat().st_dev != mount['device']:
        raise ValueError(f'physical mount device mismatch: {target}')
    if 'rw' not in mount['options'] or 'rw' not in mount['super_options']:
        raise ValueError(f'physical mount is read-only: {target}')
    return mount


def verify_view(view, branches, *, mountinfo=Path('/proc/self/mountinfo')):
    no_symlinks(view)
    mount = exact_mount(view, mountinfo)
    if (mount['filesystem'] != 'fuse.mergerfs' or mount['root'] != '/'
            or view.stat().st_dev != mount['device'] or 'rw' not in mount['options']):
        raise ValueError('mergerfs view or branches do not match the physical pools')
    expected = ':'.join(f'{branch}=RW' for branch in branches)
    if os.getxattr(view / '.mergerfs', 'user.mergerfs.branches').decode() != expected:
        raise ValueError('mergerfs branches do not match the physical pools')
    for key, expected in {'category.create': 'epff', 'ignorepponrename': 'true',
                          'moveonenospc': 'false', 'link_cow': 'false',
                          'symlinkify': 'false'}.items():
        if os.getxattr(view / '.mergerfs', 'user.mergerfs.' + key).decode() != expected:
            raise ValueError(f'unsafe mergerfs option: {key}')
    return mount


def guard(path):
    no_symlinks(path)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError(f'placement guard must be root-owned and not writable by UID 1000: {path}')


def project_directories(root):
    no_symlinks(root)
    for relative in ('', 'torrents', 'media', 'media/movies', 'media/tv'):
        path = root / relative
        no_symlinks(path)
        if not path.exists():
            path.mkdir(mode=0o755)
            os.chown(path, 1000, 1000)
    parent = root / 'torrents/.placements'
    no_symlinks(parent)
    if not parent.exists():
        parent.mkdir(mode=0o755)
    guard(parent)  # Existing unsafe guards are refused, never silently adopted.


def probe(pool, other, view):
    """A child does real operations as downloader through the exact logical view."""
    identity = 'storage-probe-' + uuid4().hex
    source_relative = Path('torrents/.placements') / identity
    media_relative = Path('media') / ('.' + identity)
    source_dir, media_dir = pool / source_relative, pool / media_relative
    for directory in (source_dir, media_dir):
        directory.mkdir(mode=0o755)
        os.chown(directory, 1000, 1000)
    try:
        pid = os.fork()
        if pid == 0:
            try:
                os.setgroups([])
                os.setgid(1000)
                os.setuid(1000)
                source = view / source_relative / 'payload'
                link = view / media_relative / 'linked'
                source.write_bytes(b'HomeServer storage fixture\n')
                os.link(source, link)
                physical_source = pool / source_relative / 'payload'
                physical_link = pool / media_relative / 'linked'
                first, second = physical_source.stat(), physical_link.stat()
                if ((first.st_dev, first.st_ino) != (second.st_dev, second.st_ino)
                        or first.st_uid != 1000 or first.st_gid != 1000
                        or first.st_nlink != 2 or (other / source_relative).exists()
                        or (other / media_relative).exists()):
                    raise ValueError('hardlink/ownership/pool affinity probe failed')
                renamed = link.with_name('renamed')
                link.rename(renamed)
                if (pool / media_relative / 'renamed').stat().st_ino != first.st_ino:
                    raise ValueError('rename changed physical identity')
                renamed.unlink()
                source.unlink()
                for parent in (pool / 'torrents/.placements', other / 'torrents/.placements',
                               view / 'torrents/.placements'):
                    try:
                        (parent / ('forbidden-' + identity)).mkdir()
                    except PermissionError:
                        continue
                    raise ValueError('downloader can recreate an unauthorized placement')
                os._exit(0)
            except BaseException as exc:
                os.write(2, (f'storage fixture failed: {exc}\n').encode())
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
            raise ValueError('UID 1000 filesystem capability probe failed')
    finally:
        # Strictly scoped unique fixture folders; no real media or unknown paths.
        for directory in (source_dir, media_dir):
            shutil.rmtree(directory)
    return dict.fromkeys(CAPABILITIES, True)


def publish_registry(output, identities, capabilities):
    if any(capabilities[p].get(key) is not True for p in ('ssd', 'hdd') for key in CAPABILITIES):
        raise ValueError('filesystem capability probe incomplete')
    no_symlinks(output)
    if not output.parent.is_dir():
        raise ValueError('registry output parent must already exist')
    document = {'version': 1, 'pools': [
        {'pool_id': name, 'filesystem_id': identities[name], 'capabilities': capabilities[name]}
        for name in ('ssd', 'hdd')]}
    descriptor, temporary = tempfile.mkstemp(prefix='.storage-', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(document, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), 1000, 1000)
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssd-uuid', required=True)
    parser.add_argument('--hdd-uuid', required=True)
    parser.add_argument('--output', type=Path,
                        default=Path('/srv/appdata/control/storage.pending.json'))
    parser.add_argument('--check', action='store_true',
                        help='read-only mount and guard checks; no probes or registry')
    args = parser.parse_args(argv)
    try:
        if args.ssd_uuid == args.hdd_uuid:
            raise ValueError('pools require distinct physical UUIDs')
        for value in (args.ssd_uuid, args.hdd_uuid):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
                raise ValueError('invalid filesystem UUID')
        ssd, external, view = Path('/srv/data'), Path('/srv/external'), Path('/srv/media-view')
        hdd = external / 'homeserver'
        identities = {'ssd': args.ssd_uuid, 'hdd': args.hdd_uuid}
        for target, name in ((ssd, 'ssd'), (external, 'hdd')):
            verify_physical(target, identities[name])
        verify_view(view, (ssd, hdd))
        if args.check:
            for root in (ssd, hdd):
                guard(root / 'torrents/.placements')
            print('storage mounts and guards verified; no capability probes or writes performed')
            return 0
        if os.geteuid() != 0:
            raise ValueError('installer probes require root to drop to UID/GID 1000')
        for root in (ssd, hdd):
            project_directories(root)
        capabilities = {'ssd': probe(ssd, hdd, view), 'hdd': probe(hdd, ssd, view)}
        # Revalidate mount identities after fixtures, before publishing evidence.
        verify_physical(ssd, args.ssd_uuid)
        verify_physical(external, args.hdd_uuid)
        verify_view(view, (ssd, hdd))
        publish_registry(args.output, identities, capabilities)
        print(f'storage capability probes passed; private registry staged at {args.output}')
        return 0
    except (OSError, ValueError) as exc:
        print(f'storage preparation refused: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
