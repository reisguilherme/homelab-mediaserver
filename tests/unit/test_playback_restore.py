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

SCRIPT = Path(__file__).parents[2] / 'scripts/restore-playback-checkpoint.py'
CONFIG = 'srv/appdata/jellyfin'
CACHE = 'var/lib/docker/volumes/' + 'a' * 64 + '/_data'
MTIME_NS = 1_700_000_000_123_456_789


@pytest.fixture
def restore():
    assert SCRIPT.exists(), 'selective playback restore helper is missing'
    spec = importlib.util.spec_from_file_location('playback_restore', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry(path, kind='file', payload=b'private fixture', **changes):
    value = {
        'path': path, 'mode': (stat.S_IFDIR if kind == 'dir' else stat.S_IFREG) | 0o750,
        'uid': os.getuid(), 'gid': os.getgid(), 'size': len(payload) if kind == 'file' else 0,
        'mtime_ns': MTIME_NS, 'ctime_ns': MTIME_NS, 'dev': 1, 'ino': 1, 'nlink': 1,
        'attrs': {},
    }
    if kind == 'file':
        value['sha256'] = hashlib.sha256(payload).hexdigest()
    value.update(changes)
    return value


def bundle(tmp_path, entries=None, archive_changes=None):
    entries = entries or [entry(CONFIG, 'dir'), entry(CONFIG + '/settings',
        attrs={'user.fixture': base64.b64encode(b'value').decode()}),
        entry(CACHE, 'dir'), entry(CACHE + '/cache')]
    manifest = tmp_path / 'payload-manifest.json'
    manifest.write_text(json.dumps({'version': 1, 'entries': entries}))
    archive = tmp_path / 'state.tar'
    rows = [(e, b'private fixture') for e in entries]
    if archive_changes:
        rows = archive_changes(rows)
    with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT) as tar:
        for e, payload in rows:
            info = tarfile.TarInfo(e['path'])
            info.mode = stat.S_IMODE(e['mode'])
            info.uid, info.gid = e['uid'], e['gid']
            info.pax_headers = {'mtime': '1700000000.123456789'}
            if stat.S_ISDIR(e['mode']):
                info.type = tarfile.DIRTYPE
            elif stat.S_ISLNK(e['mode']):
                info.type, info.linkname = tarfile.SYMTYPE, e.get('target', '/outside')
            elif stat.S_ISFIFO(e['mode']):
                info.type = tarfile.FIFOTYPE
            elif e.get('hardlink'):
                info.type, info.linkname = tarfile.LNKTYPE, e['hardlink']
            else:
                info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload) if info.isfile() else None)
    return archive, manifest, hashlib.sha256(manifest.read_bytes()).hexdigest()


def run(restore, tmp_path, **kwargs):
    archive, manifest, digest = bundle(tmp_path, **kwargs)
    destination = tmp_path / 'restored'
    result = restore.restore(archive, manifest, digest, CACHE, destination)
    return result, destination


def test_valid_restore_selects_only_two_trees_and_preserves_metadata(restore, tmp_path):
    def unrelated(rows):
        return rows + [(entry('etc/private-other-service'), b'do not restore')]
    result, dest = run(restore, tmp_path, archive_changes=unrelated)
    assert result['complete'] and result['selected_files'] == 2
    assert result['selected_bytes'] == 30
    assert result['file_hashes_verified']
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert set(p.name for p in dest.iterdir()) == {'config', 'cache', 'restore-receipt.json'}
    restored = dest / 'config/settings'
    assert restored.read_bytes() == b'private fixture'
    assert restored.stat().st_mtime_ns == MTIME_NS
    assert restored.stat().st_uid == os.getuid()
    assert restored.stat().st_gid == os.getgid()
    assert stat.S_IMODE(restored.stat().st_mode) == 0o750
    assert os.getxattr(restored, 'user.fixture') == b'value'


@pytest.mark.parametrize('path', ['../escape', '/absolute', CONFIG + '/../escape',
                                  CONFIG + '//escape', CONFIG + '/./escape'])
def test_unsafe_manifest_paths_are_rejected_before_destination(restore, tmp_path, path):
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, entries=[entry(CONFIG, 'dir'), entry(CACHE, 'dir'), entry(path)])
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('mode', [stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600])
def test_manifest_links_and_special_files_are_rejected_before_writes(restore, tmp_path, mode):
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, entries=[entry(CONFIG, 'dir'), entry(CACHE, 'dir'),
            entry(CONFIG + '/bad', mode=mode)])
    assert not (tmp_path / 'restored').exists()


@pytest.mark.parametrize('change', ['symlink', 'hardlink', 'special', 'unknown', 'duplicate',
                                  'mode', 'uid', 'gid', 'size', 'traversal', 'missing'])
def test_selected_archive_must_exactly_match_manifest(restore, tmp_path, change):
    def mutate(rows):
        rows = [(dict(e), p) for e, p in rows]
        if change == 'symlink':
            rows[1][0]['mode'] = stat.S_IFLNK | 0o750
        elif change == 'hardlink':
            rows[1][0]['hardlink'] = CACHE + '/cache'
        elif change == 'special':
            rows[1][0]['mode'] = stat.S_IFIFO | 0o750
        elif change == 'unknown':
            rows.append((entry(CONFIG + '/unknown'), b'private fixture'))
        elif change == 'duplicate':
            rows.append(rows[1])
        elif change in {'mode', 'uid', 'gid'}:
            rows[1][0][change] += 1
        elif change == 'size':
            rows[1] = (rows[1][0], b'wrong size')
        elif change == 'traversal':
            rows.append((entry(CONFIG + '/../escape'), b'private fixture'))
        elif change == 'missing':
            rows.pop()
        return rows
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, archive_changes=mutate)
    assert not (tmp_path / 'restored').exists()


def test_hash_mismatch_leaves_stage_without_success_receipt(restore, tmp_path):
    def mutate(rows):
        rows[1] = (rows[1][0], b'X' * 15)
        return rows
    with pytest.raises(restore.RestoreError, match='content_mismatch'):
        run(restore, tmp_path, archive_changes=mutate)
    assert not (tmp_path / 'restored/restore-receipt.json').exists()


def test_manifest_hash_mismatch_leaves_destination_absent(restore, tmp_path):
    archive, manifest, _ = bundle(tmp_path)
    with pytest.raises(restore.RestoreError, match='manifest_hash'):
        restore.restore(archive, manifest, '0' * 64, CACHE, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_ancestor_file_conflict_is_rejected(restore, tmp_path):
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, entries=[entry(CONFIG), entry(CONFIG + '/child'),
                                      entry(CACHE, 'dir')])
    assert not (tmp_path / 'restored').exists()


def test_duplicate_manifest_and_existing_destination_are_rejected(restore, tmp_path):
    with pytest.raises(restore.RestoreError):
        run(restore, tmp_path, entries=[entry(CONFIG, 'dir'), entry(CONFIG, 'dir'),
                                      entry(CACHE, 'dir')])
    archive, manifest, digest = bundle(tmp_path)
    dest = tmp_path / 'restored'
    dest.mkdir()
    (dest / 'keep').write_text('untouched')
    with pytest.raises(restore.RestoreError):
        restore.restore(archive, manifest, digest, CACHE, dest)
    assert (dest / 'keep').read_text() == 'untouched'


def test_cli_failure_is_nonzero_and_sanitized(restore, tmp_path):
    archive, manifest, _ = bundle(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT), '--archive', str(archive),
        '--manifest', str(manifest), '--manifest-sha256', '0' * 64, '--cache-prefix', CACHE,
        '--destination', str(tmp_path / 'stage')], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'manifest_hash' in result.stderr
    assert 'private fixture' not in result.stderr and str(tmp_path) not in result.stderr


def test_manifest_version_and_cache_scope_must_be_explicit(restore, tmp_path):
    archive, manifest, digest = bundle(tmp_path)
    with pytest.raises(restore.RestoreError, match='cache_prefix'):
        restore.restore(archive, manifest, digest, 'var/lib/docker', tmp_path / 'restored')
    data = json.loads(manifest.read_text())
    data['version'] = 2
    manifest.write_text(json.dumps(data))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(restore.RestoreError, match='manifest_version'):
        restore.restore(archive, manifest, digest, CACHE, tmp_path / 'restored')
    assert not (tmp_path / 'restored').exists()


def test_missing_selected_ancestor_and_hardlink_alias_are_rejected(restore, tmp_path):
    for middle in [entry(CONFIG + '/missing/file'), entry(CONFIG + '/alias', nlink=2)]:
        with pytest.raises(restore.RestoreError):
            run(restore, tmp_path, entries=[entry(CONFIG, 'dir'), entry(CACHE, 'dir'), middle])
    assert not (tmp_path / 'restored').exists()


def test_destination_symlink_ancestor_is_rejected(restore, tmp_path):
    archive, manifest, digest = bundle(tmp_path)
    (tmp_path / 'outside').mkdir()
    (tmp_path / 'redirect').symlink_to(tmp_path / 'outside', target_is_directory=True)
    with pytest.raises(restore.RestoreError, match='destination_parent_link'):
        restore.restore(archive, manifest, digest, CACHE, tmp_path / 'redirect/restored')
    assert list((tmp_path / 'outside').iterdir()) == []


def test_success_receipt_follows_durable_file_metadata_and_directory_entries(
        restore, tmp_path, monkeypatch):
    """A receipt must not claim completion while child metadata is still unsynced."""
    archive, manifest, digest = bundle(tmp_path, entries=[
        entry(CONFIG, 'dir'), entry(CONFIG + '/nested', 'dir'),
        entry(CONFIG + '/nested/settings'), entry(CACHE, 'dir'), entry(CACHE + '/cache'),
    ])
    destination = tmp_path / 'restored'
    receipt = destination / 'restore-receipt.json'
    events = []
    original_fsync = os.fsync

    def observed_fsync(fd):
        # Inspect the real open inode and perform the real flush, not a simulated filesystem.
        path = Path(os.readlink(f'/proc/self/fd/{fd}'))
        metadata = os.fstat(fd)
        original_fsync(fd)
        events.append((path, metadata, receipt.exists(), destination.exists()))

    monkeypatch.setattr(os, 'fsync', observed_fsync)
    restore.restore(archive, manifest, digest, CACHE, destination)
    data_paths = [destination / name for name in ['config/nested/settings', 'cache/cache']]
    directory_paths = [destination / name for name in ['config/nested', 'config', 'cache']]
    before_receipt = [(index, path, metadata) for index, (path, metadata, exists, _) in
                      enumerate(events) if not exists]
    durable = {}
    for path in data_paths + directory_paths:
        matching = [(index, metadata) for index, actual, metadata in before_receipt
                    if actual == path]
        assert matching, f'no pre-receipt fsync for {path.name}'
        index, metadata = matching[-1]
        assert metadata.st_mtime_ns == MTIME_NS, 'metadata changed after its final fsync'
        assert stat.S_IMODE(metadata.st_mode) == 0o750
        assert metadata.st_uid == os.getuid() and metadata.st_gid == os.getgid()
        durable[path] = index
    assert durable[destination / 'config/nested/settings'] < durable[destination / 'config/nested']
    assert durable[destination / 'config/nested'] < durable[destination / 'config']
    assert durable[destination / 'cache/cache'] < durable[destination / 'cache']
    stage_flush = [index for index, path, _ in before_receipt if path == destination]
    assert stage_flush and stage_flush[-1] > max(durable.values())
    assert any(path == destination.parent and stage_exists and not receipt_exists
               for path, _, receipt_exists, stage_exists in events)
    receipt_flush = next(i for i, (path, _, _, _) in enumerate(events) if path == receipt)
    assert receipt_flush > stage_flush[-1]
    assert events[-1][0] == destination and events[-1][2]
