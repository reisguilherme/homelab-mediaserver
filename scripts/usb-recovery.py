#!/usr/bin/env python3
"""Root entry point for the opt-in USB recovery cron and manual maintenance."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
import syslog
from pathlib import Path

INSTALLED = Path('/usr/local/lib/homeserver-usb-recovery')
STATE = Path('/var/lib/homeserver-usb-recovery')
CONFIG = Path('/etc/homeserver-usb-recovery.json')


def trusted(path, *, directory=False):
    for current in (path, *path.parents):
        info = current.lstat()
        if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o022):
            raise ValueError('untrusted_recovery_path')
    info = path.stat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode):
        raise ValueError('invalid_recovery_path_type')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--once', action='store_true')
    actions.add_argument('--status', action='store_true')
    actions.add_argument('--disable', action='store_true')
    actions.add_argument('--enable', action='store_true')
    parser.add_argument('--recover', action='append', choices=['hdd', 'hdd2', 'hdd3'], default=[])
    args = parser.parse_args(argv)
    if args.recover and (args.status or args.disable or args.enable):
        parser.error('--recover requires --once')
    if (os.geteuid() != 0
            or os.readlink('/proc/self/ns/mnt') != os.readlink('/proc/1/ns/mnt')):
        raise ValueError('recovery_requires_host_root')
    trusted(STATE, directory=True)
    if STATE.stat().st_mode & 0o077:
        raise ValueError('recovery_private_permissions')
    disabled = STATE / 'disabled'
    lock = STATE / 'lock'
    if lock.exists():
        trusted(lock)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'phase': 'busy'}))
            return 0
        trusted(INSTALLED, directory=True)
        trusted(CONFIG)
        if CONFIG.stat().st_mode & 0o077:
            raise ValueError('recovery_private_permissions')
        for path in INSTALLED.rglob('*'):
            trusted(path, directory=path.is_dir())
        sys.path.insert(0, str(INSTALLED))
        from homeserver_common.usb_recovery import HostBackend, RecoveryController, validate_config
        config = validate_config(json.loads(CONFIG.read_text()))
        if disabled.exists():
            trusted(disabled)
        state_path = STATE / 'state.json'
        if state_path.exists():
            trusted(state_path)
            if state_path.stat().st_mode & 0o077:
                raise ValueError('recovery_private_state_permissions')
        if args.disable:
            fd = os.open(disabled, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        elif args.enable:
            disabled.unlink(missing_ok=True)
        if args.disable or args.enable:
            result = {'enabled': not disabled.exists()}
        elif args.status:
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            result = {key: state.get(key) for key in ('phase', 'reason', 'updated_at')}
            result['enabled'] = not disabled.exists()
        elif disabled.exists():
            result = {'phase': 'disabled'}
        else:
            result = RecoveryController(HostBackend(config), state_path).tick(
                force_pools=tuple(args.recover))
        print(json.dumps(result, sort_keys=True))
        if result.get('phase') not in {'ready', 'disabled'}:
            syslog.openlog('homeserver-usb-recovery')
            syslog.syslog(syslog.LOG_NOTICE, json.dumps(result, sort_keys=True))
        return 0
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        # Never expose environment, source paths, device inventory or native names.
        reason = type(error).__name__
        syslog.openlog('homeserver-usb-recovery')
        syslog.syslog(syslog.LOG_ERR, f'recovery_entrypoint_failed:{reason}')
        print(json.dumps({'phase': 'hold', 'reason': 'recovery_entrypoint_failed'}))
        raise SystemExit(1) from None
