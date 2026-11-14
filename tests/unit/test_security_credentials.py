"""Credential tooling uses private fixture files; no native account is touched."""

import importlib.util
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / 'scripts/security-credentials.py'


@pytest.fixture
def rotation():
    assert SCRIPT.is_file(), 'The protected credential coordinator must exist'
    spec = importlib.util.spec_from_file_location('security_credentials', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inventory():
    return {
        'jellyfin': [
            {'id': 'a' * 32, 'username': 'owner', 'admin': True, 'disabled': False},
            {'id': 'b' * 32, 'username': 'viewer', 'admin': False, 'disabled': False},
        ],
        'seerr': [
            {'id': 1, 'username': 'owner@example.test', 'permissions': 2,
             'jellyfin_id': 'a' * 32, 'has_password': True},
            {'id': 2, 'username': 'viewer@example.test', 'permissions': 8,
             'jellyfin_id': 'b' * 32, 'has_password': False},
            {'id': 3, 'username': 'local@example.test', 'permissions': 8,
             'jellyfin_id': None, 'has_password': True},
        ],
        'controller_admin_id': 'a' * 32,
        'unsupported': 0,
    }


def test_preparation_is_durable_distinct_and_resumable(rotation, inventory, tmp_path):
    folder = tmp_path / 'private'
    first = rotation.prepare(folder, inventory)
    passwords = [row['password'] for row in first['credentials']]
    assert len(passwords) == len(set(passwords)) == 3
    assert all(len(password) == 24 for password in passwords)
    text = (folder / 'credentials.txt').read_text(encoding='utf-8')
    assert all(password in text for password in passwords)
    second = rotation.prepare(folder, inventory)
    assert second == first
    assert (folder / 'credentials.txt').read_text(encoding='utf-8') == text
    if os.name != 'nt':
        assert folder.stat().st_mode & 0o777 == 0o700
        assert (folder / 'credentials.txt').stat().st_mode & 0o777 == 0o600


def test_credentials_txt_damage_prevents_remote_mutation(rotation, inventory, tmp_path):
    folder = tmp_path / 'private'
    rotation.prepare(folder, inventory)
    (folder / 'credentials.txt').write_text('damaged', encoding='utf-8')
    calls = []
    with pytest.raises(rotation.RotationError, match='credential-file'):
        rotation.apply_local(folder, lambda payload: calls.append(payload))
    assert not calls


def test_changed_identity_set_is_not_silently_regenerated(rotation, inventory, tmp_path):
    folder = tmp_path / 'private'
    rotation.prepare(folder, inventory)
    changed = deepcopy(inventory)
    changed['jellyfin'].pop()
    changed['seerr'].pop(1)
    with pytest.raises(rotation.RotationError, match='inventory-changed'):
        rotation.prepare(folder, changed)


def test_unsupported_or_orphan_linked_account_fails_before_password_generation(
    rotation, inventory, tmp_path,
):
    inventory['seerr'][0]['jellyfin_id'] = 'c' * 32
    with pytest.raises(rotation.RotationError, match='account-link'):
        rotation.prepare(tmp_path / 'private', inventory)
    assert not (tmp_path / 'private' / 'credentials.txt').exists()


class NativeFixture:
    def __init__(self, inventory):
        self.original = deepcopy(inventory)
        self.passwords = {}
        self.calls = []
        self.fail = None
        self.allowed = True
        self.requests = [91, 92]
        self.sessions = ['old-cookie']

    def check_preconditions(self, stage):
        if not self.allowed:
            raise RuntimeError('must never disclose native-secret')
        self.calls.append(('preconditions', stage))

    def inventory(self):
        return deepcopy(self.original)

    def ensure_controller_key(self, identifier):
        self.calls.append(('controller-key', identifier))

    def rotate(self, service, identifier, password):
        if (service, identifier) == self.fail:
            raise RuntimeError('private-password-or-token')
        self.passwords[service, identifier] = password
        self.calls.append(('rotate', service, identifier))

    def verify_password(self, service, identifier, username, password, disabled=False):
        assert self.passwords[service, identifier] == password

    def persist_admin_password(self, password):
        self.admin_password = password

    def clear_sessions(self):
        self.sessions.clear()
        self.calls.append(('sessions',))

    def verify_preserved(self, before):
        assert self.original == before


def test_rotation_keeps_linked_identity_and_orders_key_before_passwords(
    rotation, inventory, tmp_path,
):
    plan = rotation.prepare(tmp_path / 'local', inventory)
    native = NativeFixture(inventory)
    result = rotation.apply_remote(plan, native, tmp_path / 'remote')
    credentials = {(r['service'], r['id']): r['password'] for r in plan['credentials']}
    assert native.passwords['seerr', 1] == credentials['jellyfin', 'a' * 32]
    assert ('seerr', 2) not in native.passwords
    assert native.passwords['seerr', 3] == credentials['seerr', 3]
    assert native.calls[1][0] == 'controller-key'
    assert native.admin_password == credentials['jellyfin', 'a' * 32]
    assert native.sessions == []
    assert native.requests == [91, 92]
    assert result == {'complete': 1, 'passwords': 4, 'identities': 3}
    assert all(password not in json.dumps(result) for password in credentials.values())


def test_partial_failure_resumes_same_passwords_without_repeating_completed_accounts(
    rotation, inventory, tmp_path,
):
    plan = rotation.prepare(tmp_path / 'local', inventory)
    native = NativeFixture(inventory)
    native.fail = ('seerr', 3)
    with pytest.raises(RuntimeError):
        rotation.apply_remote(plan, native, tmp_path / 'remote')
    completed_calls = list(native.calls)
    native.fail = None
    result = rotation.apply_remote(plan, native, tmp_path / 'remote')
    assert result['complete'] == 1
    before = [c for c in completed_calls if c[0] == 'rotate']
    assert all(native.calls.count(call) == 1 for call in before)


def test_preconditions_precede_every_mutation(rotation, inventory, tmp_path):
    plan = rotation.prepare(tmp_path / 'local', inventory)
    native = NativeFixture(inventory)
    native.allowed = False
    with pytest.raises(RuntimeError):
        rotation.apply_remote(plan, native, tmp_path / 'remote')
    assert native.calls == []
    assert native.passwords == {}


def test_remote_journal_cannot_accept_changed_password_bundle(rotation, inventory, tmp_path):
    plan = rotation.prepare(tmp_path / 'local', inventory)
    native = NativeFixture(inventory)
    rotation.apply_remote(plan, native, tmp_path / 'remote')
    changed = deepcopy(plan)
    changed['credentials'][0]['password'] = 'A1!' + 'x' * 21
    with pytest.raises(rotation.RotationError, match='journal-mismatch'):
        rotation.apply_remote(changed, native, tmp_path / 'remote')


def test_error_output_contains_only_fixed_code(rotation):
    assert rotation.safe_error(RuntimeError('username/password/token')) == {
        'ok': False, 'error': 'operation-failed',
    }


def test_env_update_changes_only_selected_private_keys(rotation, tmp_path):
    path = tmp_path / '.env'
    path.write_text('# retained\nHOMESERVER_ADMIN_PASSWORD="qbit-status-same"\n'
                    'HOMESERVER_JELLYFIN_API_KEY="old"\n', encoding='utf-8')
    rotation.update_env(path, {'HOMESERVER_JELLYFIN_API_KEY': 'new-private-key',
                               'HOMESERVER_JELLYFIN_ADMIN_PASSWORD': 'new-private-password'})
    result = path.read_text(encoding='utf-8')
    assert '# retained' in result
    assert 'HOMESERVER_ADMIN_PASSWORD="qbit-status-same"' in result
    assert 'old' not in result
    assert 'HOMESERVER_JELLYFIN_ADMIN_PASSWORD="new-private-password"' in result
    rotation.private_file(path)


def test_stopped_seerr_cleanup_preserves_accounts_and_request_history(rotation, tmp_path):
    import sqlite3
    from contextlib import closing

    path = tmp_path / 'db.sqlite3'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE session (id TEXT, json TEXT, expiredAt INT, destroyedAt INT)')
        db.execute("INSERT INTO session VALUES ('old-session','secret-cookie',1,NULL)")
        db.execute('CREATE TABLE user (id INT, permissions INT, password TEXT, '
                   'jellyfinUserId TEXT, jellyfinAuthToken TEXT, resetPasswordGuid TEXT, '
                   'recoveryLinkExpirationDate TEXT)')
        db.execute("INSERT INTO user VALUES (1,2,'hash','guid','old-token','reset','today')")
        db.execute('CREATE TABLE media_request (id INT, requestedById INT)')
        db.execute('INSERT INTO media_request VALUES (91,1)')
    with pytest.raises(rotation.RotationError, match='seerr-running'):
        rotation.clear_seerr_database(path, stopped=False)
    rotation.clear_seerr_database(path, stopped=True)
    assert not path.with_name(path.name + '-wal').exists()
    assert not path.with_name(path.name + '-shm').exists()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT count(*) FROM session').fetchone()[0] == 0
        assert db.execute('SELECT * FROM user').fetchone() == (
            1, 2, 'hash', 'guid', None, None, None)
        assert db.execute('SELECT * FROM media_request').fetchall() == [(91, 1)]


@pytest.mark.parametrize('threshold', [0, 3, 5, -1])
@pytest.mark.parametrize('disabled', [False, True])
def test_native_jellyfin_rotation_preserves_policy_and_uses_elevated_password_api(
    rotation, threshold, disabled,
):
    calls = []
    policy = {
        'IsAdministrator': True, 'IsHidden': False, 'IsDisabled': disabled,
        'EnableContentDeletion': False, 'EnabledFolders': ['fixture-library'],
        'InvalidLoginAttemptCount': 2, 'LoginAttemptsBeforeLockout': threshold,
    }

    class Api:
        def request(self, method, path, body=None, **kwargs):
            calls.append((method, path, body))
            if method == 'GET':
                return {'Id': 'a' * 32, 'Policy': deepcopy(policy)}

    adapter = rotation.RemoteNative.__new__(rotation.RemoteNative)
    adapter.jellyfin = Api()
    adapter.rotate('jellyfin', 'a' * 32, 'fixture-password')
    assert calls[-1] == ('POST', '/Users/Password?userId=' + 'a' * 32,
                        {'NewPw': 'fixture-password', 'ResetPassword': False})
    assert calls[-2][2] == policy | {'IsHidden': True, 'LoginAttemptsBeforeLockout': -1}
    assert all('CurrentPw' not in (body or {}) for _, _, body in calls)


def test_receipt_requires_fresh_complete_verified_scope(rotation):
    receipt = {key: True for key in rotation.REQUIRED_RECEIPT}
    receipt.update({'verified_at': 100, 'host': 'homelab', 'version': 1})
    rotation.validate_receipt(receipt, now=101)
    for key in rotation.REQUIRED_RECEIPT:
        with pytest.raises(rotation.RotationError, match='preconditions'):
            rotation.validate_receipt(receipt | {key: False}, now=101)
    with pytest.raises(rotation.RotationError, match='preconditions'):
        rotation.validate_receipt(receipt, now=4000)


def test_ssh_uses_only_encrypted_stdin_and_never_echoes_child_errors(rotation, monkeypatch):
    seen = []

    def run(args, *, payload, timeout):
        seen.append((args, payload))
        return b'{"complete":1,"passwords":2,"identities":2}'

    monkeypatch.setattr(rotation, 'command', run)
    result = rotation.ssh_transport({'password': 'fixture-secret'})
    assert result['complete'] == 1
    assert 'fixture-secret' not in str(seen[0][0])
    assert b'fixture-secret' in seen[0][1]
    assert seen[0][0] == [
        'ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ConnectTimeout=8', 'root@100.74.194.46', 'python3',
        '/home/reis/homelab-mediaserver/scripts/security-credentials.py', '--remote',
    ]


def test_seerr_password_api_uses_only_new_password_and_no_role_write(rotation):
    seen = []

    class Api:
        def request(self, *args, **kwargs):
            seen.append(args)

    adapter = rotation.RemoteNative.__new__(rotation.RemoteNative)
    adapter.seerr = Api()
    adapter.rotate('seerr', 3, 'fixture-new-password')
    assert seen == [('POST', '/user/3/settings/password', {'newPassword': 'fixture-new-password'})]


def test_owner_hash_update_is_stopped_scoped_and_preserves_other_columns(rotation, tmp_path):
    import sqlite3
    from contextlib import closing

    path = tmp_path / 'owner.sqlite'
    value = '$2b$12$' + 'a' * 53
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE user (id INT, password TEXT, permissions INT, username TEXT)')
        db.execute("INSERT INTO user VALUES (1,'old-hash',2,'owner')")
        db.execute("INSERT INTO user VALUES (2,'second-hash',8,'viewer')")
    with pytest.raises(rotation.RotationError, match='seerr-running'):
        rotation.set_owner_hash(path, value, stopped=False)
    rotation.set_owner_hash(path, value, stopped=True)
    assert not path.with_name(path.name + '-wal').exists()
    assert not path.with_name(path.name + '-shm').exists()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT * FROM user ORDER BY id').fetchall() == [
            (1, value, 2, 'owner'), (2, 'second-hash', 8, 'viewer')]


def test_local_only_account_without_old_password_can_complete_rotation(
    rotation, inventory, tmp_path,
):
    inventory['seerr'][2]['has_password'] = False
    plan = rotation.prepare(tmp_path / 'local', inventory)
    native = NativeFixture(inventory)
    rotate = native.rotate

    def set_and_reflect(service, identifier, password):
        rotate(service, identifier, password)
        if service == 'seerr' and identifier == 3:
            native.original['seerr'][2]['has_password'] = True

    native.rotate = set_and_reflect
    native.verify_preserved = lambda before: rotation.same_inventory(before, native.original)
    assert rotation.apply_remote(plan, native, tmp_path / 'remote')['complete'] == 1
    assert rotation.apply_remote(plan, native, tmp_path / 'remote')['complete'] == 1


def test_local_result_never_returns_private_env_or_unexpected_remote_fields(
    rotation, inventory, tmp_path,
):
    folder = tmp_path / 'local'
    rotation.prepare(folder, inventory)
    local_env = tmp_path / '.env'
    local_env.write_text('HOMESERVER_ADMIN_PASSWORD="unchanged"\n', encoding='utf-8')

    def transport(payload):
        return {'complete': 1, 'passwords': 4, 'identities': 3,
                'private_env': {'HOMESERVER_JELLYFIN_API_KEY': 'must-not-print'},
                'unexpected': 'must-not-print'}

    result = rotation.apply_local(folder, transport, local_env=local_env)
    assert result == {'complete': 1, 'passwords': 4, 'identities': 3}
    assert 'must-not-print' in local_env.read_text(encoding='utf-8')


def test_jellyfin_native_requests_use_modern_authorization_only(rotation):
    from email.message import Message

    seen = []

    class Response:
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            return b'{}'

    class Opener:
        def open(self, request, timeout):
            seen.append(request)
            return Response()

    api = rotation.NativeApi('http://127.0.0.1:8096/jellyfin', key='fixture-key')
    api.opener = Opener()
    api.request('GET', '/Auth/Keys')
    api.request('POST', '/Users/Password?userId=' + 'a' * 32,
                {'NewPw': 'fixture-password', 'ResetPassword': False})
    assert all(request.get_header('X-emby-token') is None for request in seen)
    assert all('Token="fixture-key"' in request.get_header('Authorization', '') for request in seen)


def test_already_revoked_devices_do_not_abort_session_cleanup(rotation):
    calls = []

    class Api:
        def request(self, method, path):
            calls.append((method, path))
            assert method == 'GET'
            return {'Items': []}

    rotation.revoke_devices(Api(), ['device-already-revoked-by-password'])
    assert calls == [('GET', '/Devices'), ('GET', '/Devices')]


def test_completed_plan_apply_only_recovers_result_without_repeating_revocation(
    rotation, inventory, tmp_path,
):
    plan = rotation.prepare(tmp_path / 'local', inventory)
    native = NativeFixture(inventory)
    first = rotation.apply_remote(plan, native, tmp_path / 'remote')
    second = rotation.apply_remote(plan, native, tmp_path / 'remote')
    assert second == first
    assert native.calls.count(('sessions',)) == 1
    assert sum(call[0] == 'controller-key' for call in native.calls) == 1


def test_a_second_coordinator_cannot_apply_the_same_private_journal(rotation, tmp_path):
    folder = rotation.secure_directory(tmp_path / 'private')
    with rotation.coordinator_lock(folder):
        with pytest.raises(rotation.RotationError, match='rotation-busy'):
            with rotation.coordinator_lock(folder):
                pytest.fail('Concurrent rotations must never run')
    with rotation.coordinator_lock(folder):
        pass


def test_seerr_helper_uses_fixed_native_endpoint_not_https_redirect_proxy(rotation):
    api = rotation.NativeApi('http://172.30.240.18:5055/api/v1', seerr=True)
    assert api.base == 'http://172.30.240.18:5055/api/v1'
    with pytest.raises(rotation.RotationError, match='native-api'):
        rotation.NativeApi('http://127.0.0.1:5055/api/v1', seerr=True)


def test_seerr_verify_accepts_destroyed_and_expired_sessions_but_rejects_active(
    rotation, inventory, tmp_path, monkeypatch,
):
    import sqlite3
    from contextlib import closing

    path = tmp_path / 'sessions.sqlite3'
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE media_request (id INT, requestedById INT)')
        db.execute('INSERT INTO media_request VALUES (91,1)')
        db.execute('CREATE TABLE session (id TEXT, expiredAt INT, destroyedAt TEXT)')
        db.execute("INSERT INTO session VALUES ('destroyed',200000,'2026-10-18')")
        db.execute("INSERT INTO session VALUES ('expired',99999,NULL)")
        db.execute("INSERT INTO session VALUES ('boundary',100000,NULL)")

    class Api:
        def request(self, method, endpoint):
            if endpoint == '/Devices':
                return {'Items': []}
            return [{'Policy': {'IsHidden': True, 'LoginAttemptsBeforeLockout': -1}}]

    adapter = rotation.RemoteNative.__new__(rotation.RemoteNative)
    adapter.inventory = lambda: deepcopy(inventory)
    adapter.jellyfin = Api()
    adapter.old_devices = []
    adapter.before_history = [(91, 1)]
    adapter.seerr_db = path
    monkeypatch.setattr(rotation.time, 'time', lambda: 100)
    adapter.verify_preserved(inventory)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO session VALUES ('active',100001,NULL)")
    with pytest.raises(rotation.RotationError, match='native-identity'):
        adapter.verify_preserved(inventory)


@pytest.mark.parametrize('readback', [-1, 0, 3, 5, None, True, False, -1.0, '-1', 'missing'])
def test_jellyfin_lockout_readback_must_be_exact_disabled_integer(
    rotation, inventory, tmp_path, readback,
):
    import sqlite3
    from contextlib import closing

    path = tmp_path / 'state.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        db.execute('CREATE TABLE media_request (id INT, requestedById INT)')
        db.execute('CREATE TABLE session (id TEXT, expiredAt INT, destroyedAt TEXT)')
    policy = {'IsHidden': True}
    if readback != 'missing':
        policy['LoginAttemptsBeforeLockout'] = readback

    class Api:
        def request(self, method, endpoint):
            if endpoint == '/Devices':
                return {'Items': []}
            return [
                {'Policy': {'IsHidden': True, 'LoginAttemptsBeforeLockout': -1}},
                {'Policy': policy},
            ]

    adapter = rotation.RemoteNative.__new__(rotation.RemoteNative)
    adapter.inventory = lambda: deepcopy(inventory)
    adapter.jellyfin = Api()
    adapter.old_devices = []
    adapter.before_history = []
    adapter.seerr_db = path
    if type(readback) is int and readback == -1:
        adapter.verify_preserved(inventory)
    else:
        with pytest.raises(rotation.RotationError, match='native-identity'):
            adapter.verify_preserved(inventory)


@pytest.mark.parametrize('outcomes', [[200, 403], [403], [200] * 8, [500]])
def test_seerr_password_verification_proves_original_cookie_revoked(
    rotation, monkeypatch, outcomes,
):
    calls = []
    remaining = iter(outcomes)

    class Api:
        def __init__(self, base, **kwargs):
            self.cookies = {'connect.sid': 'signed-fixture-cookie'}

        def request(self, method, path, body=None, **kwargs):
            calls.append((method, path, kwargs))
            if path == '/auth/local':
                return {'id': 3}
            if path == '/auth/logout':
                self.cookies.clear()
                return {'status': 'ok'}
            assert path == '/auth/me'
            assert kwargs['headers']['Cookie'] == 'connect.sid=signed-fixture-cookie'
            assert kwargs['authenticated'] is False
            status = next(remaining)
            if status != 200:
                raise rotation.NativeStatus(status)
            return {'id': 3}

    monkeypatch.setattr(rotation, 'NativeApi', Api)
    monkeypatch.setattr(rotation.time, 'sleep', lambda value: None)
    adapter = rotation.RemoteNative.__new__(rotation.RemoteNative)
    if 403 in outcomes:
        adapter.verify_password('seerr', 3, 'fixture@example.test', 'fixture-password')
    else:
        with pytest.raises(rotation.RotationError):
            adapter.verify_password('seerr', 3, 'fixture@example.test', 'fixture-password')
    assert sum(path == '/auth/me' for _, path, _ in calls) == len(outcomes)
