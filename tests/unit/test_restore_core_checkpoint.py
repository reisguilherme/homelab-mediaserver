import base64
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / 'scripts/restore-core-checkpoint.py'
APPS = ('radarr', 'sonarr', 'bazarr', 'prowlarr', 'seerr', 'qbittorrent', 'control')
PREFIX = 'srv/appdata/'
LOGS = PREFIX + 'seerr/logs/'
MTIME_NS = 1_700_000_000_123_456_789
PAYLOAD = b'private fixture'
LINKS = {
    'seerr.log': 'seerr-2025-12-01.log',
    '.machinelogs.json': '.machinelogs-2025-12-01.json',
    'jellyseerr.log': 'jellyseerr-2024-02-29.log',
}


@pytest.fixture
def restore():
    assert SCRIPT.exists(), 'selective core restore helper is missing'
    spec = importlib.util.spec_from_file_location('core_restore', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry(path, kind='file', **changes):
    types = {'file': stat.S_IFREG, 'dir': stat.S_IFDIR, 'link': stat.S_IFLNK}
    value = {
        'path': path, 'mode': types[kind] | (0o777 if kind == 'link' else 0o750),
        'uid': os.getuid(), 'gid': os.getgid(), 'size': len(PAYLOAD) if kind == 'file' else 0,
        'mtime_ns': MTIME_NS, 'nlink': 1, 'attrs': {},
    }
    if kind == 'file':
        value['sha256'] = hashlib.sha256(PAYLOAD).hexdigest()
    value.update(changes)
    if kind == 'link':
        value['size'] = len(value.get('target', '').encode())
    return value


def rows():
    values = [entry(PREFIX + app, 'dir') for app in APPS]
    values += [entry(PREFIX + 'radarr/settings',
                     attrs={'user.fixture': base64.b64encode(b'value').decode()}),
               entry(PREFIX + 'qbittorrent/qBit.homeserver-repair', 'dir'),
               entry(PREFIX + 'qbittorrent/qBit.homeserver-repair/settings'),
               entry(LOGS[:-1], 'dir')]
    # Links deliberately precede targets in the archive and manifest.
    values += [entry(LOGS + name, 'link', target=target) for name, target in LINKS.items()]
    values += [entry(LOGS + target) for target in LINKS.values()]
    values += [entry('srv/appdata/jellyfin', 'dir'), entry('srv/appdata/jellyfin/private')]
    return values


def bundle(tmp_path, entries=None, mutate_archive=None):
    entries = rows() if entries is None else entries
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'version': 1, 'entries': entries}))
    archive = tmp_path / 'state.tar'
    members = [(dict(row), PAYLOAD) for row in entries]
    if mutate_archive:
        members = mutate_archive(members)
    with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as tar:
        for row, payload in members:
            info = tarfile.TarInfo(row['path'])
            info.mode = stat.S_IMODE(row['mode'])
            info.uid, info.gid = row['uid'], row['gid']
            info.pax_headers = {'mtime': '1700000000.123456789'}
            if stat.S_ISDIR(row['mode']):
                info.type = tarfile.DIRTYPE
            elif stat.S_ISLNK(row['mode']):
                info.type, info.linkname = tarfile.SYMTYPE, row['target']
            elif row.get('hardlink'):
                info.type, info.linkname = tarfile.LNKTYPE, row['hardlink']
            else:
                info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload) if info.isfile() else None)
    return (archive, manifest, hashlib.sha256(archive.read_bytes()).hexdigest(),
            hashlib.sha256(manifest.read_bytes()).hexdigest())


def run(restore, tmp_path, verify_archive=False, **kwargs):
    archive, manifest, archive_hash, manifest_hash = bundle(tmp_path, **kwargs)
    destination = tmp_path / 'stage'
    receipt = restore.restore(archive, manifest, manifest_hash, destination,
                              archive_sha256=archive_hash if verify_archive else None)
    return receipt, destination


def test_restores_only_seven_full_trees_with_links_metadata_and_pinned_hashes(restore, tmp_path):
    receipt, stage = run(restore, tmp_path)
    assert set(p.name for p in stage.iterdir()) == {*APPS, 'restore-receipt.json'}
    assert stat.S_IMODE(stage.stat().st_mode) == 0o700
    assert (stage / 'qbittorrent/qBit.homeserver-repair/settings').read_bytes() == PAYLOAD
    target = stage / 'radarr/settings'
    assert target.read_bytes() == PAYLOAD
    assert os.getxattr(target, 'user.fixture') == b'value'
    for path in [target, stage / 'seerr/logs']:
        metadata = path.lstat()
        assert (metadata.st_uid, metadata.st_gid) == (os.getuid(), os.getgid())
        assert metadata.st_mtime_ns == MTIME_NS
        assert stat.S_IMODE(metadata.st_mode) == 0o750
    for name, value in LINKS.items():
        link = stage / 'seerr/logs' / name
        assert link.is_symlink() and os.readlink(link) == value
        assert link.read_bytes() == PAYLOAD
        assert link.lstat().st_mtime_ns == MTIME_NS
        assert stat.S_IMODE(link.lstat().st_mode) == 0o777
    assert receipt['complete'] and receipt['selected_files'] == 5
    assert receipt['selected_symlinks'] == 3 and receipt['selected_bytes'] == 75
    assert not receipt['full_archive_hash_verified'] and receipt['file_hashes_verified']
    assert receipt['metadata_verified'] and not receipt['application_started']
    assert json.loads((stage / 'restore-receipt.json').read_text()) == receipt


def test_explicit_archive_pin_is_verified_and_recorded(restore, tmp_path):
    receipt, _ = run(restore, tmp_path, verify_archive=True)
    assert receipt['full_archive_hash_verified']
    expected = hashlib.sha256((tmp_path / 'state.tar').read_bytes()).hexdigest()
    assert receipt['archive_sha256'] == expected


@pytest.mark.parametrize('path', ['../escape', '/absolute', PREFIX + 'radarr/../escape',
                                  PREFIX + 'radarr//escape', PREFIX + 'radarr/./escape'])
def test_manifest_traversal_refuses_before_stage_creation(restore, tmp_path, path):
    with pytest.raises(restore.RestoreError, match='unsafe_path'):
        run(restore, tmp_path, entries=rows() + [entry(path)])
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('target', ['/outside', '../seerr-2025-12-01.log',
                                    'nested/seerr-2025-12-01.log', 'jellyseerr-2024-02-29.log',
                                    'seerr-2025-02-30.log', 'seerr-2025-01-01.log',
                                    'seerr.log'])
def test_symlink_target_must_be_allowed_existing_regular_basename(restore, tmp_path, target):
    values = rows()
    row = next(row for row in values if row['path'] == LOGS + 'seerr.log')
    row.update(target=target, size=len(target))
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, entries=values)
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('path', [PREFIX + 'radarr/settings-link',
                                  PREFIX + 'seerr/other/seerr.log', LOGS + 'unknown.log'])
def test_other_links_are_forbidden(restore, tmp_path, path):
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, entries=rows() + [entry(path, 'link', target='seerr.log')])
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('change', ['ancestor', 'target_link', 'hardlink', 'missing_root',
                                    'missing_ancestor', 'duplicate'])
def test_manifest_structure_and_hardlinks_are_rejected(restore, tmp_path, change):
    values = rows()
    if change == 'ancestor':
        values += [entry(LOGS + 'seerr.log/child')]
    elif change == 'target_link':
        values = [row for row in values if row['path'] != LOGS + 'seerr-2025-12-01.log']
        values += [entry(LOGS + 'seerr-2025-12-01.log', 'link', target='seerr.log')]
    elif change == 'hardlink':
        next(row for row in values if row['path'] == PREFIX + 'radarr/settings')['nlink'] = 2
    elif change == 'missing_root':
        values = [row for row in values if row['path'] != PREFIX + 'control']
    elif change == 'missing_ancestor':
        values = [row for row in values if row['path'] != LOGS[:-1]]
    else:
        values.append(values[0])
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, entries=values)
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('change', ['link_target', 'hardlink', 'missing', 'duplicate',
                                    'unknown', 'mode', 'uid', 'size', 'traversal'])
def test_tar_header_preflight_refuses_mismatches_without_reading_contents(
        restore, tmp_path, monkeypatch, change):
    def mutate(members):
        link = next(row for row, _ in members if row['path'] == LOGS + 'seerr.log')
        index = next(i for i, (row, _) in enumerate(members)
                     if row['path'] == PREFIX + 'radarr/settings')
        if change == 'link_target':
            link['target'] = 'seerr-2025-01-01.log'
        elif change == 'hardlink':
            members[index][0]['hardlink'] = PREFIX + 'sonarr/settings'
        elif change == 'missing':
            members.pop(index)
        elif change == 'duplicate':
            members.append(members[index])
        elif change == 'unknown':
            members.append((entry(PREFIX + 'control/unknown'), PAYLOAD))
        elif change in {'mode', 'uid'}:
            members[index][0][change] += 1
        elif change == 'size':
            members[index] = (members[index][0], b'wrong')
        else:
            members.append((entry('../escape'), PAYLOAD))
        return members

    def no_payload(*args, **kwargs):
        pytest.fail('payload read before successful header preflight')

    monkeypatch.setattr(tarfile.TarFile, 'extractfile', no_payload)
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, mutate_archive=mutate)
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('source', ['manifest', 'archive'])
def test_source_digest_pin_is_required_before_stage(restore, tmp_path, source):
    archive, manifest, archive_hash, manifest_hash = bundle(tmp_path)
    if source == 'manifest':
        manifest_hash = '0' * 64
    else:
        archive_hash = '0' * 64
    with pytest.raises(restore.RestoreError, match=source + '_hash_mismatch'):
        restore.restore(archive, manifest, manifest_hash, tmp_path / 'stage',
                        archive_sha256=archive_hash)
    assert not (tmp_path / 'stage').exists()


def test_selected_content_hash_mismatch_leaves_no_success_receipt(restore, tmp_path):
    def mutate(members):
        index = next(i for i, (row, _) in enumerate(members)
                     if row['path'] == PREFIX + 'radarr/settings')
        members[index] = (members[index][0], b'X' * len(PAYLOAD))
        return members
    with pytest.raises(restore.RestoreError, match='content_mismatch'):
        run(restore, tmp_path, mutate_archive=mutate)
    assert not (tmp_path / 'stage/restore-receipt.json').exists()


@pytest.mark.parametrize('source', ['manifest', 'archive'])
@pytest.mark.parametrize('replace', [False, True])
def test_changed_source_during_restore_leaves_no_receipt(
        restore, tmp_path, monkeypatch, source, replace):
    archive, manifest, archive_hash, manifest_hash = bundle(tmp_path)
    source_path = manifest if source == 'manifest' else archive
    original = restore.restore_metadata

    def change_source(path, row):
        original(path, row)
        if not change_source.done:
            change_source.done = True
            if replace:
                replacement = tmp_path / 'replacement'
                replacement.write_bytes(source_path.read_bytes())
                replacement.replace(source_path)
            else:
                os.utime(source_path, ns=(MTIME_NS, MTIME_NS))

    change_source.done = False
    monkeypatch.setattr(restore, 'restore_metadata', change_source)
    with pytest.raises(restore.RestoreError, match=source + '_changed'):
        restore.restore(archive, manifest, manifest_hash, tmp_path / 'stage',
                        archive_sha256=archive_hash)
    assert not (tmp_path / 'stage/restore-receipt.json').exists()


def test_existing_destination_is_never_overwritten(restore, tmp_path):
    archive, manifest, archive_hash, manifest_hash = bundle(tmp_path)
    destination = tmp_path / 'stage'
    destination.mkdir()
    (destination / 'keep').write_text('untouched')
    with pytest.raises(restore.RestoreError, match='destination_exists'):
        restore.restore(archive, manifest, manifest_hash, destination, archive_sha256=archive_hash)
    assert (destination / 'keep').read_text() == 'untouched'


def test_parent_symlink_is_rejected(restore, tmp_path):
    archive, manifest, archive_hash, manifest_hash = bundle(tmp_path)
    (tmp_path / 'outside').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'outside', target_is_directory=True)
    with pytest.raises(restore.RestoreError, match='destination_parent_link'):
        restore.restore(archive, manifest, manifest_hash, tmp_path / 'link/stage',
                        archive_sha256=archive_hash)
    assert not (tmp_path / 'outside/stage').exists()


def test_free_space_uses_selected_bytes_plus_margin_before_writes(restore, tmp_path, monkeypatch):
    real_statvfs = os.statvfs

    def insufficient(path):
        values = list(real_statvfs(path))
        values[1], values[4] = 1, 75  # f_frsize, f_bavail: payload only, no margin.
        return os.statvfs_result(values)

    monkeypatch.setattr(os, 'statvfs', insufficient)
    with pytest.raises(restore.RestoreError, match='insufficient_space'):
        run(restore, tmp_path)
    assert not (tmp_path / 'stage').exists()


@pytest.mark.parametrize('limit', ['MAX_SELECTED_MEMBERS', 'MAX_SELECTED_BYTES', 'MAX_MEMBERS'])
def test_selection_and_archive_bounds_are_enforced(restore, tmp_path, monkeypatch, limit):
    monkeypatch.setattr(restore, limit, 1)
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path)
    assert not (tmp_path / 'stage').exists()


def test_receipt_is_published_only_after_fsync_and_links_never_chmodded(
        restore, tmp_path, monkeypatch):
    real_fsync, real_chmod = os.fsync, os.chmod
    flushed = set()
    receipt = tmp_path / 'stage/restore-receipt.json'

    def sync(fd):
        path = Path(os.readlink(f'/proc/self/fd/{fd}'))
        if receipt.exists():
            assert (tmp_path / 'stage/radarr/settings') in flushed
            assert (tmp_path / 'stage/seerr/logs') in flushed
            assert tmp_path / 'stage/.restore-receipt.tmp' in flushed
        real_fsync(fd)
        flushed.add(path)

    def chmod(path, mode, **kwargs):
        assert not Path(path).is_symlink(), 'chmod must never follow or target a symlink'
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(os, 'fsync', sync)
    monkeypatch.setattr(os, 'chmod', chmod)
    run(restore, tmp_path)
    assert receipt.is_file()


def test_receipt_write_failure_cannot_publish_success(restore, tmp_path, monkeypatch):
    original = os.fsync

    def fail_receipt(fd):
        path = Path(os.readlink(f'/proc/self/fd/{fd}'))
        if path.name == '.restore-receipt.tmp':
            raise OSError('fixture fsync failure')
        return original(fd)

    monkeypatch.setattr(os, 'fsync', fail_receipt)
    with pytest.raises(OSError):
        run(restore, tmp_path)
    assert not (tmp_path / 'stage/restore-receipt.json').exists()


def test_cli_errors_are_nonzero_and_sanitized(restore, tmp_path):
    archive, manifest, archive_hash, _ = bundle(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT), '--archive', str(archive),
        '--archive-sha256', archive_hash, '--manifest', str(manifest),
        '--manifest-sha256', '0' * 64, '--destination', str(tmp_path / 'stage')],
        capture_output=True, text=True)
    assert result.returncode == 2 and 'manifest_hash_mismatch' in result.stderr
    assert str(tmp_path) not in result.stderr and PAYLOAD.decode() not in result.stderr
