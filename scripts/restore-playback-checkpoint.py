#!/usr/bin/env python3
"""Restore two authenticated checkpoint trees into new, private Linux staging.

Never starts Jellyfin or publishes the stage. A missing success receipt means the
stage is incomplete and must not be deployed. Sources are opened read-only.
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

CONFIG_PREFIX = 'srv/appdata/jellyfin'
MAX_MANIFEST_BYTES = 256 * 1024**2
MAX_MEMBERS = 700_000
MAX_SELECTED_MEMBERS = 100_000
MAX_SELECTED_BYTES = 64 * 1024**3


class RestoreError(RuntimeError):
    """Fixed, non-sensitive failure codes only."""


def require(value, code):
    if not value:
        raise RestoreError(code)


def safe_name(value):
    require(isinstance(value, str) and value and '\x00' not in value, 'unsafe_path')
    require(not any(part in {'', '.', '..'} for part in value.split('/')), 'unsafe_path')
    return value


def is_selected(name, prefixes):
    return any(name == prefix or name.startswith(prefix + '/') for prefix in prefixes)


def source_identity(stream):
    s = os.fstat(stream.fileno())
    require(stat.S_ISREG(s.st_mode), 'source_not_regular')
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def read_manifest(path, trusted_hash, prefixes):
    require(re.fullmatch('[0-9a-f]{64}', trusted_hash), 'manifest_hash_invalid')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        identity = source_identity(source)
        require(identity[2] <= MAX_MANIFEST_BYTES, 'manifest_size_limit')
        blob = source.read(MAX_MANIFEST_BYTES + 1)
        require(source_identity(source) == identity, 'manifest_changed')
    require(hashlib.sha256(blob).hexdigest() == trusted_hash, 'manifest_hash_mismatch')
    data = json.loads(blob)
    require(isinstance(data, dict) and type(data.get('version')) is int
            and data['version'] == 1, 'manifest_version')
    entries = data.get('entries')
    require(isinstance(entries, list) and len(entries) <= MAX_MEMBERS, 'manifest_entries')
    seen, selected, modes = set(), {}, {}
    for row in entries:
        require(isinstance(row, dict), 'manifest_entry')
        name = safe_name(row.get('path'))
        require(name not in seen, 'manifest_duplicate')
        seen.add(name)
        require(type(row.get('mode')) is int, 'manifest_mode')
        modes[name] = row['mode']
        if not is_selected(name, prefixes):
            continue
        mode = row['mode']
        require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), 'unsupported_selected_type')
        for key in ('uid', 'gid', 'size', 'mtime_ns'):
            require(type(row.get(key)) is int, 'manifest_metadata')
        require(0 <= row['uid'] < 2**32 and 0 <= row['gid'] < 2**32
                and row['size'] >= 0, 'manifest_metadata')
        attrs = row.get('attrs')
        require(isinstance(attrs, dict), 'manifest_attrs')
        decoded = {}
        for key, value in attrs.items():
            require(isinstance(key, str) and key and '\x00' not in key
                    and isinstance(value, str), 'manifest_attrs')
            decoded[key] = base64.b64decode(value, validate=True)
        row = dict(row, decoded_attrs=decoded)
        if stat.S_ISREG(mode):
            require(row.get('nlink', 1) == 1, 'unsupported_selected_hardlink')
            require(isinstance(row.get('sha256'), str)
                    and re.fullmatch('[0-9a-f]{64}', row['sha256']), 'manifest_file_hash')
        selected[name] = row
    require(len(selected) <= MAX_SELECTED_MEMBERS, 'selection_size_limit')
    for prefix in prefixes:
        require(prefix in selected and stat.S_ISDIR(selected[prefix]['mode']), 'missing_root')
    for name in selected:
        for ancestor in PurePosixPath(name).parents:
            value = str(ancestor)
            if value in modes:
                require(stat.S_ISDIR(modes[value]), 'unsafe_ancestor')
            if is_selected(value, prefixes):
                require(value in selected, 'missing_ancestor')
    size = sum(r['size'] for r in selected.values() if stat.S_ISREG(r['mode']))
    require(size <= MAX_SELECTED_BYTES, 'selection_size_limit')
    return selected


class BoundedTarInfo(tarfile.TarInfo):
    def _proc_pax(self, archive):
        require(self.size <= 16 * 1024**2, 'archive_metadata_limit')
        return super()._proc_pax(archive)

    def _proc_gnulong(self, archive):
        require(self.size <= 16 * 1024**2, 'archive_metadata_limit')
        return super()._proc_gnulong(archive)


def preflight(archive, selected, prefixes):
    members = {}
    count = 0
    for member in archive:
        count += 1
        require(count <= MAX_MEMBERS, 'archive_member_limit')
        name = safe_name(member.name[:-1] if member.isdir()
                         and member.name.endswith('/') else member.name)
        # TarFile otherwise retains every header, including unrelated services.
        archive.members.clear()
        if not is_selected(name, prefixes):
            continue
        require(name in selected and name not in members, 'archive_member_set')
        row = selected[name]
        require((stat.S_ISDIR(row['mode']) and member.isdir())
                or (stat.S_ISREG(row['mode']) and member.isfile()), 'archive_type')
        require(member.uid == row['uid'] and member.gid == row['gid']
                and member.mode == stat.S_IMODE(row['mode']), 'archive_metadata')
        require(member.size == (row['size'] if member.isfile() else 0), 'archive_size')
        members[name] = member
    require(members.keys() == selected.keys(), 'archive_member_set')
    return members


def output_path(destination, name, cache_prefix):
    prefix, root = (CONFIG_PREFIX, 'config') if is_selected(name, [CONFIG_PREFIX]) \
        else (cache_prefix, 'cache')
    suffix = name[len(prefix):].lstrip('/')
    return destination / root / suffix


def restore_metadata(path, row):
    os.chown(path, row['uid'], row['gid'], follow_symlinks=False)
    for name, value in row['decoded_attrs'].items():
        os.setxattr(path, name, value, follow_symlinks=False)
    os.chmod(path, stat.S_IMODE(row['mode']), follow_symlinks=False)
    os.utime(path, ns=(row['mtime_ns'], row['mtime_ns']), follow_symlinks=False)
    actual = path.lstat()
    require(actual.st_uid == row['uid'] and actual.st_gid == row['gid']
            and stat.S_IMODE(actual.st_mode) == stat.S_IMODE(row['mode'])
            and actual.st_mtime_ns == row['mtime_ns'], 'restored_metadata_mismatch')
    attrs = {key: os.getxattr(path, key, follow_symlinks=False)
             for key in os.listxattr(path, follow_symlinks=False)}
    require(attrs == row['decoded_attrs'], 'restored_attrs_mismatch')


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def restore(archive_path, manifest_path, manifest_sha256, cache_prefix, destination):
    require(re.fullmatch(r'var/lib/docker/volumes/[a-f0-9]{64}/_data', cache_prefix),
            'cache_prefix_invalid')
    prefixes = [CONFIG_PREFIX, cache_prefix]
    destination = Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(), 'destination_exists')
    require(destination.parent.is_dir(), 'destination_parent_missing')
    require(all(not p.is_symlink() for p in destination.parents), 'destination_parent_link')
    selected = read_manifest(manifest_path, manifest_sha256, prefixes)
    with os.fdopen(os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        identity = source_identity(source)
        with tarfile.open(fileobj=source, mode='r:', tarinfo=BoundedTarInfo) as archive:
            members = preflight(archive, selected, prefixes)
            require(source_identity(source) == identity, 'archive_changed')
            destination.mkdir(mode=0o700)
            destination.chmod(0o700)
            sync_directory(destination.parent)
            directories = sorted((n for n, r in selected.items() if stat.S_ISDIR(r['mode'])),
                                 key=lambda name: (name.count('/'), name))
            for name in directories:
                output_path(destination, name, cache_prefix).mkdir(mode=0o700)
            files, copied = 0, 0
            for name, row in selected.items():
                if not stat.S_ISREG(row['mode']):
                    continue
                target = output_path(destination, name, cache_prefix)
                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                digest, length = hashlib.sha256(), 0
                with os.fdopen(fd, 'wb') as output, archive.extractfile(members[name]) as payload:
                    while block := payload.read(1024 * 1024):
                        length += len(block)
                        require(length <= row['size'], 'content_size_mismatch')
                        output.write(block)
                        digest.update(block)
                    output.flush()
                    require(length == row['size'] and digest.hexdigest() == row['sha256'],
                            'content_mismatch')
                    restore_metadata(target, row)
                    os.fsync(output.fileno())
                files += 1
                copied += length
            for name in reversed(directories):
                target = output_path(destination, name, cache_prefix)
                restore_metadata(target, selected[name])
                sync_directory(target)
            require(source_identity(source) == identity, 'archive_changed')
            sync_directory(destination)
    receipt = {
        'version': 1, 'complete': True, 'manifest_sha256': manifest_sha256,
        'selected_files': files, 'selected_directories': len(directories),
        'selected_bytes': copied, 'file_hashes_verified': True, 'metadata_verified': True,
        'full_archive_hash_verified': False, 'application_started': False,
        'created_at_utc': datetime.datetime.now(datetime.UTC).isoformat(),
    }
    fd = os.open(destination / 'restore-receipt.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(receipt, output, sort_keys=True, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    sync_directory(destination)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--cache-prefix', required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = restore(args.archive, args.manifest, args.manifest_sha256,
                          args.cache_prefix, args.destination)
    except RestoreError as exc:
        print('Restore refused: ' + str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, tarfile.TarError):
        print('Restore refused: input_or_filesystem_error', file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
