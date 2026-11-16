import importlib.util
import json
import os
import tempfile
from pathlib import Path

import pytest
from test_verify_consolidation_target import (
    canonical,
    digest,
    fixture,  # noqa: F401
    historical_signature,
    write_json,
)

SCRIPT = Path(__file__).parents[2] / 'scripts/publish-consolidation-target.py'


@pytest.fixture
def publisher():
    spec = importlib.util.spec_from_file_location('publication', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def case(fixture):  # noqa: F811
    fixture.args.publication_relative = 'homeserver'
    fixture.args.directory_uid = os.geteuid()
    fixture.args.directory_gid = os.getegid()
    fixture.args.directory_mode = 0o755
    fixture.args.plan_directory = fixture.args.output_directory
    fixture.publication = fixture.args.mountpoint / 'homeserver'
    fixture.publication.mkdir()
    with tempfile.TemporaryDirectory(prefix='publication-lease-', dir='/run') as lease_dir:
        fixture.args.lease_path = Path(lease_dir) / 'lease'
        fixture.args.lease_path.write_text(
            'version=2\nrole=core-writer\nstate=ready\n'
            f'boot_id={fixture.args.boot_id}\n'
            f'filesystem_uuid={fixture.args.filesystem_uuid}\n'
            'generation=' + 'a' * 32 + '\nphysical_generation=' + 'b' * 64 + '\n'
            'sequence=1\nexpires_uptime_seconds=140\n')
        fixture.args.lease_path.chmod(0o644)
        fixture.args.execute = True
        fixture.args.max_links = 50_000
        yield fixture


def plan(publisher, case):
    summary = publisher.make_plan(case.args, observe=case.observe)
    case.args.plan_sha256 = summary['plan_sha256']
    return summary


def apply(publisher, case, **kwargs):
    return publisher.apply_plan(case.args, observe=case.observe, boot_clock=lambda: 100, **kwargs)


def test_publish_only_hardlinks_no_content_reads(publisher, case, monkeypatch):
    original = os.read

    def read(fd, amount):
        assert os.fstat(fd).st_dev != case.stage.stat().st_dev, 'media content was read'
        return original(fd, amount)

    monkeypatch.setattr(os, 'read', read)
    summary = plan(publisher, case)
    assert summary['selected_paths'] == 3
    outcome = apply(publisher, case)
    assert outcome['phase'] == 'publication_complete'
    assert outcome['current_content_hash_verified'] is False
    assert outcome['published_paths'] == 3
    for group in case.groups:
        for path in group['paths']:
            assert (case.stage / group['pool'] / path).stat().st_ino == (
                case.publication / path).stat().st_ino
    assert apply(publisher, case)['published_paths'] == 3


def test_global_collision_prevents_every_link(publisher, case):
    (case.publication / 'media').mkdir()
    (case.publication / 'media/second.bin').write_bytes(b'foreign')
    with pytest.raises(publisher.Hold, match='collision'):
        plan(publisher, case)
    assert not (case.publication / 'media/first.bin').exists()


@pytest.mark.parametrize('point', ['intent', 'linked', 'source_fsync', 'parent_fsync', 'done'])
def test_crash_then_reconcile_does_not_duplicate(publisher, case, point):
    plan(publisher, case)

    def crash(where, operation):
        if operation['kind'] == 'link' and where == point:
            raise RuntimeError('simulated_crash')

    with pytest.raises(RuntimeError, match='simulated_crash'):
        apply(publisher, case, hook=crash)
    result = apply(publisher, case)
    assert result['published_paths'] == 3
    assert (case.stage / 'hdd2/media/first.bin').stat().st_nlink == 4
    assert (case.stage / 'ssd/media/second.bin').stat().st_nlink == 2


def test_expired_lease_stops_before_media_mutation(publisher, case):
    plan(publisher, case)
    case.args.lease_path.write_text(case.args.lease_path.read_text().replace('=140', '=99'))
    with pytest.raises(publisher.Hold, match='lease'):
        apply(publisher, case)
    assert list(case.publication.iterdir()) == []


def test_current_metadata_mismatch_and_bad_plan_pin_hold(publisher, case):
    plan(publisher, case)
    correct = case.args.plan_sha256
    case.args.plan_sha256 = '0' * 64
    with pytest.raises(publisher.Hold):
        apply(publisher, case)
    case.args.plan_sha256 = correct
    (case.stage / 'ssd/media/second.bin').chmod(0o600)
    with pytest.raises(publisher.Hold, match='metadata'):
        apply(publisher, case)
    assert list(case.publication.iterdir()) == []


def test_resume_checks_completed_alias_and_no_false_success(publisher, case):
    plan(publisher, case)
    apply(publisher, case)
    (case.publication / 'media/first.bin').unlink()
    with pytest.raises(publisher.Hold):
        apply(publisher, case)


def test_receipt_records_historical_basis_and_exclusions(publisher, case):
    summary = plan(publisher, case)
    assert summary['excluded_paths'] == 0
    apply(publisher, case)
    receipt = json.loads((case.args.plan_directory / 'publication-receipt.json').read_text())
    assert receipt['basis'] == 'authenticated_historical_hashes_and_current_metadata'
    assert receipt['degraded_published'] is False
    assert receipt['all_media_ready'] is False


def extra_group(case, paths):
    group = {'pool': 'hdd', 'paths': sorted(paths)}
    source = case.stage / group['pool'] / group['paths'][0]
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b'auxiliary')
    for alias in group['paths'][1:]:
        destination = case.stage / group['pool'] / alias
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.hardlink_to(source)
    receipt = json.loads(next((case.evidence / 'healthy-content-cache/receipts').glob(
        '*.json')).read_text())
    receipt.update(group=group, source=historical_signature(source),
                   target=historical_signature(source), sha256=digest(b'auxiliary'),
                   source_sha256=digest(b'auxiliary'), target_sha256=digest(b'auxiliary'))
    write_json(case.evidence / 'healthy-content-cache/receipts' / (canonical(group) + '.json'),
               receipt)
    case.args.expected_groups += 1
    case.args.expected_paths += len(paths)
    case.args.expected_bytes += len(b'auxiliary')
    case.repin()
    return source


def test_hidden_recovery_excluded_and_mixed_alias_group_preserved(publisher, case):
    hidden = extra_group(case, ['.requests-recovery/private.json'])
    mixed = extra_group(case, ['.recovery/keep.bin', 'media/third.bin'])
    summary = plan(publisher, case)
    assert summary['selected_groups'] == 3 and summary['selected_paths'] == 4
    assert summary['excluded_groups'] == 1 and summary['excluded_paths'] == 2
    apply(publisher, case)
    assert hidden.stat().st_nlink == 1
    assert mixed.stat().st_nlink == 3
    assert not (case.publication / '.requests-recovery').exists()
    assert not (case.publication / '.recovery').exists()


@pytest.mark.parametrize('old,new', [
    ('state=ready', 'state=hold'), ('sequence=1', 'sequence=0'),
    ('expires_uptime_seconds=140', 'expires_uptime_seconds=146'),
    ('physical_generation=' + 'b' * 64, 'physical_generation=none'),
    ('version=2', 'version=1'), ('role=core-writer', 'role=playback'),
    ('boot_id=11111111', 'boot_id=33333333'),
    ('filesystem_uuid=22222222', 'filesystem_uuid=33333333'),
])
def test_invalid_lease_holds_without_links(publisher, case, old, new):
    plan(publisher, case)
    case.args.lease_path.write_text(case.args.lease_path.read_text().replace(old, new))
    with pytest.raises(publisher.Hold, match='lease'):
        apply(publisher, case)
    assert list(case.publication.iterdir()) == []


@pytest.mark.parametrize('change', ['generation', 'physical_generation', 'sequence'])
def test_lease_continuity_survives_process_restart(publisher, case, change):
    case.args.lease_path.write_text(
        case.args.lease_path.read_text().replace('sequence=1', 'sequence=2'))
    plan(publisher, case)
    case.args.max_links = 1
    assert apply(publisher, case)['phase'] == 'publication_partial'
    data = case.args.lease_path.read_text()
    replacements = {'generation': ('generation=' + 'a' * 32, 'generation=' + 'c' * 32),
                    'physical_generation': ('physical_generation=' + 'b' * 64,
                                            'physical_generation=' + 'c' * 64),
                    'sequence': ('sequence=2', 'sequence=1')}
    case.args.lease_path.write_text(data.replace(*replacements[change]))
    with pytest.raises(publisher.Hold, match='lease_regression'):
        apply(publisher, case)
    assert not (case.args.plan_directory / 'publication-receipt.json').exists()


def test_batch_resume_advances_and_reconcile_creates_no_unplanned_links(publisher, case):
    plan(publisher, case)
    case.args.max_links = 1
    assert apply(publisher, case)['published_paths'] == 1
    assert apply(publisher, case, reconcile=True)['published_paths'] == 1
    assert apply(publisher, case)['published_paths'] == 2
    assert apply(publisher, case)['phase'] == 'publication_complete'


@pytest.mark.parametrize('damage', ['partial_line', 'hash', 'sequence', 'link_metadata'])
def test_journal_corruption_holds(publisher, case, damage):
    plan(publisher, case)
    case.args.max_links = 1
    apply(publisher, case)
    journal = case.args.plan_directory / 'journal.jsonl'
    if damage == 'partial_line':
        with journal.open('ab') as stream:
            stream.write(b'{')
    else:
        rows = [json.loads(row) for row in journal.read_text().splitlines()]
        if damage == 'hash':
            rows[-1]['sha256'] = '0' * 64
        elif damage == 'sequence':
            rows[-1]['body']['sequence'] += 1
            rows[-1]['sha256'] = canonical(rows[-1]['body'])
        else:
            rows[-1]['body']['after']['size'] += 1
            rows[-1]['sha256'] = canonical(rows[-1]['body'])
        journal.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with pytest.raises(publisher.Hold, match='journal'):
        apply(publisher, case)
    assert not (case.args.plan_directory / 'publication-receipt.json').exists()


def test_directory_crash_is_ambiguous_and_does_not_adopt(publisher, case):
    plan(publisher, case)

    def crash(where, operation):
        if where == 'created':
            raise RuntimeError('crash_after_mkdir')

    with pytest.raises(RuntimeError):
        apply(publisher, case, hook=crash)
    with pytest.raises(publisher.Hold, match='directory'):
        apply(publisher, case)
    assert not (case.publication / 'media/first.bin').exists()


def test_unexpected_nlink_after_intent_is_not_adopted(publisher, case):
    plan(publisher, case)

    def crash(where, operation):
        if where == 'linked':
            source = case.stage / case.groups[0]['pool'] / case.groups[0]['paths'][0]
            (case.stage / 'foreign-alias').hardlink_to(source)
            raise RuntimeError('foreign_mutation')

    with pytest.raises(RuntimeError):
        apply(publisher, case, hook=crash)
    with pytest.raises(publisher.Hold, match='metadata'):
        apply(publisher, case)


@pytest.mark.parametrize('scope', ['source', 'parent', 'destination'])
def test_symlinks_are_never_followed(publisher, case, scope):
    if scope == 'source':
        source = case.stage / 'ssd/media/second.bin'
        source.unlink()
        source.symlink_to('/etc/passwd')
    elif scope == 'parent':
        (case.publication / 'media').symlink_to('/tmp', target_is_directory=True)
    else:
        (case.publication / 'media').mkdir()
        (case.publication / 'media/second.bin').symlink_to('/etc/passwd')
    with pytest.raises((publisher.Hold, OSError)):
        plan(publisher, case)


def test_foreign_exact_hardlink_is_a_collision(publisher, case):
    (case.publication / 'media').mkdir()
    (case.publication / 'media/second.bin').hardlink_to(case.stage / 'ssd/media/second.bin')
    with pytest.raises(publisher.Hold):
        plan(publisher, case)


def test_existing_directory_attributes_preserved(publisher, case):
    parent = case.publication / 'media'
    parent.mkdir(mode=0o711)
    before = (parent.stat().st_ino, parent.stat().st_mode, parent.stat().st_uid)
    plan(publisher, case)
    apply(publisher, case)
    assert (parent.stat().st_ino, parent.stat().st_mode, parent.stat().st_uid) == before


def test_evidence_pin_and_changed_boot_prevent_planning_or_apply(publisher, case):
    pin = case.args.evidence_receipt_sha256
    case.args.evidence_receipt_sha256 = '0' * 64
    with pytest.raises(publisher.Hold, match='pin'):
        plan(publisher, case)
    case.args.evidence_receipt_sha256 = pin
    plan(publisher, case)
    case.observation['boot_id'] = '33333333-3333-3333-3333-333333333333'
    with pytest.raises(publisher.Hold, match='runtime_pin'):
        apply(publisher, case)


def test_hold_after_link_preserves_pending_intent_for_safe_reconcile(publisher, case):
    plan(publisher, case)

    def withdraw(where, operation):
        if where == 'linked':
            case.args.lease_path.write_text(
                case.args.lease_path.read_text().replace('state=ready', 'state=hold'))

    with pytest.raises(publisher.Hold, match='lease'):
        apply(publisher, case, hook=withdraw)
    assert not (case.args.plan_directory / 'publication-receipt.json').exists()
    case.args.lease_path.write_text(
        case.args.lease_path.read_text().replace('state=hold', 'state=ready'))
    assert apply(publisher, case, reconcile=True)['published_paths'] == 1
    assert apply(publisher, case)['phase'] == 'publication_complete'


def test_cancellation_after_link_does_not_claim_success_or_lose_intent(publisher, case):
    plan(publisher, case)
    cancelled = False

    def cancel(where, operation):
        nonlocal cancelled
        if where == 'linked':
            cancelled = True

    with pytest.raises(publisher.Hold, match='cancelled'):
        apply(publisher, case, hook=cancel, cancelled=lambda: cancelled)
    assert not (case.args.plan_directory / 'publication-receipt.json').exists()
    assert apply(publisher, case)['published_paths'] == 3


def test_link_race_does_not_overwrite_foreign_file(publisher, case, monkeypatch):
    plan(publisher, case)
    original = publisher.link_from_fd

    def raced(source, parent, name):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        os.write(fd, b'foreign-race')
        os.close(fd)
        original(source, parent, name)

    monkeypatch.setattr(publisher, 'link_from_fd', raced)
    with pytest.raises(FileExistsError):
        apply(publisher, case)
    published = list(case.publication.rglob('*.bin'))
    assert len(published) == 1 and published[0].read_bytes() == b'foreign-race'
    assert not (case.args.plan_directory / 'publication-receipt.json').exists()


def test_unexplained_ctime_with_absent_destination_holds(publisher, case):
    plan(publisher, case)

    def change_after_intent(where, operation):
        if where == 'intent' and operation['kind'] == 'link':
            source = case.stage / 'hdd2/media/first.bin'
            previous = source.stat().st_mode & 0o7777
            source.chmod(0o600)
            source.chmod(previous)
            raise RuntimeError('ctime_changed')

    with pytest.raises(RuntimeError):
        apply(publisher, case, hook=change_after_intent)
    with pytest.raises(publisher.Hold, match='metadata'):
        apply(publisher, case)


@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('expiry', [139, 141])
def test_equal_sequence_requires_identical_expiry(publisher, case, resume, expiry):
    if resume:
        plan(publisher, case)
        case.args.max_links = 1
        assert apply(publisher, case)['phase'] == 'publication_partial'
    else:
        admission = publisher.Admission(case.args.lease_path, case.args, lambda: 100)
        admission.check()
    case.args.lease_path.write_text(case.args.lease_path.read_text().replace(
        'expires_uptime_seconds=140', f'expires_uptime_seconds={expiry}'))
    with pytest.raises(publisher.Hold, match='lease'):
        if resume:
            apply(publisher, case)
        else:
            admission.check()


def test_higher_sequence_can_renew_expiry_across_resume(publisher, case):
    plan(publisher, case)
    case.args.max_links = 1
    assert apply(publisher, case)['phase'] == 'publication_partial'
    case.args.lease_path.write_text(case.args.lease_path.read_text().replace(
        'expires_uptime_seconds=140', 'expires_uptime_seconds=141').replace(
        'sequence=1', 'sequence=2'))
    assert apply(publisher, case)['published_paths'] == 2
