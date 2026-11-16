#!/usr/bin/env python3
"""Restore seven authenticated core trees into NEW private Linux SSD staging.

Never starts applications, mounts storage, overwrites a stage or removes an
incomplete stage. Only restore-receipt.json marks success. Deploy the existing
restore-playback-checkpoint.py alongside this script for its audited helpers.
The manifest pin and selected content hashes are mandatory. A full archive hash
is checked only when --archive-sha256 is supplied; the receipt states its scope.
"""

import argparse
import base64
import datetime
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

_spec = importlib.util.spec_from_file_location(
    'playback_checkpoint_helpers', Path(__file__).with_name('restore-playback-checkpoint.py'))
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
RestoreError = _helpers.RestoreError
require = _helpers.require
safe_name = _helpers.safe_name
is_selected = _helpers.is_selected
source_identity = _helpers.source_identity
restore_metadata = _helpers.restore_metadata
sync_directory = _helpers.sync_directory
BoundedTarInfo = _helpers.BoundedTarInfo

APPS = ('radarr', 'sonarr', 'bazarr', 'prowlarr', 'seerr', 'qbittorrent', 'control')
PREFIXES = tuple('srv/appdata/' + name for name in APPS)
LOGS = 'srv/appdata/seerr/logs/'
LINK_PATTERNS = {
    'seerr.log': r'seerr-(\d{4}-\d{2}-\d{2})\.log',
    '.machinelogs.json': r'\.machinelogs-(\d{4}-\d{2}-\d{2})\.json',
    'jellyseerr.log': r'jellyseerr-(\d{4}-\d{2}-\d{2})\.log',
}
MAX_MANIFEST_BYTES = _helpers.MAX_MANIFEST_BYTES
MAX_MEMBERS = _helpers.MAX_MEMBERS
MAX_SELECTED_MEMBERS = _helpers.MAX_SELECTED_MEMBERS
MAX_SELECTED_BYTES = _helpers.MAX_SELECTED_BYTES
SPACE_MARGIN_BYTES = 1024**3


def valid_hash(value, source):
    require(isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value),
            source + '_hash_invalid')


def validate_link(name, row, selected):
    basename = name.removeprefix(LOGS)
    require(name.startswith(LOGS) and basename in LINK_PATTERNS, 'unsupported_selected_link')
    target = row.get('target')
    require(isinstance(target, str), 'unsafe_link_target')
    match = re.fullmatch(LINK_PATTERNS[basename], target)
    require(match is not None, 'unsafe_link_target')
    try:
        datetime.date.fromisoformat(match[1])
    except ValueError as exc:
        raise RestoreError('unsafe_link_target') from exc
    target_row = selected.get(LOGS + target)
    require(target_row is not None and stat.S_ISREG(target_row['mode']), 'unsafe_link_target')
    require(row['size'] == len(target.encode('utf-8'))
            and stat.S_IMODE(row['mode']) == 0o777, 'manifest_link_metadata')


def read_manifest(source, trusted_hash):
    valid_hash(trusted_hash, 'manifest')
    require(source_identity(source)[2] <= MAX_MANIFEST_BYTES, 'manifest_size_limit')
    blob = source.read(MAX_MANIFEST_BYTES + 1)
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
        if not is_selected(name, PREFIXES):
            continue
        mode = row['mode']
        require(stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode),
                'unsupported_selected_type')
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
        if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            require(type(row.get('nlink', 1)) is int and row.get('nlink', 1) == 1,
                    'unsupported_selected_hardlink')
        if stat.S_ISREG(mode):
            valid_hash(row.get('sha256'), 'manifest_file')
        selected[name] = row
        require(len(selected) <= MAX_SELECTED_MEMBERS, 'selection_size_limit')
    for prefix in PREFIXES:
        require(prefix in selected and stat.S_ISDIR(selected[prefix]['mode']), 'missing_root')
    for name, row in selected.items():
        for ancestor in PurePosixPath(name).parents:
            value = str(ancestor)
            if value in modes:
                require(stat.S_ISDIR(modes[value]), 'unsafe_ancestor')
            if is_selected(value, PREFIXES):
                require(value in selected, 'missing_ancestor')
        if stat.S_ISLNK(row['mode']):
            validate_link(name, row, selected)
    size = sum(row['size'] for row in selected.values() if stat.S_ISREG(row['mode']))
    require(size <= MAX_SELECTED_BYTES, 'selection_size_limit')
    return selected


def preflight(archive, selected):
    members = {}
    for count, member in enumerate(archive, 1):
        require(count <= MAX_MEMBERS, 'archive_member_limit')
        name = safe_name(member.name[:-1] if member.isdir()
                         and member.name.endswith('/') else member.name)
        archive.members.clear()
        if not is_selected(name, PREFIXES):
            continue
        require(name in selected and name not in members, 'archive_member_set')
        row = selected[name]
        require((stat.S_ISDIR(row['mode']) and member.isdir())
                or (stat.S_ISREG(row['mode']) and member.isfile())
                or (stat.S_ISLNK(row['mode']) and member.issym()), 'archive_type')
        require(member.uid == row['uid'] and member.gid == row['gid']
                and member.mode == stat.S_IMODE(row['mode']), 'archive_metadata')
        require(member.size == (row['size'] if member.isfile() else 0), 'archive_size')
        if member.issym():
            require(member.linkname == row['target'], 'archive_link_target')
        members[name] = member
    require(members.keys() == selected.keys(), 'archive_member_set')
    return members


def unchanged(source, path, identity, kind):
    require(source_identity(source) == identity, kind + '_changed')
    current = os.stat(path, follow_symlinks=False)
    require(stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                 current.st_ctime_ns) == identity, kind + '_changed')


def output_path(destination, name):
    return destination / name.removeprefix('srv/appdata/')


def restore_link(path, row):
    os.symlink(row['target'], path)
    os.lchown(path, row['uid'], row['gid'])
    for name, value in row['decoded_attrs'].items():
        os.setxattr(path, name, value, follow_symlinks=False)
    os.utime(path, ns=(row['mtime_ns'], row['mtime_ns']), follow_symlinks=False)
    actual = path.lstat()
    require(stat.S_ISLNK(actual.st_mode) and os.readlink(path) == row['target']
            and actual.st_uid == row['uid'] and actual.st_gid == row['gid']
            and stat.S_IMODE(actual.st_mode) == stat.S_IMODE(row['mode'])
            and actual.st_mtime_ns == row['mtime_ns'], 'restored_metadata_mismatch')
    attrs = {key: os.getxattr(path, key, follow_symlinks=False)
             for key in os.listxattr(path, follow_symlinks=False)}
    require(attrs == row['decoded_attrs'], 'restored_attrs_mismatch')


def publish_receipt(destination, receipt, verify_sources):
    temporary = destination / '.restore-receipt.tmp'
    final = destination / 'restore-receipt.json'
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(receipt, output, sort_keys=True, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    verify_sources()
    # link() atomically publishes a complete inode and refuses an existing name.
    os.link(temporary, final, follow_symlinks=False)
    try:
        temporary.unlink()
        sync_directory(destination)
    except OSError:
        final.unlink()
        sync_directory(destination)
        raise


def restore(archive_path, manifest_path, manifest_sha256, destination, *, archive_sha256=None):
    if archive_sha256 is not None:
        valid_hash(archive_sha256, 'archive')
    destination = Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(), 'destination_exists')
    require(destination.parent.is_dir(), 'destination_parent_missing')
    require(all(not p.is_symlink() for p in destination.parents), 'destination_parent_link')
    with os.fdopen(os.open(manifest_path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as manifest, \
            os.fdopen(os.open(archive_path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        manifest_identity, archive_identity = source_identity(manifest), source_identity(source)

        def verify_sources():
            unchanged(manifest, manifest_path, manifest_identity, 'manifest')
            unchanged(source, archive_path, archive_identity, 'archive')

        selected = read_manifest(manifest, manifest_sha256)
        verify_sources()
        with tarfile.open(fileobj=source, mode='r:', tarinfo=BoundedTarInfo) as archive:
            members = preflight(archive, selected)
            verify_sources()
            selected_bytes = sum(r['size'] for r in selected.values() if stat.S_ISREG(r['mode']))
            margin = max(SPACE_MARGIN_BYTES, (selected_bytes + 19) // 20)
            space = os.statvfs(destination.parent)
            require(space.f_bavail * space.f_frsize >= selected_bytes + margin,
                    'insufficient_space')
            if archive_sha256 is not None:
                source.seek(0)
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
                require(digest == archive_sha256, 'archive_hash_mismatch')
                verify_sources()
            destination.mkdir(mode=0o700)
            destination.chmod(0o700)
            sync_directory(destination.parent)
            directories = sorted((n for n, r in selected.items() if stat.S_ISDIR(r['mode'])),
                                 key=lambda name: (name.count('/'), name))
            for name in directories:
                output_path(destination, name).mkdir(mode=0o700)
            files, copied = 0, 0
            for name, row in selected.items():
                if not stat.S_ISREG(row['mode']):
                    continue
                target = output_path(destination, name)
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
            links = 0
            for name, row in selected.items():
                if stat.S_ISLNK(row['mode']):
                    restore_link(output_path(destination, name), row)
                    links += 1
            for name in reversed(directories):
                target = output_path(destination, name)
                restore_metadata(target, selected[name])
                sync_directory(target)
            verify_sources()
            sync_directory(destination)
        receipt = {
            'version': 1, 'complete': True, 'manifest_sha256': manifest_sha256,
            'archive_sha256': archive_sha256, 'selected_files': files,
            'selected_directories': len(directories), 'selected_symlinks': links,
            'selected_bytes': copied, 'file_hashes_verified': True, 'metadata_verified': True,
            'full_archive_hash_verified': archive_sha256 is not None,
            'space_required_bytes': selected_bytes + margin, 'application_started': False,
            'created_at_utc': datetime.datetime.now(datetime.UTC).isoformat(),
        }
        publish_receipt(destination, receipt, verify_sources)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--archive-sha256')
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = restore(args.archive, args.manifest, args.manifest_sha256, args.destination,
                          archive_sha256=args.archive_sha256)
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
