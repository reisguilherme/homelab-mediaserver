#!/usr/bin/env python3
"""Hash authenticated healthy consolidation groups on the destination only.

Linux only. No source-pool reads, media writes, fsync on media, application
actions or publication. New progress/results live on a separate private SSD.
Historical identities never substitute for current reads. Matching healthy
baselines excludes degraded content and partials and does not make media ready.
Signals cooperate between reads; kernel D-state cannot have a guaranteed bound.
"""

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import time
import uuid
from pathlib import Path

MIB = 1024**2
CHUNK = 4 * MIB
MAX_JSON = 16 * MIB
MAX_RECEIPT = MIB
MAX_EVIDENCE_BYTES = 64 * MIB
MAX_GROUPS = 10_000
MAX_PATHS = 50_000
CACHE = 'healthy-content-cache'
HEALTHY_KEYS = set(('cache_manifest_sha256 context destination_fsynced group identities owner '
                    'sha256 source source_filesystem_id source_sha256 stage_bindings target '
                    'target_filesystem_id target_sha256 verified_at version').split())
SIGNATURE_KEYS = set(
    'allocated ctime_ns device gid inode mode mtime_ns nlink size uid xattrs'.split())
COMPARE = ('size', 'mtime_ns', 'mode', 'uid', 'gid', 'nlink', 'xattrs')


class Hold(RuntimeError):
    """Only fixed, non-sensitive reason codes."""


class BatchLimit(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Hold(code)


def sha256(blob):
    return hashlib.sha256(blob).hexdigest()


def canonical(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def components(value):
    require(isinstance(value, str) and value and '\x00' not in value, 'unsafe_path')
    parts = value.split('/')
    require(not any(part in ('', '.', '..', '.rsync-partial') for part in parts), 'unsafe_path')
    return parts


def open_directory(path):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'absolute_path_required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = following
        return fd
    except BaseException:
        os.close(fd)
        raise


def open_relative(root, name, *, directory=False):
    parts = components(name)
    device = os.fstat(root).st_dev
    parent = os.dup(root)
    try:
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                os.O_CLOEXEC, dir_fd=parent)
            os.close(parent)
            parent = following
            require(os.fstat(parent).st_dev == device, 'nested_filesystem')
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        flags |= os.O_DIRECTORY if directory else os.O_NOATIME
        result = os.open(parts[-1], flags, dir_fd=parent)
        try:
            info = os.fstat(result)
            require(info.st_dev == device, 'nested_filesystem')
            require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
                    'unexpected_file_type')
            return result
        except BaseException:
            os.close(result)
            raise
    finally:
        os.close(parent)


def identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino]


def file_identity(fd):
    info = os.fstat(fd)
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_mode, info.st_uid, info.st_gid, info.st_nlink]


def private(fd, *, directory=False):
    info = os.fstat(fd)
    require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and info.st_uid == os.geteuid() and not stat.S_IMODE(info.st_mode) & 0o077
            and (directory or info.st_nlink == 1), 'private_path_required')


def strict_json(blob):
    def pairs(values):
        result = {}
        for key, value in values:
            require(key not in result, 'duplicate_json_key')
            result[key] = value
        return result

    def constant(_):
        raise Hold('invalid_json_number')

    value = json.loads(blob, object_pairs_hook=pairs, parse_constant=constant)
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        require(depth <= 32 and count <= 200_000, 'json_structure_limit')
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, float):
            require(math.isfinite(item), 'invalid_json_number')
    return value


def read_json(root, name, *, expected=None, maximum=MAX_RECEIPT):
    fd = open_relative(root, name)
    try:
        private(fd)
        before = file_identity(fd)
        require(before[2] <= maximum, 'json_size_limit')
        blocks, length = [], 0
        while block := os.read(fd, min(MIB, maximum + 1 - length)):
            blocks.append(block)
            length += len(block)
            require(length <= maximum, 'json_size_limit')
        require(file_identity(fd) == before, 'evidence_changed')
        blob = b''.join(blocks)
        digest = sha256(blob)
        require(expected is None or digest == expected, 'evidence_hash_or_pin_mismatch')
        return strict_json(blob), digest, before
    finally:
        os.close(fd)


def validate_signature(row):
    require(isinstance(row, dict) and set(row) == SIGNATURE_KEYS, 'signature_schema')
    for name in SIGNATURE_KEYS - {'xattrs'}:
        require(type(row[name]) is int and row[name] >= 0, 'signature_integer')
    require(row['mode'] <= 0o7777 and 0 < row['nlink'] <= 1024, 'signature_metadata')
    attrs = row['xattrs']
    require(isinstance(attrs, dict) and len(attrs) <= 256
            and all(isinstance(k, str) and k and '\x00' not in k and valid_hash(v)
                    for k, v in attrs.items()), 'signature_attributes')


def load_plan(args, evidence):
    extraction, _, extraction_identity = read_json(
        evidence, 'restore-receipt.json', expected=args.evidence_receipt_sha256, maximum=MAX_JSON)
    require(isinstance(extraction, dict) and extraction.get('version') == 1
            and extraction.get('complete') is True
            and extraction.get('file_hashes_verified') is True, 'extraction_receipt_schema')
    require(extraction.get('manifest_sha256') == args.checkpoint_manifest_sha256,
            'checkpoint_manifest_pin_mismatch')
    expected_prefix = ('var/lib/homeserver-usb-recovery/incidents/'
                       + args.stage_relative.split('/')[-1])
    require(extraction.get('source_prefix') == expected_prefix, 'incident_scope_mismatch')
    files = extraction.get('files')
    require(isinstance(files, dict) and len(files) <= 20_000, 'evidence_index_schema')
    identities = {'restore-receipt.json': extraction_identity}
    evidence_bytes = 0

    def authenticated(name):
        nonlocal evidence_bytes
        row = files.get(name)
        require(isinstance(row, dict) and type(row.get('bytes')) is int
                and row['bytes'] >= 0 and valid_hash(row.get('sha256')), 'evidence_index_schema')
        evidence_bytes += row['bytes']
        require(evidence_bytes <= MAX_EVIDENCE_BYTES, 'evidence_byte_limit')
        value, digest, observed = read_json(evidence, name, expected=row['sha256'])
        require(observed[2] == row['bytes'], 'evidence_size_mismatch')
        identities[name] = observed
        return value, digest

    manifest, manifest_hash = authenticated(CACHE + '/manifest.json')
    require(isinstance(manifest, dict) and type(manifest.get('version')) is int
            and manifest['version'] == 1 and isinstance(manifest.get('owner'), str)
            and isinstance(manifest.get('pins'), dict)
            and manifest.get('context') == canonical(manifest['pins'])
            and valid_hash(manifest.get('producer_sha256')), 'cache_manifest_schema')
    plan, seen_paths, total_bytes = [], set(), 0
    prefix = CACHE + '/receipts/'
    for name in sorted(n for n in files if n.startswith(prefix)):
        require(re.fullmatch(re.escape(prefix) + r'[0-9a-f]{64}\.json', name), 'receipt_name')
        row, digest = authenticated(name)
        require(isinstance(row, dict) and set(row) == HEALTHY_KEYS, 'healthy_receipt_schema')
        require(type(row['version']) is int and row['version'] == 1
                and row['owner'] == manifest['owner'] and row['context'] == manifest['context']
                and row['cache_manifest_sha256'] == manifest_hash
                and row['destination_fsynced'] is True, 'historical_context_mismatch')
        require(valid_hash(row['sha256'])
                and row['sha256'] == row['source_sha256'] == row['target_sha256'],
                'historical_hash_mismatch')
        require(isinstance(row['identities'], dict) and isinstance(row['stage_bindings'], dict)
                and isinstance(row['source_filesystem_id'], str)
                and isinstance(row['target_filesystem_id'], str)
                and type(row['verified_at']) in (int, float)
                and math.isfinite(row['verified_at']), 'historical_metadata_schema')
        group = row['group']
        require(isinstance(group, dict) and set(group) == {'pool', 'paths'}
                and group['pool'] in ('ssd', 'hdd', 'hdd2'), 'group_schema')
        paths = group['paths']
        require(isinstance(paths, list) and 0 < len(paths) <= 1024
                and all(isinstance(p, str) for p in paths)
                and paths == sorted(set(paths)), 'group_paths')
        for path in paths:
            components(path)
            key = (group['pool'], path)
            require(key not in seen_paths, 'duplicate_group_path')
            seen_paths.add(key)
        group_id = canonical(group)
        require(name == prefix + group_id + '.json', 'group_digest_mismatch')
        validate_signature(row['source'])
        validate_signature(row['target'])
        require(row['target']['nlink'] == len(paths)
                and all(row['source'][key] == row['target'][key]
                        for key in COMPARE if key != 'nlink'), 'historical_group_metadata')
        total_bytes += row['target']['size']
        require(len(plan) < MAX_GROUPS and len(seen_paths) <= MAX_PATHS, 'plan_size_limit')
        plan.append(dict(id=group_id, group=group, expected=row['target'], sha256=row['sha256'],
                         baseline_receipt_sha256=digest))
    require(len(plan) == args.expected_groups and len(seen_paths) == args.expected_paths
            and total_bytes == args.expected_bytes, 'healthy_coverage_mismatch')
    plan.sort(key=lambda row: (row['expected']['size'], row['id']))
    return plan, identities


def parse_mountinfo(text, mountpoint):
    def unescape(value):
        return re.sub(r'\\(040|011|012|134)', lambda match: chr(int(match[1], 8)), value)

    rows = []
    for line in text.splitlines():
        left, separator, right = line.partition(' - ')
        fields, filesystem = left.split(), right.split()
        require(separator and len(fields) >= 6 and len(filesystem) >= 3, 'mountinfo_schema')
        candidate = unescape(fields[4])
        require(not candidate.startswith(str(mountpoint).rstrip('/') + '/'), 'nested_mount')
        if candidate == str(mountpoint):
            rows.append(dict(mount_id=int(fields[0]), device=fields[2], root=unescape(fields[3]),
                             options=sorted(fields[5].split(',')), fstype=filesystem[0]))
    require(len(rows) == 1, 'exact_mount_required')
    return rows[0]


def observe_mount(args):
    row = parse_mountinfo(Path('/proc/self/mountinfo').read_text(), args.mountpoint)
    row['boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    row['namespace'] = os.readlink('/proc/self/ns/mnt')
    require(row['namespace'] == os.readlink('/proc/1/ns/mnt'), 'mount_namespace_mismatch')
    # stat resolves the UUID link; no block device is opened or read.
    device = os.stat('/dev/disk/by-uuid/' + args.filesystem_uuid)
    require(stat.S_ISBLK(device.st_mode)
            and f'{os.major(device.st_rdev)}:{os.minor(device.st_rdev)}' == row['device'],
            'uuid_device_mismatch')
    row['filesystem_uuid'] = args.filesystem_uuid
    return row


class Guard:
    def __init__(self, args, observe, mount, stage, clock, cancelled):
        self.args, self.observe, self.mount, self.stage = args, observe, mount, stage
        self.clock, self.cancelled = clock, cancelled
        self.initial = observe()
        for key in ('boot_id', 'filesystem_uuid', 'mount_id', 'device'):
            require(self.initial.get(key) == getattr(args, key), 'runtime_pin_mismatch')
        require(self.initial.get('root') == '/' and self.initial.get('fstype') == 'ext4'
                and 'noatime' in self.initial.get('options', []), 'runtime_mount_policy')
        self.bindings = dict(mount=identity(mount), stage=identity(stage))
        major, minor = map(int, args.device.split(':'))
        require(os.fstat(mount).st_dev == os.makedev(major, minor), 'runtime_device_mismatch')
        self.last = float('-inf')
        # The per-batch budget begins after bounded evidence preflight/session admission.
        self.deadline = float('inf')

    def check(self, *, force=False):
        require(not self.cancelled(), 'cancelled')
        now = self.clock()
        if now >= self.deadline:
            raise BatchLimit()
        if not force and now - self.last < 1:
            return
        require(self.observe() == self.initial, 'runtime_changed')
        mount = open_directory(self.args.mountpoint)
        try:
            stage = open_relative(mount, self.args.stage_relative, directory=True)
            try:
                require(identity(mount) == identity(self.mount) == self.bindings['mount']
                        and identity(stage) == identity(self.stage) == self.bindings['stage'],
                        'runtime_root_changed')
            finally:
                os.close(stage)
        finally:
            os.close(mount)
        self.last = now


def signature(fd):
    before = os.fstat(fd)
    require(stat.S_ISREG(before.st_mode), 'regular_file_required')
    attrs = {key: sha256(os.getxattr(fd, key)) for key in sorted(os.listxattr(fd))}
    after = os.fstat(fd)
    require((before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'metadata_changed')
    return dict(device=after.st_dev, inode=after.st_ino, size=after.st_size,
                mtime_ns=after.st_mtime_ns, ctime_ns=after.st_ctime_ns,
                mode=stat.S_IMODE(after.st_mode), uid=after.st_uid, gid=after.st_gid,
                nlink=after.st_nlink, xattrs=attrs)


def matching_group(stage, entry):
    observed = None
    for path in entry['group']['paths']:
        fd = open_relative(stage, entry['group']['pool'] + '/' + path)
        try:
            current = signature(fd)
            require(all(current[key] == entry['expected'][key] for key in COMPARE),
                    'target_metadata_mismatch')
            require(observed is None or current == observed, 'alias_identity_changed')
            observed = current
        finally:
            os.close(fd)
    return observed


def atomic_json(directory, name, value, *, exclusive=False):
    temporary = '.write-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                 os.O_CLOEXEC, 0o600, dir_fd=directory)
    published = False
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(json.dumps(value, sort_keys=True).encode() + b'\n')
            output.flush()
            os.fsync(output.fileno())
        if exclusive:
            os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
            published = True
            os.unlink(temporary, dir_fd=directory)
        else:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    except BaseException:
        if published:
            os.unlink(name, dir_fd=directory)
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def output_session(args, mount, evidence, stack):
    path = Path(args.output_directory).absolute()
    require(not path.is_relative_to(Path(args.evidence_directory).absolute()),
            'output_inside_evidence')
    parent = open_directory(path.parent)
    stack.callback(os.close, parent)
    require(os.fstat(parent).st_dev != os.fstat(mount).st_dev, 'output_filesystem')
    private(parent, directory=True)
    exists = path.exists() or path.is_symlink()
    require(args.resume or not exists, 'output_exists')
    require(exists or not args.resume, 'resume_output_missing')
    if not exists:
        os.mkdir(path.name, 0o700, dir_fd=parent)
        os.fsync(parent)
    output = open_relative(parent, path.name, directory=True)
    stack.callback(os.close, output)
    private(output, directory=True)
    require(identity(output) != identity(evidence), 'output_inside_evidence')
    lock = os.open('session.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK |
                   os.O_CLOEXEC, 0o600, dir_fd=output)
    stack.callback(os.close, lock)
    private(lock)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise Hold('session_busy') from exc
    if not exists:
        os.mkdir('results', 0o700, dir_fd=output)
        os.fsync(output)
    results = open_relative(output, 'results', directory=True)
    stack.callback(os.close, results)
    private(results, directory=True)
    return output, results


def validate_options(args):
    require(hasattr(os, 'O_NOATIME'), 'linux_noatime_required')
    require(valid_hash(args.evidence_receipt_sha256)
            and valid_hash(args.checkpoint_manifest_sha256), 'invalid_pin')
    for value in (args.boot_id, args.filesystem_uuid):
        require(isinstance(value, str)
                and re.fullmatch('[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', value),
                'invalid_runtime_pin')
    require(re.fullmatch('[0-9]+:[0-9]+', args.device) and args.mount_id > 0, 'invalid_runtime_pin')
    parts = components(args.stage_relative)
    require(len(parts) == 2 and parts[0] == '.homeserver-consolidation', 'stage_scope')
    require(0 < args.expected_groups <= MAX_GROUPS and 0 < args.expected_paths <= MAX_PATHS
            and args.expected_bytes >= 0, 'invalid_coverage')
    require(1 <= args.max_groups <= 1024 and 0 < args.max_bytes <= 1024**4
            and 1 <= args.rate_mib <= 128 and 0 < args.max_seconds <= 86400,
            'invalid_batch_budget')


def run_batch(args, *, observe=None, clock=time.monotonic, sleep=time.sleep,
              cancelled=lambda: False):
    validate_options(args)
    with contextlib.ExitStack() as stack:
        mount = open_directory(args.mountpoint)
        stack.callback(os.close, mount)
        stage = open_relative(mount, args.stage_relative, directory=True)
        stack.callback(os.close, stage)
        guard = Guard(args, observe or (lambda: observe_mount(args)), mount, stage,
                      clock, cancelled)
        guard.check(force=True)
        evidence = open_directory(args.evidence_directory)
        stack.callback(os.close, evidence)
        private(evidence, directory=True)
        require(os.fstat(evidence).st_dev != os.fstat(mount).st_dev, 'evidence_filesystem')
        plan, evidence_identities = load_plan(args, evidence)
        guard.check(force=True)
        output, results = output_session(args, mount, evidence, stack)
        configuration = dict(version=1, evidence_receipt_sha256=args.evidence_receipt_sha256,
                             checkpoint_manifest_sha256=args.checkpoint_manifest_sha256,
                             verifier_sha256=sha256(Path(__file__).read_bytes()),
                             runtime=guard.initial, bindings=guard.bindings,
                             evidence_binding=identity(evidence), output_binding=identity(output),
                             stage_relative=args.stage_relative,
                             healthy_groups=len(plan), healthy_paths=args.expected_paths,
                             healthy_bytes=args.expected_bytes,
                             order='size_ascending_then_group_digest',
                             order_sha256=canonical([row['id'] for row in plan]))
        context = canonical(configuration)
        session = dict(context=context, configuration=configuration)
        if args.resume:
            previous, _, _ = read_json(output, 'session.json', maximum=MAX_JSON)
            require(previous == session, 'session_context_changed')
        else:
            atomic_json(output, 'session.json', session, exclusive=True)
        guard.deadline = clock() + args.max_seconds
        done, active, active_bytes = {}, None, 0
        batch_bytes, batch_groups = 0, 0
        next_progress = clock()
        rate_deadline = clock()

        def summary(phase, reason=None):
            next_entry = next((entry for entry in plan if entry['id'] not in done), None)
            return dict(version=1, phase=phase, context=context, reason=reason,
                        batch_complete=phase in ('batch_limited', 'healthy_scope_complete'),
                        healthy_complete=phase == 'healthy_scope_complete',
                        verified_groups=len(done),
                        verified_paths=sum(len(row['group']['paths']) for row in done.values()),
                        verified_bytes=sum(row['before']['size'] for row in done.values()),
                        batch_verified_groups=batch_groups, batch_read_bytes=batch_bytes,
                        expected_groups=len(plan), expected_paths=args.expected_paths,
                        expected_bytes=args.expected_bytes,
                        active_group=active, active_read_bytes=active_bytes,
                        next_group_id=next_entry['id'] if next_entry else None,
                        next_group_bytes=next_entry['expected']['size'] if next_entry else None,
                        baseline_kind='healthy', degraded_content_verified=False,
                        partials_in_scope=False, media_ready=False, published=False,
                        updated_at=time.time())

        def persist(phase, reason=None):
            value = summary(phase, reason)
            atomic_json(output, 'progress.json', value)
            return value

        def throttle(length):
            nonlocal rate_deadline
            rate_deadline = max(clock(), rate_deadline) + length / (args.rate_mib * MIB)
            while clock() < rate_deadline:
                guard.check()
                remaining = rate_deadline - clock()
                if remaining > 0:
                    sleep(min(0.25, remaining))
            guard.check()

        def check_evidence():
            for name, expected in evidence_identities.items():
                fd = open_relative(evidence, name)
                try:
                    require(file_identity(fd) == expected, 'evidence_changed')
                finally:
                    os.close(fd)

        try:
            names = set(os.listdir(results))
            require(names <= {entry['id'] + '.json' for entry in plan}, 'unexpected_result_file')
            for entry in plan:
                if entry['id'] + '.json' not in names:
                    continue
                row, _, _ = read_json(results, entry['id'] + '.json')
                require(isinstance(row, dict) and row.get('context') == context
                        and row.get('group') == entry['group']
                        and row.get('baseline_receipt_sha256') == entry['baseline_receipt_sha256']
                        and row.get('sha256') == entry['sha256']
                        and row.get('fresh_destination_read') is True
                        and row.get('baseline_kind') == 'healthy'
                        and row.get('before') == row.get('after'), 'current_receipt_invalid')
                require(matching_group(stage, entry) == row['after'], 'completed_group_changed')
                done[entry['id']] = row
                guard.check()
            persist('hashing')
            for entry in plan:
                if entry['id'] in done:
                    continue
                guard.check(force=True)
                size = entry['expected']['size']
                if batch_groups >= args.max_groups or batch_bytes + size > args.max_bytes:
                    require(batch_groups > 0 or size <= args.max_bytes,
                            'group_exceeds_batch_budget')
                    break
                active, active_bytes = entry['id'], 0
                persist('hashing')
                before = matching_group(stage, entry)
                path = entry['group']['pool'] + '/' + entry['group']['paths'][0]
                fd = open_relative(stage, path)
                started_at = time.time()
                try:
                    require(signature(fd) == before, 'target_changed_before_hash')
                    digest = hashlib.sha256()
                    while True:
                        guard.check()
                        block = os.read(fd, min(CHUNK, size - active_bytes + 1))
                        if not block:
                            break
                        active_bytes += len(block)
                        batch_bytes += len(block)
                        require(active_bytes <= size, 'content_size_changed')
                        digest.update(block)
                        throttle(len(block))
                        if clock() >= next_progress:
                            persist('hashing')
                            next_progress = clock() + 1
                    after = signature(fd)
                    require(after == before, 'target_changed_during_hash')
                    require(active_bytes == size and digest.hexdigest() == entry['sha256'],
                            'content_mismatch')
                finally:
                    os.close(fd)
                require(matching_group(stage, entry) == before, 'alias_identity_changed')
                guard.check(force=True)
                row = dict(version=1, context=context, group=entry['group'],
                           baseline_kind='healthy', fresh_destination_read=True,
                           baseline_receipt_sha256=entry['baseline_receipt_sha256'],
                           sha256=digest.hexdigest(), before=before, after=after,
                           read_started_at=started_at, read_finished_at=time.time(),
                           degraded_content_verified=False, media_ready=False, published=False)
                atomic_json(results, entry['id'] + '.json', row, exclusive=True)
                done[entry['id']] = row
                batch_groups += 1
                active, active_bytes = None, 0
                persist('hashing')
            # No final scope claim unless all current aliases still match the
            # new receipts, all pinned evidence is unchanged and runtime agrees.
            for entry in plan:
                if entry['id'] in done:
                    require(matching_group(stage, entry) == done[entry['id']]['after'],
                            'completed_group_changed')
                    guard.check()
            check_evidence()
            guard.check(force=True)
            return persist('healthy_scope_complete' if len(done) == len(plan) else 'batch_limited')
        except BatchLimit:
            return persist('batch_limited', 'time_budget')
        except (Hold, OSError) as exc:
            persist('hold', str(exc) if isinstance(exc, Hold) else 'input_or_filesystem_error')
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('evidence-directory', 'output-directory', 'mountpoint'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('evidence-receipt-sha256', 'checkpoint-manifest-sha256', 'stage-relative',
                 'boot-id', 'filesystem-uuid', 'device'):
        parser.add_argument('--' + name, required=True)
    for name in ('mount-id', 'expected-groups', 'expected-paths', 'expected-bytes'):
        parser.add_argument('--' + name, type=int, required=True)
    parser.add_argument('--max-groups', type=int, default=32)
    parser.add_argument('--max-bytes', type=int, default=64 * 1024**3)
    parser.add_argument('--rate-mib', type=int, default=64)
    parser.add_argument('--max-seconds', type=int, default=1800)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    cancelled = False

    def cancel(*_):
        nonlocal cancelled
        cancelled = True

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, cancel)
    try:
        result = run_batch(args, cancelled=lambda: cancelled)
    except (Hold, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        reason = str(exc) if isinstance(exc, Hold) else 'input_or_filesystem_error'
        print(json.dumps(dict(phase='hold', batch_complete=False, healthy_complete=False,
                              reason=reason, media_ready=False)), flush=True)
        return 2
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
