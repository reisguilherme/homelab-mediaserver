#!/usr/bin/env python3
"""Verify mounted ext4 pools and mergerfs, probe UID 1000, stage private registry.

Never formats, mounts, edits fstab, installs packages, or touches existing media.
Only fixture files and project directories beneath verified roots are created.
"""
from __future__ import annotations

import argparse
import json
import mmap
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from uuid import uuid4

CAPABILITIES = ('hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement')
POOL_IDS = ('ssd', 'hdd', 'hdd2', 'hdd3')


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
                          'symlinkify': 'false', 'cache.files': 'auto-full',
                          'dropcacheonclose': 'true'}.items():
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


def probe_mmap(path):
    """Exercise libtorrent's shared mapping IO, not just ordinary file writes."""
    marker = b'HomeServer mmap fixture\n'
    payload = marker + bytes(4096 - len(marker))
    with path.open('r+b', buffering=0) as stream:
        with mmap.mmap(stream.fileno(), 4096, access=mmap.ACCESS_WRITE) as mapped:
            mapped[:] = payload
            mapped.flush()
            if mapped[:] != payload:
                raise ValueError('mmap readback differs from written fixture')
        os.fsync(stream.fileno())
    if path.read_bytes() != payload:
        raise ValueError('mmap flush/readback failed')
    return payload


def probe(pool, other, view):
    """A child does real operations as downloader through the exact logical view."""
    others = [other] if isinstance(other, Path) else list(other)
    if pool in others or len(set(others)) != len(others):
        raise ValueError('probe requires distinct counterpart roots')
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
                source.write_bytes(bytes(4096))
                mmap_payload = probe_mmap(source)
                os.link(source, link)
                physical_source = pool / source_relative / 'payload'
                physical_link = pool / media_relative / 'linked'
                first, second = physical_source.stat(), physical_link.stat()
                if ((first.st_dev, first.st_ino) != (second.st_dev, second.st_ino)
                        or first.st_uid != 1000 or first.st_gid != 1000
                        or first.st_nlink != 2
                        or any((root / source_relative).exists()
                               or (root / media_relative).exists() for root in others)
                        or physical_source.read_bytes() != mmap_payload
                        or link.read_bytes() != mmap_payload):
                    raise ValueError('hardlink/ownership/pool affinity probe failed')
                renamed = link.with_name('renamed')
                link.rename(renamed)
                if (pool / media_relative / 'renamed').stat().st_ino != first.st_ino:
                    raise ValueError('rename changed physical identity')
                renamed.unlink()
                source.unlink()
                for parent in [root / 'torrents/.placements' for root in (pool, *others, view)]:
                    forbidden = parent / ('forbidden-' + identity)
                    try:
                        forbidden.mkdir()
                    except PermissionError:
                        continue
                    forbidden.rmdir()
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
    if ((set(identities) != {'hdd3'} and not {'ssd', 'hdd'}.issubset(identities))
            or set(identities) - set(POOL_IDS)
            or len(set(identities.values())) != len(identities)):
        raise ValueError('pools require distinct physical UUIDs and the fixed layout')
    ordered = [name for name in POOL_IDS if name in identities]
    if any(capabilities.get(p, {}).get(key) is not True
           for p in ordered for key in CAPABILITIES):
        raise ValueError('filesystem capability probe incomplete')
    document = {'version': 1, 'pools': [
        {'pool_id': name, 'filesystem_id': identities[name], 'capabilities': capabilities[name]}
        for name in ordered]}
    publish_text(output, json.dumps(document, indent=2) + '\n', mode=0o600)


def publish_text(output, content, *, mode):
    no_symlinks(output)
    if not output.parent.is_dir():
        raise ValueError('registry output parent must already exist')
    descriptor, temporary = tempfile.mkstemp(prefix='.storage-', dir=output.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), 1000, 1000)
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssd-uuid',
                        help='required with --hdd-uuid for a legacy multi-pool layout')
    parser.add_argument('--hdd-uuid',
                        help='required with --ssd-uuid for a legacy multi-pool layout')
    parser.add_argument('--hdd2-uuid',
                        help='optional second USB HD; requires the extra Compose override')
    parser.add_argument('--hdd3-uuid',
                        help='alone selects singleton hdd3; otherwise adds the third USB HD')
    parser.add_argument('--output', type=Path,
                        default=Path('/srv/appdata/control/storage.pending.json'))
    parser.add_argument('--check', action='store_true',
                        help='read-only mount and guard checks; no probes or registry')
    args = parser.parse_args(argv)
    try:
        identities = {name: value for name, value in
                      (('ssd', args.ssd_uuid), ('hdd', args.hdd_uuid)) if value is not None}
        if args.hdd2_uuid:
            identities['hdd2'] = args.hdd2_uuid
        if args.hdd3_uuid:
            identities['hdd3'] = args.hdd3_uuid
        if set(identities) != {'hdd3'} and not {'ssd', 'hdd'}.issubset(identities):
            raise ValueError('SSD and first HD, or singleton hdd3, are required')
        if len(set(identities.values())) != len(identities):
            raise ValueError('pools require distinct physical UUIDs')
        for value in identities.values():
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value):
                raise ValueError('invalid filesystem UUID')
        view = Path('/srv/media-view')
        mount_paths = {'ssd': '/srv/data', 'hdd': '/srv/external',
                       'hdd2': '/srv/external2', 'hdd3': '/srv/external3'}
        mounts_by_pool = {name: Path(mount_paths[name]) for name in POOL_IDS if name in identities}
        roots = {name: mount if name == 'ssd' else mount / 'homeserver'
                 for name, mount in mounts_by_pool.items()}
        for name, target in mounts_by_pool.items():
            verify_physical(target, identities[name])
        verify_view(view, tuple(roots.values()))
        if args.check:
            for root in roots.values():
                guard(root / 'torrents/.placements')
            print('storage mounts and guards verified; no capability probes or writes performed')
            return 0
        if os.geteuid() != 0:
            raise ValueError('installer probes require root to drop to UID/GID 1000')
        for root in roots.values():
            project_directories(root)
        capabilities = {
            name: probe(root, [other for other in roots.values() if other != root], view)
            for name, root in roots.items()
        }
        # Revalidate mount identities after fixtures, before publishing evidence.
        for name, target in mounts_by_pool.items():
            verify_physical(target, identities[name])
        verify_view(view, tuple(roots.values()))
        publish_registry(args.output, identities, capabilities)
        if set(identities) == {'hdd3'}:
            publish_text(args.output.parent / 'storage-native.identity',
                         f'homeserver-single-media-v1 hdd3 {identities["hdd3"]}\n', mode=0o644)
        print(f'storage capability probes passed; private registry staged at {args.output}')
        return 0
    except (OSError, ValueError) as exc:
        print(f'storage preparation refused: {exc}')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
