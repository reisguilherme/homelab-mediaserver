import hashlib
import importlib.util
import json
import os
import stat
import tempfile
from argparse import Namespace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / 'scripts/verify-consolidation-target.py'
CHECKPOINT = 'a' * 64
BOOT = '11111111-1111-1111-1111-111111111111'
UUID = '22222222-2222-2222-2222-222222222222'
STAGE = '.homeserver-consolidation/consolidation-fixture'


def digest(blob):
    return hashlib.sha256(blob).hexdigest()


def canonical(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, sort_keys=True))
    path.chmod(0o600)
    return path.read_bytes()


def test_destination_verifier_is_available():
    assert SCRIPT.is_file(), 'read-only destination verifier is missing'


@pytest.fixture
def verifier():
    spec = importlib.util.spec_from_file_location('destination_verify', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def historical_signature(path):
    value = path.stat()
    return dict(device=12345, inode=67890, ctime_ns=1, size=value.st_size,
                allocated=value.st_blocks * 512, mtime_ns=value.st_mtime_ns,
                mode=stat.S_IMODE(value.st_mode), uid=value.st_uid, gid=value.st_gid,
                nlink=value.st_nlink, xattrs={})


@pytest.fixture
def fixture(tmp_path):
    with tempfile.TemporaryDirectory(prefix='target-evidence-', dir='/dev/shm') as private:
        private = Path(private)
        mount = tmp_path / 'target'
        mount.mkdir()
        evidence = private / 'evidence'
        evidence.mkdir(mode=0o700)
        stage = mount / STAGE
        groups = [dict(pool='hdd2', paths=['media/first.bin', 'torrents/first.bin']),
                  dict(pool='ssd', paths=['media/second.bin'])]
        payloads = [b'first payload', b'second payload']
        cache = dict(version=1, owner='fixture', pins={'historical': True},
                     producer_sha256='b' * 64, created_at=1)
        cache['context'] = canonical(cache['pins'])
        cache_blob = write_json(evidence / 'healthy-content-cache/manifest.json', cache)
        for group, payload in zip(groups, payloads, strict=True):
            paths = [stage / group['pool'] / name for name in group['paths']]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
            paths[0].write_bytes(payload)
            for path in paths[1:]:
                path.hardlink_to(paths[0])
            signature = historical_signature(paths[0])
            receipt = dict(version=1, owner='fixture', context=cache['context'], group=group,
                           source=signature, target=signature, sha256=digest(payload),
                           source_sha256=digest(payload), target_sha256=digest(payload),
                           destination_fsynced=True, verified_at=1,
                           source_filesystem_id='old-source', target_filesystem_id='old-target',
                           identities={'source_root': '/NEVER_OPEN_DEGRADED_SOURCE'},
                           stage_bindings={'old': [987, 654]},
                           cache_manifest_sha256=digest(cache_blob))
            write_json(evidence / 'healthy-content-cache/receipts' /
                       (canonical(group) + '.json'), receipt)
        partial = stage / 'hdd2/.rsync-partial/excluded.bin'
        partial.parent.mkdir()
        partial.write_bytes(b'NEVER HASH THIS PARTIAL')
        write_json(evidence / 'degraded-proof.json', {'target_degraded': True})
        args = Namespace(evidence_directory=evidence, output_directory=private / 'new-session',
                         evidence_receipt_sha256='', checkpoint_manifest_sha256=CHECKPOINT,
                         mountpoint=mount, stage_relative=STAGE, boot_id=BOOT,
                         filesystem_uuid=UUID, mount_id=51,
                         device=f'{os.major(mount.stat().st_dev)}:{os.minor(mount.stat().st_dev)}',
                         expected_groups=2, expected_paths=3,
                         expected_bytes=sum(map(len, payloads)),
                         max_groups=32, max_bytes=64 * 1024**3, rate_mib=64,
                         max_seconds=900, resume=False)

        def repin():
            files = {}
            for path in evidence.rglob('*.json'):
                if path.name != 'restore-receipt.json':
                    blob = path.read_bytes()
                    files[str(path.relative_to(evidence))] = {'bytes': len(blob),
                                                             'sha256': digest(blob)}
            extraction = dict(version=1, complete=True, files=files,
                              manifest_sha256=CHECKPOINT, file_hashes_verified=True,
                              full_archive_hash_verified=False,
                              source_prefix='var/lib/homeserver-usb-recovery/incidents/'
                                            'consolidation-fixture')
            args.evidence_receipt_sha256 = digest(
                write_json(evidence / 'restore-receipt.json', extraction))

        repin()
        observation = dict(boot_id=BOOT, filesystem_uuid=UUID, mount_id=51,
                           device=args.device, root='/', fstype='ext4',
                           options=['noatime', 'rw'], namespace='fixture-namespace')
        yield Namespace(args=args, stage=stage, evidence=evidence, groups=groups,
                        payloads=payloads, observe=lambda: dict(observation),
                        observation=observation, repin=repin)


def execute(verifier, fixture, **kwargs):
    return verifier.run_batch(fixture.args, observe=fixture.observe, **kwargs)


def receipts(fixture):
    directory = fixture.args.output_directory / 'results'
    return list(directory.glob('*.json')) if directory.exists() else []


def test_reads_only_destination_once_per_inode_and_preserves_atime(
        verifier, fixture, monkeypatch):
    original_open, original_read, original_sync = os.open, os.read, os.fsync
    media_reads = 0
    target_device = fixture.stage.stat().st_dev
    paths = [fixture.stage / g['pool'] / g['paths'][0] for g in fixture.groups]
    before = [path.stat().st_atime_ns for path in paths]

    def opened(path, flags, *args, **kwargs):
        assert 'NEVER_OPEN_DEGRADED_SOURCE' not in str(path)
        fd = original_open(path, flags, *args, **kwargs)
        info = os.fstat(fd)
        if info.st_dev == target_device and stat.S_ISREG(info.st_mode):
            assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
            assert flags & os.O_NOATIME and flags & os.O_NOFOLLOW
        return fd

    def read(fd, size):
        nonlocal media_reads
        result = original_read(fd, size)
        if result and os.fstat(fd).st_dev == target_device:
            media_reads += len(result)
            assert b'NEVER HASH' not in result
        return result

    def sync(fd):
        assert os.fstat(fd).st_dev != target_device, 'fsync must never reach destination'
        return original_sync(fd)

    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(os, 'read', read)
    monkeypatch.setattr(os, 'fsync', sync)
    result = execute(verifier, fixture)
    assert result['phase'] == 'healthy_scope_complete'
    assert result['verified_groups'] == 2 and result['verified_paths'] == 3
    assert media_reads == fixture.args.expected_bytes == result['verified_bytes']
    assert result['degraded_content_verified'] is False and result['media_ready'] is False
    assert [path.stat().st_atime_ns for path in paths] == before
    assert len(receipts(fixture)) == 2
    for path in receipts(fixture):
        row = json.loads(path.read_text())
        assert row['fresh_destination_read'] is True
        assert row['baseline_kind'] == 'healthy'
        assert row['before'] == row['after']
        assert row['before']['device'] != 12345  # Old identities never become runtime pins.


@pytest.mark.parametrize('bad_pin', ['evidence_receipt_sha256', 'checkpoint_manifest_sha256'])
def test_bad_trust_pin_refuses_before_output(verifier, fixture, bad_pin):
    setattr(fixture.args, bad_pin, '0' * 64)
    with pytest.raises(RuntimeError, match='pin|manifest'):
        execute(verifier, fixture)
    assert not fixture.args.output_directory.exists()


@pytest.mark.parametrize('mutation', ['hash', 'traversal', 'partial', 'duplicate_path',
                                     'owner', 'nlink', 'boolean_size', 'cache_hash'])
def test_bad_authenticated_receipt_schema_refuses(verifier, fixture, mutation):
    path = next((fixture.evidence / 'healthy-content-cache/receipts').glob('*.json'))
    row = json.loads(path.read_text())
    if mutation == 'hash':
        row['target_sha256'] = '0' * 64
    elif mutation == 'traversal':
        row['group']['paths'] = ['../escape']
    elif mutation == 'partial':
        row['group']['paths'] = ['.rsync-partial/excluded.bin']
    elif mutation == 'duplicate_path':
        row['group']['paths'] *= 2
    elif mutation == 'owner':
        row['owner'] = 'different'
    elif mutation == 'nlink':
        row['target']['nlink'] = 99
    elif mutation == 'boolean_size':
        row['target']['size'] = True
    else:
        row['cache_manifest_sha256'] = '0' * 64
    write_json(path, row)
    fixture.repin()
    with pytest.raises(RuntimeError):
        execute(verifier, fixture)
    assert not fixture.args.output_directory.exists()


def test_duplicate_json_key_refuses(verifier, fixture):
    path = next((fixture.evidence / 'healthy-content-cache/receipts').glob('*.json'))
    path.write_text('{"version":1,"version":1}')
    fixture.repin()
    with pytest.raises(RuntimeError, match='duplicate_json_key'):
        execute(verifier, fixture)


def test_changed_evidence_hash_refuses(verifier, fixture):
    path = next((fixture.evidence / 'healthy-content-cache/receipts').glob('*.json'))
    path.write_text('{}')
    with pytest.raises(RuntimeError, match='evidence_hash'):
        execute(verifier, fixture)


@pytest.mark.parametrize('field,value', [('boot_id', 'other'), ('mount_id', 52),
                                        ('filesystem_uuid', 'other'), ('device', '8:99')])
def test_runtime_pin_mismatch_refuses(verifier, fixture, field, value):
    fixture.observation[field] = value
    with pytest.raises(RuntimeError, match='runtime_pin'):
        execute(verifier, fixture)
    assert not fixture.args.output_directory.exists()


def test_same_filesystem_output_refuses(verifier, fixture):
    fixture.args.output_directory = fixture.args.mountpoint / 'output'
    with pytest.raises(RuntimeError, match='output_filesystem'):
        execute(verifier, fixture)


def test_content_mismatch_has_no_group_success(verifier, fixture):
    path = fixture.stage / 'hdd2/media/first.bin'
    info = path.stat()
    path.write_bytes(b'x' * info.st_size)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(RuntimeError, match='content_mismatch'):
        execute(verifier, fixture)
    assert not receipts(fixture)
    progress = json.loads((fixture.args.output_directory / 'progress.json').read_text())
    assert progress['phase'] == 'hold'


@pytest.mark.parametrize('kind', ['symlink', 'missing_alias', 'split_alias'])
def test_unsafe_current_aliases_are_not_hashed_as_valid(verifier, fixture, kind):
    path = fixture.stage / 'hdd2/torrents/first.bin'
    first = fixture.stage / 'hdd2/media/first.bin'
    path.unlink()
    if kind == 'symlink':
        path.symlink_to(first)
    elif kind == 'split_alias':
        path.write_bytes(fixture.payloads[0])
    with pytest.raises((RuntimeError, OSError)):
        execute(verifier, fixture)
    assert not receipts(fixture)


def test_cancelled_hash_has_no_receipt(verifier, fixture, monkeypatch):
    original, cancelled = os.read, False
    device = fixture.stage.stat().st_dev

    def read(fd, size):
        nonlocal cancelled
        value = original(fd, size)
        if value and os.fstat(fd).st_dev == device:
            cancelled = True
        return value

    monkeypatch.setattr(os, 'read', read)
    with pytest.raises(RuntimeError, match='cancelled'):
        execute(verifier, fixture, cancelled=lambda: cancelled)
    assert not receipts(fixture)


def test_file_changes_during_hash_refuses(verifier, fixture, monkeypatch):
    original, changed = os.read, False
    path = fixture.stage / 'hdd2/media/first.bin'
    device = path.stat().st_dev

    def read(fd, size):
        nonlocal changed
        value = original(fd, size)
        if value and os.fstat(fd).st_dev == device and not changed:
            changed = True
            os.utime(path, ns=(1, 2))
        return value

    monkeypatch.setattr(os, 'read', read)
    with pytest.raises(RuntimeError, match='changed'):
        execute(verifier, fixture)
    assert not receipts(fixture)


def test_mount_change_during_hash_refuses(verifier, fixture, monkeypatch):
    original = os.read
    device = fixture.stage.stat().st_dev

    def read(fd, size):
        value = original(fd, size)
        if value and os.fstat(fd).st_dev == device:
            fixture.observation['mount_id'] += 1
        return value

    monkeypatch.setattr(os, 'read', read)
    with pytest.raises(RuntimeError, match='runtime_pin|runtime_changed'):
        execute(verifier, fixture)
    assert not receipts(fixture)


def test_noatime_failure_has_no_fallback(verifier, fixture, monkeypatch):
    original = os.open

    def opened(path, flags, *args, **kwargs):
        if str(path) == 'first.bin' and flags & os.O_NOATIME:
            raise PermissionError('fixture missing CAP_FOWNER')
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, 'open', opened)
    with pytest.raises((RuntimeError, PermissionError)):
        execute(verifier, fixture)
    assert not receipts(fixture)


def test_batch_resume_only_reuses_new_current_receipts(verifier, fixture, monkeypatch):
    fixture.args.max_groups = 1
    first = execute(verifier, fixture)
    assert first['phase'] == 'batch_limited' and first['verified_groups'] == 1
    fixture.args.resume = True
    original, read_bytes = os.read, 0
    device = fixture.stage.stat().st_dev

    def read(fd, size):
        nonlocal read_bytes
        value = original(fd, size)
        if value and os.fstat(fd).st_dev == device:
            read_bytes += len(value)
        return value

    monkeypatch.setattr(os, 'read', read)
    second = execute(verifier, fixture)
    assert second['phase'] == 'healthy_scope_complete' and second['verified_groups'] == 2
    assert read_bytes == len(fixture.payloads[1])


def test_resume_after_identity_change_is_refused(verifier, fixture):
    fixture.args.max_groups = 1
    execute(verifier, fixture)
    fixture.args.resume = True
    path = fixture.stage / 'hdd2/media/first.bin'
    os.utime(path, ns=(1, 2))
    with pytest.raises(RuntimeError, match='metadata|changed'):
        execute(verifier, fixture)


def test_existing_output_requires_explicit_resume(verifier, fixture):
    execute(verifier, fixture)
    with pytest.raises(RuntimeError, match='output_exists'):
        execute(verifier, fixture)


def test_budget_refuses_oversized_first_group(verifier, fixture):
    fixture.args.max_bytes = 1
    with pytest.raises(RuntimeError, match='group_exceeds_batch_budget'):
        execute(verifier, fixture)
    assert not receipts(fixture)


def test_rate_limit_accounts_for_every_hashed_byte(verifier, fixture):
    now = [0.0]
    sleeps = []

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    execute(verifier, fixture, clock=lambda: now[0], sleep=sleep)
    assert sum(sleeps) >= fixture.args.expected_bytes / (fixture.args.rate_mib * 1024**2)
    assert all(0 < delay <= 0.25 for delay in sleeps)


def test_mountinfo_parser_uses_exact_mountpoint(verifier):
    text = '51 1 8:17 / /srv/target rw,noatime - ext4 /dev/example rw\n'
    row = verifier.parse_mountinfo(text, Path('/srv/target'))
    assert row['mount_id'] == 51 and row['device'] == '8:17' and row['root'] == '/'
    with pytest.raises(RuntimeError):
        verifier.parse_mountinfo(text, Path('/srv/target/subdir'))


def test_same_device_nested_bind_mount_is_refused(verifier):
    text = ('51 1 8:17 / /srv/target rw,noatime - ext4 /dev/example rw\n'
            '52 51 8:17 /outside /srv/target/nested rw,noatime - ext4 /dev/example rw\n')
    with pytest.raises(RuntimeError, match='nested_mount'):
        verifier.parse_mountinfo(text, Path('/srv/target'))


def test_total_authenticated_evidence_budget_is_bounded(verifier, fixture, monkeypatch):
    monkeypatch.setattr(verifier, 'MAX_EVIDENCE_BYTES', 1, raising=False)
    with pytest.raises(RuntimeError, match='evidence_byte_limit'):
        execute(verifier, fixture)
    assert not fixture.args.output_directory.exists()


def test_time_budget_mid_read_is_batch_limited_without_success(verifier, fixture, monkeypatch):
    original = os.read
    now = [0.0]
    device = fixture.stage.stat().st_dev

    def read(fd, size):
        value = original(fd, size)
        if value and os.fstat(fd).st_dev == device:
            now[0] = fixture.args.max_seconds + 1
        return value

    monkeypatch.setattr(os, 'read', read)
    result = execute(verifier, fixture, clock=lambda: now[0])
    assert result['phase'] == 'batch_limited' and result['reason'] == 'time_budget'
    assert result['verified_groups'] == 0 and not receipts(fixture)


def test_result_fsync_failure_never_counts_success(verifier, fixture, monkeypatch):
    original = os.fsync

    def sync(fd):
        path = os.readlink(f'/proc/self/fd/{fd}')
        if '/results/.write-' in path:
            raise OSError('fixture SSD failure')
        return original(fd)

    monkeypatch.setattr(os, 'fsync', sync)
    with pytest.raises(OSError, match='fixture SSD failure'):
        execute(verifier, fixture)
    assert not receipts(fixture)


def test_resume_rejects_tampered_current_receipt(verifier, fixture):
    fixture.args.max_groups = 1
    execute(verifier, fixture)
    path = receipts(fixture)[0]
    row = json.loads(path.read_text())
    row['fresh_destination_read'] = False
    write_json(path, row)
    fixture.args.resume = True
    with pytest.raises(RuntimeError, match='current_receipt_invalid'):
        execute(verifier, fixture)


def test_rate_limiter_tolerates_time_spent_in_runtime_check(verifier, fixture, monkeypatch):
    original = os.read
    now, read_started, jumped = [0.0], False, False
    device = fixture.stage.stat().st_dev

    def read(fd, size):
        nonlocal read_started
        value = original(fd, size)
        if value and os.fstat(fd).st_dev == device:
            read_started = True
        return value

    def cancelled():
        nonlocal jumped
        if read_started and not jumped:
            now[0] += 1
            jumped = True
        return False

    def sleep(delay):
        assert delay >= 0, 'a runtime observation may outlast the throttle deadline'
        now[0] += delay

    monkeypatch.setattr(os, 'read', read)
    result = execute(verifier, fixture, clock=lambda: now[0], sleep=sleep, cancelled=cancelled)
    assert result['healthy_complete']
