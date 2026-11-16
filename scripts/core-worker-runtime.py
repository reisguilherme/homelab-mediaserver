#!/usr/bin/env python3
"""Fixed worker and UID-1000 capacity child inside the admitted process group."""

import argparse
import contextlib
import importlib.util
import json
import os
import re
import select
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

from homeserver_common.storage import load_storage_registry


def helper(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = helper('worker_capacity_guard', 'core-storage-guard.py')
capacity = helper('worker_capacity_snapshot', 'capacity-snapshot.py')
REGISTRY_PATH = Path('/run/homeserver/storage.json')
OUTPUT_DIRECTORY = Path('/run/core-capacity')
OUTPUT_NAME = 'capacity.json'
INTERVAL = 5
MAX_SAMPLE_WAIT = 10
WORKER_COMMAND = ['/app/.venv/bin/python', '-m', 'homeserver_control.worker']
COLLECTOR_COMMAND = ['/app/.venv/bin/python', '/app/scripts/core-worker-runtime.py', '--collector']


def collector_environment(environment):
    expected_uuid = environment.get('HOMESERVER_MEDIA_UUID', '')
    guard.require(isinstance(expected_uuid, str)
                  and re.fullmatch(guard.UUID, expected_uuid), 'capacity_uuid')
    return {
        'PATH': '/app/.venv/bin:/usr/local/bin:/usr/bin:/bin',
        'PYTHONPATH': '/app/services/common/src', 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONUNBUFFERED': '1', 'HOME': '/tmp', 'TMPDIR': '/tmp', 'LANG': 'C.UTF-8',
        'HOMESERVER_MEDIA_UUID': expected_uuid,
    }


def open_output_directory():
    fd = os.open(OUTPUT_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    info = os.fstat(fd)
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (1000, 1000, 0o750):
        os.close(fd)
        raise guard.GuardError('capacity_directory_identity')
    return fd


def invalidate(fd):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(OUTPUT_NAME, dir_fd=fd)
    os.fsync(fd)


def invalidate_output():
    fd = open_output_directory()
    try:
        invalidate(fd)
    finally:
        os.close(fd)


def collect_once(fd, expected_uuid, previous=None):
    temporary = '.capacity-' + uuid.uuid4().hex
    try:
        before = guard.check_runtime(expected_uuid, 'rw')
        if previous is not None:
            before = guard.accept_update(previous, before)
        registry = load_storage_registry(REGISTRY_PATH, required_backend='ext4-single')
        guard.require(registry is not None
                      and registry.primary_pool.filesystem_id == expected_uuid, 'capacity_registry')
        payload = capacity.collect_pools(registry)
        rows = payload.get('pools', [])
        guard.require(len(rows) == 1 and rows[0].get('pool_id') == 'hdd3', 'capacity_pool')
        for row in [payload, *rows]:
            guard.require(row.get('filesystem_id') == expected_uuid and row.get('state') == 'ready'
                          and type(row.get('total_bytes')) is int and row['total_bytes'] > 0
                          and type(row.get('free_bytes')) is int
                          and 0 <= row['free_bytes'] <= row['total_bytes'], 'capacity_sample')
        current = guard.accept_update(before, guard.check_runtime(expected_uuid, 'rw'))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=fd)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(payload, output, sort_keys=True, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        guard.valid_snapshot(current)
        os.replace(temporary, OUTPUT_NAME, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
        guard.valid_snapshot(current)
        return current
    except BaseException:
        invalidate(fd)
        raise
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=fd)


def collector_loop(expected_uuid):
    guard.require((os.geteuid(), os.getegid(), os.getgroups()) == (1000, 1000, []),
                  'capacity_identity')
    fd = open_output_directory()
    previous = None
    try:
        invalidate(fd)
        while True:
            started = guard.uptime_clock()
            previous = collect_once(fd, expected_uuid, previous)
            # Private IPC acknowledgement follows durable publication, never precedes it.
            os.write(sys.stdout.fileno(), b'.')
            time.sleep(max(0, INTERVAL - (guard.uptime_clock() - started)))
    finally:
        invalidate(fd)
        os.close(fd)


def run_group(environment):
    guard.require(os.geteuid() == os.getegid() == 0
                  and os.getpid() == os.getpgrp() == os.getsid(0), 'worker_group_identity')
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    saved = {signum: signal.signal(signum, stop) for signum in (signal.SIGTERM, signal.SIGINT)}
    collector = None
    worker = None
    try:
        invalidate_output()
        collector = subprocess.Popen(COLLECTOR_COMMAND, stdout=subprocess.PIPE, bufsize=0,
                                     user=1000, group=1000, extra_groups=(),
                                     env=collector_environment(environment), cwd='/app')
        deadline = guard.uptime_clock() + MAX_SAMPLE_WAIT
        while not stopped:
            if collector.poll() is not None or (worker is not None and worker.poll() is not None):
                return 78
            guard.require(guard.uptime_clock() < deadline, 'capacity_timeout')
            ready, _, _ = select.select([collector.stdout], [], [], 0.1)
            if ready:
                acknowledgement = os.read(collector.stdout.fileno(), 64)
                guard.require(acknowledgement and set(acknowledgement) == {ord('.')},
                              'capacity_protocol')
                guard.require(guard.uptime_clock() < deadline, 'capacity_timeout')
                deadline = guard.uptime_clock() + MAX_SAMPLE_WAIT
                if worker is None:
                    worker = subprocess.Popen(WORKER_COMMAND, env=environment, cwd='/app')
        return 0
    finally:
        # The outer guard pins this group leader until every descendant is stopped.
        # It performs TERM/KILL verification even if this wrapper exits first.
        for signum, handler in saved.items():
            signal.signal(signum, handler)
        if collector is not None and collector.stdout is not None:
            collector.stdout.close()
        invalidate_output()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--collector', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.collector:
            collector_loop(collector_environment(os.environ)['HOMESERVER_MEDIA_UUID'])
            return 0
        return run_group(dict(os.environ))
    except (guard.GuardError, OSError, RuntimeError, ValueError, TypeError):
        print('core worker capacity unavailable', file=sys.stderr)
        return 78


if __name__ == '__main__':
    sys.exit(main())
