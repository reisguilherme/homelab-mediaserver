"""Fixed Python launchers: no application import before current storage admission."""

import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeserver_common.env import serialize_env
from homeserver_common.settings import PRODUCTION_REQUIRED

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/core-python-entrypoint.py'
UUID = 'b2256ce1-b29c-4608-a227-c78a50ea169e'
ROLES = ('control-api', 'control-worker', 'download-gateway')


def load():
    assert SCRIPT.is_file(), 'guarded core Python entrypoint is missing'
    spec = importlib.util.spec_from_file_location('core_python_fixture', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def document(module, role='control-api', **changes):
    value = dict(schema=1, scope='production-core-python-activation', role=role,
                 filesystem_uuid=UUID, data_mode='rw' if role == 'control-worker' else 'ro',
                 image_id='sha256:' + 'a' * 64,
                 native_command_sha256=module.guard.native_plan_digest(module.python_plan(role)),
                 restore_receipt_sha256='b' * 64, configuration_receipt_sha256='c' * 64,
                 native_process_group_receipt_sha256='d' * 64,
                 storage_registry_sha256='e' * 64, runtime_environment_sha256='f' * 64)
    value.update(changes)
    raw = json.dumps(value).encode()
    return raw, hashlib.sha256(raw).hexdigest()


def row(target, root, *, mode='ro', device='8:1', identifier=10, fs='ext4'):
    return (f'{identifier} 1 {device} {root} {target} {mode},nosuid,nodev,noexec '
            f'- {fs} /dev/sda rw\n').encode()


def bindings(role='control-api'):
    host = row('/', '/', mode='rw', identifier=1)
    local = row('/run/core-activation', f'/srv/appdata/core-activation/{role}')
    local += row('/run/homeserver/storage.json', '/srv/appdata/core-runtime/storage.json',
                 identifier=11)
    local += row('/run/core-runtime.env', f'/srv/appdata/core-runtime/{role}.env', identifier=12)
    local += row('/run/core-capacity', '/srv/appdata/core-capacity',
                 mode='rw' if role == 'control-worker' else 'ro', identifier=13)
    return host, local


@pytest.mark.parametrize('role,mode,uid,module_name,port', [
    ('control-api', 'ro', 1000, 'homeserver_control.api.app:app', '8080'),
    ('control-worker', 'rw', 0, '/app/scripts/core-worker-runtime.py', None),
    ('download-gateway', 'ro', 1000, 'homeserver_control.gateway.app:app', '8081'),
])
def test_fixed_command_and_storage_policy(role, mode, uid, module_name, port):
    module = load()
    plan = module.python_plan(role)
    assert plan['data_mode'] == mode and plan['uid'] == uid and plan['gid'] == uid
    assert plan['cwd'] == '/app'
    assert plan['argv'][0] == '/app/.venv/bin/python'
    if role != 'control-worker':
        assert plan['argv'][1] == '-m'
    assert module_name in plan['argv']
    if port:
        assert plan['argv'][-2:] == ['--port', port]
    assert 'KILL' in module.WORKER_CAPABILITIES


@pytest.mark.parametrize('role', ['jellyfin', 'qbit-monitor', 'sonarr', '', 'python -c payload'])
def test_no_arbitrary_role_or_command(role):
    module = load()
    with pytest.raises(module.guard.GuardError):
        module.python_plan(role)


def test_importing_entrypoint_does_not_import_native_application():
    before = set(sys.modules)
    load()
    assert not any(name.startswith('homeserver_control.') for name in set(sys.modules) - before)


@pytest.mark.parametrize('role', ROLES)
def test_activation_pins_exact_role_command_registry_and_private_environment(role):
    module = load()
    raw, pin = document(module, role)
    parsed = module.parse_activation(raw, role, UUID, pin)
    assert parsed['storage_registry_sha256'] == 'e' * 64
    assert parsed['runtime_environment_sha256'] == 'f' * 64


@pytest.mark.parametrize('changes', [
    {'schema': True}, {'scope': 'production-core-activation'}, {'role': 'control-worker'},
    {'filesystem_uuid': 'a' * 36}, {'data_mode': 'rw'}, {'extra': True},
    {'native_command_sha256': '0' * 64}, {'storage_registry_sha256': ''},
    {'runtime_environment_sha256': None}, {'image_id': 'latest'},
])
def test_activation_mismatch_refuses(changes):
    module = load()
    raw, pin = document(module, **changes)
    with pytest.raises(module.guard.GuardError):
        module.parse_activation(raw, 'control-api', UUID, pin)


def test_activation_pin_and_duplicate_keys_refuse():
    module = load()
    raw, pin = document(module)
    with pytest.raises(module.guard.GuardError):
        module.parse_activation(raw + b' ', 'control-api', UUID, pin)
    raw = raw[:-1] + b', "schema": 1}'
    with pytest.raises(module.guard.GuardError):
        module.parse_activation(raw, 'control-api', UUID, hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize('role', ROLES)
def test_runtime_bindings_are_readonly_on_the_guest_ssd(role):
    module = load()
    host, local = bindings(role)
    assert module.check_bindings(host, local, role, ('8:1', '8:1', '8:1', '8:1'))


@pytest.mark.parametrize('change', [
    lambda value: value.replace(b' ro,', b' rw,', 1),
    lambda value: value.replace(b'/srv/appdata/core-runtime/storage.json', b'/tmp/storage.json'),
    lambda value: value.replace(b'/srv/appdata/core-runtime/control-api.env', b'/tmp/private.env'),
    lambda value: value.replace(b'8:1', b'8:16'),
    lambda value: value + value.splitlines(keepends=True)[0],
])
def test_unsafe_or_ambiguous_bindings_refuse(change):
    module = load()
    host, local = bindings()
    with pytest.raises(module.guard.GuardError):
        module.check_bindings(host, change(local), 'control-api', ('8:1', '8:1', '8:1', '8:1'))


@pytest.mark.parametrize('role', ROLES)
def test_capacity_bind_policy_is_enforced(role):
    module = load()
    host, local = bindings(role)
    correct = b'rw' if role == 'control-worker' else b'ro'
    wrong = b'ro' if correct == b'rw' else b'rw'
    local = local.replace(b'/run/core-capacity ' + correct, b'/run/core-capacity ' + wrong)
    with pytest.raises(module.guard.GuardError):
        module.check_bindings(host, local, role, ('8:1',) * 4)


def wire_check(module, monkeypatch, role='control-api'):
    registry = b'fixture registry'
    environment = b'fixture environment'
    raw, pin = document(module, role, storage_registry_sha256=hashlib.sha256(registry).hexdigest(),
                        runtime_environment_sha256=hashlib.sha256(environment).hexdigest())
    host, local = bindings(role)
    values = {module.guard.ACTIVATION_PATH: raw, module.REGISTRY_PATH: registry,
              module.ENV_PATH: environment, module.guard.HOST_MOUNT_PATH: host}
    calls = []

    def read(path, _limit, **_kwargs):
        calls.append(('read', path))
        return values[path], '8:1'

    monkeypatch.setattr(module.guard, 'read_trusted', read)
    monkeypatch.setattr(module.guard, 'read_kernel', lambda *_args: local)
    monkeypatch.setattr(module, 'capacity_device', lambda: '8:1')
    def registry_loader(*a, **kw):
        calls.append(('load', a, kw))
        return SimpleNamespace(primary_pool=SimpleNamespace(filesystem_id=UUID))

    monkeypatch.setattr(module, 'load_storage_registry', registry_loader)
    monkeypatch.setattr(module, 'verify_storage_startup',
                        lambda *a, **kw: calls.append(('verify', a, kw)) or True)
    snapshot = dict(token=('original-mount',), sequence=1, lease_sha256='c' * 64,
                    expires_at=module.guard.uptime_clock() + 20)

    def check_runtime(uuid, mode):
        calls.append(('lease', uuid, mode))
        return snapshot

    monkeypatch.setattr(module.guard, 'check_runtime', check_runtime)
    return values, calls, pin, snapshot


@pytest.mark.parametrize('role', ROLES)
def test_repeated_check_requires_explicit_backend_and_role_mode_before_lease(monkeypatch, role):
    module = load()
    _values, calls, pin, snapshot = wire_check(module, monkeypatch, role)
    checked = module.execution_check(role, UUID, pin)
    assert ('load', (module.REGISTRY_PATH,), {'required_backend': 'ext4-single'}) in calls
    assert ('verify', (module.REGISTRY_PATH, Path('/data')),
            {'required_backend': 'ext4-single', 'writable': role == 'control-worker'}) in calls
    assert calls[-1] == ('lease', UUID, 'rw' if role == 'control-worker' else 'ro')
    assert checked['expires_at'] == snapshot['expires_at']
    assert checked['token'][:1] == snapshot['token']


@pytest.mark.parametrize('failure', ['missing', 'changed', 'wrong-uuid', 'legacy', 'hold'])
def test_failed_runtime_check_never_imports_or_starts_application(monkeypatch, failure):
    module = load()
    values, _calls, pin, _snapshot = wire_check(module, monkeypatch)
    if failure == 'missing':
        def missing(*args, **kwargs):
            raise FileNotFoundError('fixture missing registry')
        monkeypatch.setattr(module, 'load_storage_registry', missing)
    elif failure == 'changed':
        values[module.REGISTRY_PATH] += b'changed'
    elif failure == 'wrong-uuid':
        monkeypatch.setattr(module, 'load_storage_registry', lambda *a, **kw: SimpleNamespace(
            primary_pool=SimpleNamespace(filesystem_id='other')))
    elif failure == 'legacy':
        monkeypatch.setattr(module, 'verify_storage_startup', lambda *a, **kw: False)
    else:
        def hold(*args, **kwargs):
            raise module.guard.GuardError('hold')
        monkeypatch.setattr(module.guard, 'check_runtime', hold)
    before = set(sys.modules)
    with pytest.raises((module.guard.GuardError, OSError, RuntimeError, ValueError)):
        module.execution_check('control-api', UUID, pin)
    assert not any(name.startswith('homeserver_control.') for name in set(sys.modules) - before)


def preferences(tmp_path):
    values = {f'HOMESERVER_{key.upper()}': 'fixture-secret' for key in PRODUCTION_REQUIRED}
    values.update(HOMESERVER_JELLYFIN_URL='http://playback.example.test:8096/jellyfin',
                  HOMESERVER_MEDIA_ROOT='/old/media', HOMESERVER_RUN_ROOT='/old/runtime',
                  HOMESERVER_QBIT_PASSWORD='literal $HOME # secret',
                  HOMESERVER_WORKER_INTERVAL_SECONDS='12')
    path = tmp_path / 'runtime.env'
    path.write_text(serialize_env(values))
    return path


@pytest.mark.parametrize('role', ROLES)
def test_private_settings_preserve_literal_credentials_and_fix_paths(monkeypatch, tmp_path, role):
    module = load()
    monkeypatch.setattr(module, 'ENV_PATH', preferences(tmp_path))
    monkeypatch.setenv('HOMESERVER_QBIT_PASSWORD', 'ambient-must-not-leak')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'ambient-cloud-must-not-leak')
    monkeypatch.setenv('PYTHONPATH', '/evil')
    env = module.build_environment(role, UUID)
    assert env['HOMESERVER_QBIT_PASSWORD'] == 'literal $HOME # secret'
    assert env['HOMESERVER_MEDIA_ROOT'] == '/data'
    assert env['HOMESERVER_RUN_ROOT'] == '/run/homeserver'
    assert env['HOMESERVER_DB_PATH'] == '/var/lib/homeserver/control.sqlite'
    assert env['HOMESERVER_CAPACITY_SNAPSHOT'] == '/run/core-capacity/capacity.json'
    assert env['HOMESERVER_JELLYFIN_URL'] == 'http://playback.example.test:8096/jellyfin'
    assert env['HOMESERVER_SERVICE_ROLE'] == role
    assert '/evil' not in env['PYTHONPATH'] and 'AWS_SECRET_ACCESS_KEY' not in env
    if role == 'download-gateway':
        assert 'HOMESERVER_JELLYFIN_API_KEY' not in env and 'HOMESERVER_ADMIN_TOKEN' not in env


def test_main_missing_mode_and_configuration_failure_never_print_credentials(monkeypatch, capsys):
    module = load()
    assert module.main([]) == 78
    monkeypatch.setattr(module, 'execution_check', lambda *args: None)
    monkeypatch.setattr(module, 'check_identity', lambda *args: None)

    def bad_config(*_args):
        raise ValueError('fixture-secret-that-must-never-be-printed')

    monkeypatch.setattr(module, 'build_environment', bad_config)
    assert module.main(['--run', 'control-api', '--filesystem-uuid', UUID,
                        '--activation-sha256', 'a' * 64]) == 78
    output = capsys.readouterr()
    assert 'fixture-secret' not in output.out + output.err


@pytest.mark.parametrize('role', ROLES)
def test_wrong_process_identity_refuses(monkeypatch, role):
    module = load()
    monkeypatch.setattr(os, 'geteuid', lambda: 1234)
    monkeypatch.setattr(os, 'getegid', lambda: 1234)
    with pytest.raises(module.guard.GuardError):
        module.check_identity(role)


@pytest.mark.parametrize('reason', ['hold', 'expiry'])
def test_shared_supervisor_contains_started_python_on_hold_or_expiry(monkeypatch, tmp_path, reason):
    module = load()
    started = tmp_path / 'fixture-imported'
    fixture = tmp_path / 'fixture-app.py'
    fixture.write_text('from pathlib import Path\nimport time, os\n'
                       f'Path({str(started)!r}).write_text(str(os.getpid()))\n'
                       'time.sleep(30)\n')
    original_plan = module.python_plan

    def fixture_plan(role):
        return {**original_plan(role), 'argv': [sys.executable, str(fixture)], 'cwd': str(tmp_path)}

    monkeypatch.setattr(module, 'python_plan', fixture_plan)
    monkeypatch.setattr(module, 'check_identity', lambda *_: None)
    monkeypatch.setattr(module, 'build_environment', lambda *_: {'PATH': '/usr/bin:/bin'})
    expiry = module.guard.uptime_clock() + 0.8
    calls = []

    def checker(*_args):
        calls.append(started.exists())
        if reason == 'hold' and started.exists():
            raise module.guard.GuardError('hold')
        return dict(token=('fixture',), sequence=1, lease_sha256='a' * 64, expires_at=expiry)

    monkeypatch.setattr(module, 'execution_check', checker)
    before = time.monotonic()
    assert module.main(['--run', 'control-api', '--filesystem-uuid', UUID,
                        '--activation-sha256', 'a' * 64]) == 78
    assert started.exists() and len(calls) >= 3
    assert not any(calls[:3]), 'the child must not run before all initial checks'
    assert time.monotonic() - before < 4
    with pytest.raises(ProcessLookupError):
        os.kill(int(started.read_text()), 0)


def test_changed_environment_after_loading_refuses_before_application_import(monkeypatch):
    module = load()
    values, _calls, pin, _snapshot = wire_check(module, monkeypatch)
    monkeypatch.setattr(module, 'check_identity', lambda *_: None)

    def config_changed(*_args):
        values[module.ENV_PATH] += b'changed-after-settings-read'
        return {}

    def forbidden_child(*_args, **_kwargs):
        pytest.fail('application must not be started with changed pinned environment')

    monkeypatch.setattr(module, 'build_environment', config_changed)
    monkeypatch.setattr(module.guard.subprocess, 'Popen', forbidden_child)
    assert module.main(['--run', 'control-api', '--filesystem-uuid', UUID,
                        '--activation-sha256', pin]) == 78
