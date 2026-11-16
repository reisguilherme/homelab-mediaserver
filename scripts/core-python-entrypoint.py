#!/usr/bin/env python3
"""Fixed, supervised core Python roles; never import application code in the guard."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from homeserver_common.env import load_settings
from homeserver_common.runtime import apply_runtime_configuration
from homeserver_common.storage import load_storage_registry
from homeserver_common.storage_startup import verify_storage_startup

_spec = importlib.util.spec_from_file_location(
    'core_python_storage_guard', Path(__file__).with_name('core-storage-guard.py'))
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ROLES = ('control-api', 'control-worker', 'download-gateway')
REGISTRY_PATH = Path('/run/homeserver/storage.json')
ENV_PATH = Path('/run/core-runtime.env')
# Deployment requirement, not a claim that this script provisions capabilities.
# KILL is necessary for the supervisor to contain imports that drop to UID 1000.
WORKER_CAPABILITIES = frozenset({'CHOWN', 'DAC_OVERRIDE', 'SETUID', 'SETGID', 'KILL'})


def python_plan(role):
    guard.require(role in ROLES, 'python_role')
    argv = ['/app/.venv/bin/python', '-m']
    if role == 'control-worker':
        argv.append('homeserver_control.worker')
    else:
        module, port = (('api', '8080') if role == 'control-api' else ('gateway', '8081'))
        argv.extend(['uvicorn', f'homeserver_control.{module}.app:app',
                     '--host', '0.0.0.0', '--port', port])
    uid = 0 if role == 'control-worker' else 1000
    return dict(argv=argv, cwd='/app', data_mode='rw' if uid == 0 else 'ro', uid=uid, gid=uid)


def check_identity(role):
    plan = python_plan(role)
    guard.require((os.geteuid(), os.getegid()) == (plan['uid'], plan['gid']), 'python_identity')


def parse_activation(raw, role, expected_uuid, pinned_sha256):
    plan = python_plan(role)
    guard.require(isinstance(expected_uuid, str) and re.fullmatch(guard.UUID, expected_uuid),
                  'expected_uuid')
    guard.require(isinstance(pinned_sha256, str) and re.fullmatch(guard.SHA256, pinned_sha256),
                  'activation_pin')
    guard.require(isinstance(raw, bytes) and 0 < len(raw) <= 8192, 'activation_size')
    guard.require(hashlib.sha256(raw).hexdigest() == pinned_sha256, 'activation_changed')

    def unique(pairs):
        value = {}
        for key, item in pairs:
            guard.require(key not in value, 'activation_duplicate')
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise guard.GuardError('activation_json') from None
    digests = {'native_command_sha256', 'restore_receipt_sha256',
               'configuration_receipt_sha256', 'native_process_group_receipt_sha256',
               'storage_registry_sha256', 'runtime_environment_sha256'}
    guard.require(isinstance(value, dict) and set(value) == {
        'schema', 'scope', 'role', 'filesystem_uuid', 'data_mode', 'image_id', *digests,
    }, 'activation_schema')
    guard.require(type(value['schema']) is int and value['schema'] == 1
                  and value['scope'] == 'production-core-python-activation', 'activation_scope')
    guard.require(value['role'] == role and value['filesystem_uuid'] == expected_uuid
                  and value['data_mode'] == plan['data_mode'], 'activation_identity')
    guard.require(isinstance(value['image_id'], str)
                  and re.fullmatch('sha256:' + guard.SHA256, value['image_id']), 'activation_image')
    guard.require(all(isinstance(value[key], str) and re.fullmatch(guard.SHA256, value[key])
                      for key in digests), 'activation_evidence_digest')
    guard.require(value['native_command_sha256'] == guard.native_plan_digest(plan),
                  'activation_command')
    return value


def check_bindings(host, local, role, devices):
    """Use the shared parser; these three Python-specific sources live on the SSD."""
    python_plan(role)
    root = guard.exact_mount(guard.mount_rows(host), '/')
    rows = guard.mount_rows(local)
    specs = (
        ('/run/core-activation', f'/srv/appdata/core-activation/{role}'),
        (str(REGISTRY_PATH), '/srv/appdata/core-runtime/storage.json'),
        (str(ENV_PATH), f'/srv/appdata/core-runtime/{role}.env'),
    )
    token = []
    for (target, source), observed_device in zip(specs, devices, strict=True):
        mount = guard.exact_mount(rows, target)
        guard.require(mount['root'] == source
                      and mount['device'] == observed_device == root['device']
                      and mount['source'] == root['source']
                      and mount['filesystem'] == root['filesystem']
                      and 'ro' in mount['options'] and 'rw' not in mount['options'],
                      'python_configuration_mount')
        token.extend((mount['id'], mount['device']))
    return tuple(token)


def execution_check(role, expected_uuid, pinned_sha256):
    # Complete config/mount reads before the lease read. They cannot extend its deadline.
    raw, activation_device = guard.read_trusted(guard.ACTIVATION_PATH, 8192, expected_mode=0o444)
    receipt = parse_activation(raw, role, expected_uuid, pinned_sha256)
    registry_raw, registry_device = guard.read_trusted(REGISTRY_PATH, 65536, expected_mode=0o444)
    env_raw, env_device = guard.read_trusted(ENV_PATH, 262144, expected_mode=0o440)
    guard.require(hashlib.sha256(registry_raw).hexdigest() == receipt['storage_registry_sha256'],
                  'registry_changed')
    guard.require(hashlib.sha256(env_raw).hexdigest() == receipt['runtime_environment_sha256'],
                  'runtime_environment_changed')
    host, _ = guard.read_trusted(guard.HOST_MOUNT_PATH, guard.MAX_MOUNT_BYTES, expected_mode=0o444)
    local = guard.read_kernel(guard.local_mount_path(), guard.MAX_MOUNT_BYTES)
    binding = check_bindings(host, local, role, (activation_device, registry_device, env_device))
    registry = load_storage_registry(REGISTRY_PATH, required_backend='ext4-single')
    guard.require(registry is not None and registry.primary_pool.filesystem_id == expected_uuid,
                  'registry_identity')
    guard.require(verify_storage_startup(REGISTRY_PATH, Path('/data'),
                                        required_backend='ext4-single',
                                        writable=role == 'control-worker'), 'registry_required')
    storage = guard.check_runtime(expected_uuid, python_plan(role)['data_mode'])
    return {**storage, 'token': (*storage['token'], *binding, pinned_sha256,
                                receipt['storage_registry_sha256'],
                                receipt['runtime_environment_sha256'])}


def build_environment(role, expected_uuid):
    python_plan(role)
    # The private file is the only settings input. No ambient credentials, proxies,
    # Python injection or alternate commands pass into the application process.
    environment = load_settings(ENV_PATH, mode='prod').as_environment()
    environment.update({
        'PATH': '/app/.venv/bin:/usr/local/bin:/usr/bin:/bin',
        'PYTHONPATH': '/app/services/common/src:/app/services/control/src',
        'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
        'HOME': '/var/lib/homeserver', 'TMPDIR': '/tmp', 'LANG': 'C.UTF-8',
        'HOMESERVER_SERVICE_ROLE': role, 'HOMESERVER_ENVIRONMENT': 'prod',
        'HOMESERVER_APPDATA_ROOT': '/var/lib/homeserver',
        'HOMESERVER_MEDIA_ROOT': '/data', 'HOMESERVER_MEDIA_ROOTS': '/data/media:/data/torrents',
        'HOMESERVER_RUN_ROOT': '/run/homeserver',
        'HOMESERVER_DB_PATH': '/var/lib/homeserver/control.sqlite',
        'HOMESERVER_MEDIA_UUID': expected_uuid,
        'HOMESERVER_STORAGE_REGISTRY': str(REGISTRY_PATH),
        'HOMESERVER_CAPACITY_SNAPSHOT': '/run/core-capacity/capacity.json',
        'HOMESERVER_HOST_SNAPSHOT': '/run/homeserver/host.json',
        'HOMESERVER_RECOVERY_MODE': '/var/lib/homeserver/RECOVERY_MODE',
        'HOMESERVER_SERVICE_UID': '1000', 'HOMESERVER_SERVICE_GID': '1000',
        'HOMESERVER_ARR_UID': '1000', 'HOMESERVER_ARR_GID': '1000',
    })
    return apply_runtime_configuration(ENV_PATH, 'prod', environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', choices=ROLES)
    parser.add_argument('--filesystem-uuid')
    parser.add_argument('--activation-sha256')
    args = parser.parse_args(argv)
    if args.run is None:
        print('core Python startup denied: explicit role required', file=sys.stderr)
        return 78
    try:
        check_identity(args.run)
        plan = python_plan(args.run)

        def checker():
            return execution_check(args.run, args.filesystem_uuid, args.activation_sha256)

        checker()
        environment = build_environment(args.run, args.filesystem_uuid)
        # The shared supervisor rechecks twice before Popen, then continuously.
        # These are the only child commands; no Python application imports above.
        return guard.supervise(plan['argv'], checker, cwd=plan['cwd'], env=environment)
    except (guard.GuardError, OSError, RuntimeError, ValueError, TypeError):
        # Configuration exceptions may contain private values; expose no details.
        print('core Python startup denied: configuration or admission unavailable', file=sys.stderr)
        return 78


if __name__ == '__main__':
    sys.exit(main())
