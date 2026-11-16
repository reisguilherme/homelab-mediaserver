#!/usr/bin/env python3
"""Publish authenticated healthy staging aliases using Linux hardlinks only.

No media content reads, source unlink, overwrite, application action or mount.
Historical hashes plus current metadata are the explicitly selected criterion;
they do not constitute a fresh content hash. Private plan/journal stay off media.
"""

import argparse
import contextlib
import ctypes
import fcntl
import importlib.util
import json
import os
import re
import signal
import stat
import time
from pathlib import Path

HELPER = Path(__file__).with_name('verify-consolidation-target.py')
SPEC = importlib.util.spec_from_file_location('publication_metadata', HELPER)
v = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(v)
Hold = v.Hold
require = v.require
ALLOW = {'media', 'torrents'}
MAX_JOURNAL = 64 * 1024**2
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
LEASE = re.compile(
    rf'version=2\nrole=core-writer\nstate=(ready|hold)\nboot_id=({UUID})\n'
    rf'filesystem_uuid=({UUID})\ngeneration=([0-9a-f]{{32}})\n'
    r'physical_generation=(none|[0-9a-f]{64})\nsequence=([1-9][0-9]{0,18})\n'
    r'expires_uptime_seconds=(0|[1-9][0-9]{0,15})\n')


@contextlib.contextmanager
def owned(fd):
    try:
        yield fd
    finally:
        os.close(fd)


def directory_signature(fd):
    value = os.fstat(fd)
    require(stat.S_ISDIR(value.st_mode), 'directory_required')
    return dict(device=value.st_dev, inode=value.st_ino, uid=value.st_uid,
                gid=value.st_gid, mode=stat.S_IMODE(value.st_mode),
                xattrs={name: v.sha256(os.getxattr(fd, name)) for name in os.listxattr(fd)})


def directory(root, path):
    return os.dup(root) if not path else v.open_relative(root, path, directory=True)


def directory_or_none(root, path):
    try:
        with owned(directory(root, path)) as fd:
            return directory_signature(fd)
    except FileNotFoundError:
        return None


def destination_signature(root, path):
    parts = v.components(path)
    try:
        with owned(directory(root, '/'.join(parts[:-1]))) as parent:
            info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            require(stat.S_ISREG(info.st_mode), 'destination_collision')
            with owned(v.open_relative(parent, parts[-1])) as fd:
                return v.signature(fd)
    except FileNotFoundError:
        return None


def group_signature(stage, entry):
    result = None
    for path in entry['group']['paths']:
        with owned(v.open_relative(stage, entry['group']['pool'] + '/' + path)) as fd:
            current = v.signature(fd)
            require(result is None or result == current, 'stage_alias_changed')
            result = current
    return result


def linked_signature(before, after):
    return (isinstance(after, dict) and set(before) == set(after)
            and all(after[key] == value for key, value in before.items()
                    if key not in ('nlink', 'ctime_ns'))
            and after['nlink'] == before['nlink'] + 1
            and after['ctime_ns'] >= before['ctime_ns'])


def code_pins():
    return {'publisher': v.sha256(Path(__file__).read_bytes()),
            'metadata_helper': v.sha256(HELPER.read_bytes())}


class Admission:
    """Read the host lease directly; no Docker, callback or physical probe."""

    def __init__(self, path, args, clock):
        self.path, self.args, self.clock = Path(path), args, clock
        self.previous = None

    def check(self):
        require(self.path.is_absolute() and '..' not in self.path.parts, 'lease_path')
        with contextlib.ExitStack() as stack:
            parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            stack.callback(os.close, parent)
            for part in self.path.parts[1:-1]:
                parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                 os.O_CLOEXEC, dir_fd=parent)
                stack.callback(os.close, parent)
                info = os.fstat(parent)
                require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022,
                        'lease_directory_permissions')
            fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                         os.O_CLOEXEC, dir_fd=parent)
            stack.callback(os.close, fd)
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
                    and stat.S_IMODE(info.st_mode) == 0o644 and info.st_size <= 4096,
                    'lease_file_permissions')
            before = v.file_identity(fd)
            blob = os.read(fd, 4097)
            require(v.file_identity(fd) == before, 'lease_changed_during_read')
            live = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            require((live.st_dev, live.st_ino, live.st_size, live.st_mtime_ns, live.st_ctime_ns)
                    == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),
                    'lease_replaced_during_read')
            match = LEASE.fullmatch(blob.decode('ascii'))
            require(match is not None, 'lease_schema')
            state, boot, filesystem, generation, physical, sequence, expiry = match.groups()
            now = self.clock()
            require(state == 'ready' and boot == self.args.boot_id
                    and filesystem == self.args.filesystem_uuid and physical != 'none'
                    and now < int(expiry) <= now + 45 and int(sequence) <= 2**63 - 1,
                    'lease_not_ready_or_expired')
            current = dict(generation=generation, physical_generation=physical,
                           sequence=int(sequence), expires_uptime_seconds=int(expiry))
            if self.previous is not None:
                require(current['generation'] == self.previous['generation']
                        and current['physical_generation'] == self.previous['physical_generation']
                        and current['sequence'] >= self.previous['sequence'], 'lease_regression')
                require(current['sequence'] != self.previous['sequence']
                        or current == self.previous,
                        'lease_same_sequence_changed')
            self.previous = current
            return current


def link_from_fd(source, parent, name):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_int]
    libc.linkat.restype = ctypes.c_int
    # AT_EMPTY_PATH pins the inode already opened O_NOFOLLOW. No path fallback.
    if libc.linkat(source, b'', parent, os.fsencode(name), 0x1000) != 0:
        raise OSError(ctypes.get_errno(), 'linkat_failed')


def operations(plan):
    result = [dict(kind='mkdir', path=name) for name, value in plan['directories'].items()
              if value is None]
    result.sort(key=lambda row: (row['path'].count('/'), row['path']))
    result.extend(dict(kind='link', group=index, path=name)
                  for index, entry in enumerate(plan['groups']) for name in entry['publish'])
    return result


def setup_roots(args, stack, observe, cancelled):
    mount = v.open_directory(args.mountpoint)
    stack.callback(os.close, mount)
    stage = v.open_relative(mount, args.stage_relative, directory=True)
    stack.callback(os.close, stage)
    publication = v.open_relative(mount, args.publication_relative, directory=True)
    stack.callback(os.close, publication)
    guard = v.Guard(args, observe or (lambda: v.observe_mount(args)), mount, stage,
                    time.monotonic, cancelled)
    require('rw' in guard.initial['options'], 'writable_mount_required')
    require(v.identity(stage) != v.identity(publication), 'roots_overlap')
    a, b = v.components(args.stage_relative), v.components(args.publication_relative)
    require(a[:len(b)] != b and b[:len(a)] != a, 'roots_overlap')
    return mount, stage, publication, guard


def open_private_plan(path, mount, stack, *, create=False):
    path = Path(path)
    with owned(v.open_directory(path.parent)) as parent:
        v.private(parent, directory=True)
        require(os.fstat(parent).st_dev != os.fstat(mount).st_dev, 'plan_requires_separate_ssd')
        if create:
            os.mkdir(path.name, 0o700, dir_fd=parent)
            os.fsync(parent)
        output = v.open_relative(parent, path.name, directory=True)
    stack.callback(os.close, output)
    v.private(output, directory=True)
    lock = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                   0o600, dir_fd=output)
    stack.callback(os.close, lock)
    v.private(lock)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return output


CONFIG = ('mountpoint stage_relative publication_relative boot_id filesystem_uuid mount_id device '
          'directory_uid directory_gid directory_mode evidence_receipt_sha256 '
          'checkpoint_manifest_sha256 expected_groups expected_paths expected_bytes').split()


def make_plan(args, *, observe=None, cancelled=lambda: False):
    require(type(args.directory_uid) is int and args.directory_uid >= 0
            and type(args.directory_gid) is int and args.directory_gid >= 0
            and args.directory_mode == 0o755, 'new_directory_policy')
    with contextlib.ExitStack() as stack:
        mount, stage, publication, guard = setup_roots(args, stack, observe, cancelled)
        evidence = v.open_directory(args.evidence_directory)
        stack.callback(os.close, evidence)
        v.private(evidence, directory=True)
        baseline, _ = v.load_plan(args, evidence)
        selected, names, directories = [], set(), {'': directory_signature(publication)}
        excluded_paths = excluded_groups = excluded_bytes = 0
        for row in baseline:
            guard.check()
            publish = [p for p in row['group']['paths'] if v.components(p)[0] in ALLOW]
            excluded_paths += len(row['group']['paths']) - len(publish)
            if not publish:
                excluded_groups += 1
                excluded_bytes += row['expected']['size']
                continue
            current = v.matching_group(stage, row)
            selected.append(dict(id=row['id'], group=row['group'], publish=publish,
                                 current=current, sha256=row['sha256'],
                                 baseline_receipt_sha256=row['baseline_receipt_sha256']))
            for path in publish:
                require(path not in names, 'destination_mapping_collision')
                names.add(path)
                require(destination_signature(publication, path) is None, 'destination_collision')
                parts = v.components(path)
                for length in range(1, len(parts)):
                    parent = '/'.join(parts[:length])
                    if parent not in directories:
                        directories[parent] = directory_or_none(publication, parent)
        require(names and not (names & set(directories)), 'file_directory_collision')
        guard.check(force=True)
        config = {key: str(getattr(args, key)) if isinstance(getattr(args, key), Path)
                  else getattr(args, key) for key in CONFIG}
        summary = dict(selected_groups=len(selected), selected_paths=len(names),
                       selected_bytes=sum(row['current']['size'] for row in selected),
                       excluded_groups=excluded_groups, excluded_paths=excluded_paths,
                       excluded_bytes=excluded_bytes)
        plan = dict(version=1, config=config, codes=code_pins(), runtime=guard.initial,
                    bindings=guard.bindings, groups=selected, directories=directories,
                    summary=summary, allowlist=sorted(ALLOW))
        output = open_private_plan(args.plan_directory, mount, stack, create=True)
        v.atomic_json(output, 'plan.json', plan, exclusive=True)
        _, digest, _ = v.read_json(output, 'plan.json', maximum=v.MAX_JSON)
        return dict(phase='planned', plan_sha256=digest, **summary)


class Journal:
    """Single pending operation; hash chained append-only events, fsync each row."""

    def __init__(self, root, plan, plan_sha):
        self.plan, self.plan_sha = plan, plan_sha
        self.ops = operations(plan)
        self.current = [entry['current'] for entry in plan['groups']]
        self.directories = dict(plan['directories'])
        self.completed, self.pending, self.admission = 0, None, None
        self.sequence, self.previous = 0, '0' * 64
        self.fd = os.open('journal.jsonl', os.O_RDWR | os.O_APPEND | os.O_CREAT |
                          os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root)
        try:
            v.private(self.fd)
            require(os.fstat(self.fd).st_size <= MAX_JOURNAL, 'journal_size_limit')
            data = bytearray()
            while blob := os.read(self.fd, 1024**2):
                data.extend(blob)
                require(len(data) <= MAX_JOURNAL, 'journal_size_limit')
            require(not data or data.endswith(b'\n'), 'journal_incomplete_record')
            for line in data.splitlines():
                row = v.strict_json(line)
                require(isinstance(row, dict) and set(row) == {'body', 'sha256'}
                        and row['sha256'] == v.canonical(row['body']), 'journal_hash_mismatch')
                self.accept(row['body'], row['sha256'])
            os.fsync(root)
        except BaseException:
            os.close(self.fd)
            raise

    def accept(self, body, digest):
        require(isinstance(body, dict) and body.get('sequence') == self.sequence + 1
                and body.get('previous') == self.previous and body.get('plan') == self.plan_sha,
                'journal_chain_mismatch')
        lease = body.get('lease')
        require(isinstance(lease, dict) and set(lease) == {
            'generation', 'physical_generation', 'sequence', 'expires_uptime_seconds'}
            and re.fullmatch('[0-9a-f]{32}', lease['generation'])
            and v.valid_hash(lease['physical_generation'])
            and type(lease['sequence']) is int and 0 < lease['sequence'] <= 2**63 - 1,
            'journal_admission_schema')
        require(type(lease['expires_uptime_seconds']) is int
                and 0 < lease['expires_uptime_seconds'] < 10**16, 'journal_expiry_schema')
        if self.admission is not None:
            require(lease['generation'] == self.admission['generation']
                    and lease['physical_generation'] == self.admission['physical_generation']
                    and lease['sequence'] >= self.admission['sequence'], 'journal_lease_regression')
            require(lease['sequence'] != self.admission['sequence'] or lease == self.admission,
                    'journal_lease_same_sequence_changed')
        kind = body.get('event')
        if kind == 'admission':
            require(self.sequence == 0, 'journal_duplicate_admission')
        else:
            require(self.admission is not None and self.completed < len(self.ops)
                    and body.get('index') == self.completed, 'journal_operation_order')
            operation = self.ops[self.completed]
            if kind == 'intent':
                require(self.pending is None, 'journal_pending_operation')
                before = (self.current[operation['group']]
                          if operation['kind'] == 'link' else None)
                require(body.get('before') == before, 'journal_before_mismatch')
                self.pending = body
            elif kind == 'done':
                require(self.pending is not None, 'journal_done_without_intent')
                after = body.get('after')
                if operation['kind'] == 'link':
                    require(linked_signature(self.pending['before'], after),
                            'journal_after_mismatch')
                    self.current[operation['group']] = after
                else:
                    config = self.plan['config']
                    require(isinstance(after, dict) and after.get('device') ==
                            self.plan['directories']['']['device']
                            and after.get('uid') == config['directory_uid']
                            and after.get('gid') == config['directory_gid']
                            and after.get('mode') == config['directory_mode'],
                            'journal_directory_mismatch')
                    self.directories[operation['path']] = after
                self.completed += 1
                self.pending = None
            else:
                raise Hold('journal_event_schema')
        self.admission = lease
        self.sequence, self.previous = body['sequence'], digest

    def append(self, event, lease, **details):
        body = dict(sequence=self.sequence + 1, previous=self.previous, plan=self.plan_sha,
                    event=event, lease=lease, **details)
        digest = v.canonical(body)
        blob = json.dumps(dict(body=body, sha256=digest), sort_keys=True).encode() + b'\n'
        require(os.fstat(self.fd).st_size + len(blob) <= MAX_JOURNAL, 'journal_size_limit')
        remaining = memoryview(blob)
        while remaining:
            count = os.write(self.fd, remaining)
            require(count > 0, 'journal_write_failed')
            remaining = remaining[count:]
        os.fsync(self.fd)
        self.accept(body, digest)


def preflight(plan, journal, stage, publication, check):
    """Global collisions and current aliases checked before any new hardlink."""
    pending_op = journal.ops[journal.completed] if journal.pending else None
    completed_paths = {op['path'] for op in journal.ops[:journal.completed] if op['kind'] == 'link'}
    for path, expected in journal.directories.items():
        check()
        require(directory_or_none(publication, path) == expected, 'directory_changed_or_collision')
    for index, entry in enumerate(plan['groups']):
        check()
        current = group_signature(stage, entry)
        expected = journal.current[index]
        pending_here = (pending_op is not None and pending_op['kind'] == 'link'
                        and pending_op['group'] == index)
        pending_present = (destination_signature(publication, pending_op['path'])
                           if pending_here else None)
        if pending_present is not None:
            require(linked_signature(expected, current) and pending_present == current,
                    'pending_link_metadata_mismatch')
        else:
            require(current == expected, 'current_metadata_mismatch')
        for path in entry['publish']:
            observed = destination_signature(publication, path)
            if path in completed_paths or (pending_here and path == pending_op['path']
                                           and pending_present is not None):
                require(observed == current, 'owned_link_changed')
            else:
                require(observed is None, 'destination_collision')


def execute_pending(plan, journal, stage, publication, check, hook):
    operation = journal.ops[journal.completed]
    parts = v.components(operation['path'])
    parent_path = '/'.join(parts[:-1])
    with owned(directory(publication, parent_path)) as parent:
        require(directory_signature(parent) == journal.directories[parent_path], 'parent_changed')
        if operation['kind'] == 'mkdir':
            # An interrupted mkdir has no known pre-creation inode. Never adopt it.
            require(directory_or_none(publication, operation['path']) is None,
                    'ambiguous_directory_intent')
            check()
            os.mkdir(parts[-1], 0o700, dir_fd=parent)
            hook('created', operation)
            with owned(v.open_relative(parent, parts[-1], directory=True)) as created:
                os.fchown(created, plan['config']['directory_uid'], plan['config']['directory_gid'])
                os.fchmod(created, plan['config']['directory_mode'])
                os.fsync(created)
                after = directory_signature(created)
            os.fsync(parent)
        else:
            entry = plan['groups'][operation['group']]
            source_path = entry['group']['pool'] + '/' + entry['group']['paths'][0]
            with owned(v.open_relative(stage, source_path)) as source:
                before, current = journal.pending['before'], v.signature(source)
                dest = destination_signature(publication, operation['path'])
                if dest is None:
                    require(current == before, 'source_changed_before_link')
                    check()
                    link_from_fd(source, parent, parts[-1])
                    hook('linked', operation)
                else:
                    require(linked_signature(before, current) and dest == current,
                            'pending_link_not_owned')
                after = v.signature(source)
                require(linked_signature(before, after), 'link_metadata_mismatch')
                require(group_signature(stage, entry) == after, 'stage_changed_after_link')
                require(destination_signature(publication, operation['path']) == after,
                        'destination_changed_after_link')
                os.fsync(source)
                hook('source_fsync', operation)
                os.fsync(parent)
                hook('parent_fsync', operation)
                require(v.signature(source) == after, 'source_changed_during_fsync')
        lease = check()
        journal.append('done', lease, index=journal.completed, after=after)
        hook('done', operation)


def apply_plan(args, *, observe=None, boot_clock=None, cancelled=lambda: False,
               hook=lambda where, operation: None, reconcile=False):
    require(args.execute is True, 'explicit_execute_required')
    require(v.valid_hash(args.plan_sha256), 'plan_pin_required')
    with contextlib.ExitStack() as stack:
        # Open read-only first: the pinned plan supplies all runtime pins, not defaults.
        with owned(v.open_directory(args.plan_directory)) as initial:
            v.private(initial, directory=True)
            plan, _, _ = v.read_json(initial, 'plan.json', expected=args.plan_sha256,
                                     maximum=v.MAX_JSON)
        require(plan.get('version') == 1 and plan.get('codes') == code_pins()
                and plan.get('allowlist') == sorted(ALLOW), 'plan_schema_or_code_mismatch')
        config = argparse.Namespace(**plan['config'])
        mount, stage, publication, guard = setup_roots(config, stack, observe, cancelled)
        require(guard.initial == plan['runtime'] and guard.bindings == plan['bindings'],
                'planned_runtime_changed')
        output = open_private_plan(args.plan_directory, mount, stack)
        v.read_json(output, 'plan.json', expected=args.plan_sha256, maximum=v.MAX_JSON)
        journal = Journal(output, plan, args.plan_sha256)
        stack.callback(os.close, journal.fd)
        admission = Admission(args.lease_path, config, boot_clock or (
            lambda: time.clock_gettime(time.CLOCK_BOOTTIME)))
        admission.previous = journal.admission

        def check():
            guard.check(force=True)
            require(directory_signature(publication) == plan['directories'][''],
                    'publication_root_changed')
            fresh_fd = v.open_relative(mount, config.publication_relative, directory=True)
            with owned(fresh_fd) as fresh:
                require(v.identity(fresh) == v.identity(publication), 'publication_root_replaced')
            return admission.check()

        lease = check()
        preflight(plan, journal, stage, publication, check)
        if journal.admission is None:
            journal.append('admission', lease)
        if journal.pending:
            execute_pending(plan, journal, stage, publication, check, hook)
        links = 0
        while not reconcile and journal.completed < len(journal.ops):
            operation = journal.ops[journal.completed]
            if operation['kind'] == 'link' and links >= args.max_links:
                break
            lease = check()
            before = (journal.current[operation['group']] if operation['kind'] == 'link' else None)
            journal.append('intent', lease, index=journal.completed, before=before)
            hook('intent', operation)
            execute_pending(plan, journal, stage, publication, check, hook)
            links += operation['kind'] == 'link'
        preflight(plan, journal, stage, publication, check)
        complete = journal.completed == len(journal.ops)
        result = dict(version=1,
                      phase='publication_complete' if complete else 'publication_partial',
                      plan_sha256=args.plan_sha256, journal_head=journal.previous,
                      journal_events=journal.sequence, published_paths=sum(
                          op['kind'] == 'link' for op in journal.ops[:journal.completed]),
                      summary=plan['summary'],
                      basis='authenticated_historical_hashes_and_current_metadata',
                      current_content_hash_verified=False, degraded_published=False,
                      all_media_ready=False, stage_is_immutable_backup=False)
        if complete:
            # Existing success is never overwritten. A later rerun still validates every alias.
            try:
                previous, _, _ = v.read_json(output, 'publication-receipt.json')
            except FileNotFoundError:
                check()
                v.atomic_json(output, 'publication-receipt.json', result, exclusive=True)
            else:
                require(previous == result, 'completion_receipt_changed')
        return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='command', required=True)
    planning = sub.add_parser('plan', help='metadata-only preflight and new private SSD plan')
    for option in ('mountpoint', 'stage-relative', 'publication-relative', 'boot-id',
                   'filesystem-uuid', 'device', 'evidence-directory', 'evidence-receipt-sha256',
                   'checkpoint-manifest-sha256', 'plan-directory'):
        planning.add_argument('--' + option, required=True)
    for option in ('mount-id', 'expected-groups', 'expected-paths', 'expected-bytes',
                   'directory-uid', 'directory-gid'):
        planning.add_argument('--' + option, type=int, required=True)
    planning.add_argument('--directory-mode', type=lambda s: int(s, 8), default=0o755)
    for command in ('apply', 'reconcile'):
        applying = sub.add_parser(command)
        applying.add_argument('--plan-directory', required=True)
        applying.add_argument('--plan-sha256', required=True)
        applying.add_argument('--lease-path',
                              default='/var/lib/homelab-storage/core-writer-admission/lease')
        applying.add_argument('--execute', action='store_true')
        applying.add_argument('--max-links', type=int, default=50_000)
    return result


def main():
    args = parser().parse_args()
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        if args.command == 'plan':
            result = make_plan(args, cancelled=lambda: stopped)
        else:
            require(0 < args.max_links <= 50_000, 'max_links_bounds')
            result = apply_plan(args, cancelled=lambda: stopped,
                                reconcile=args.command == 'reconcile')
        print(json.dumps(result, sort_keys=True))
        return 0
    except Hold as exc:
        print(json.dumps({'phase': 'HOLD', 'reason': str(exc)}))
        return 2
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        # Private plans/journals contain titles; stdout never includes paths or exception strings.
        print(json.dumps({'phase': 'HOLD', 'reason': 'publication_precondition_or_io_failed'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
