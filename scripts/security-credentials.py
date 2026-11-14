"""One-off private credential coordinator; never invoke remote mode from an agent.

Local Windows usage: --prepare|--apply|--verify --output <new private directory>.
Only the local coordinator may consume the remote inventory pipe. Its public
output contains counts and fixed error codes. Passwords never enter arguments,
environment variables, terminal output or logs. No account is created/deleted.

Native contracts: Jellyfin v12.2 UserController/ApiKeyController/DevicesController;
Seerr v3.5.0 user/usersettings.ts, auth.ts and entity/Session.ts. Apply requires
an operator-created, fresh root-owned precondition receipt after migration and
private access verification. This script does not establish those prerequisites.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import string
import subprocess
import sys
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PROJECT = Path('/home/reis/homelab-mediaserver')
PRIVATE = Path('/var/lib/homeserver/security-hardening-20261018')
HOST = 'root@100.74.194.46'
SEERR_NATIVE = 'http://172.30.240.18:5055/api/v1'
PASSWORD_CHARS = string.ascii_letters + string.digits + '!@#%+=_-'
MAX_MESSAGE = 2 * 1024 * 1024
GUID = re.compile(r'[a-f0-9]{32}')
ERROR_CODES = frozenset({
    'operation-failed', 'private-path', 'private-permissions', 'credential-file',
    'inventory-changed', 'account-link', 'unsupported-account', 'invalid-plan',
    'journal-mismatch', 'preconditions', 'native-api', 'native-identity',
    'seerr-running', 'database-schema', 'transport', 'invalid-operation', 'rotation-busy',
})


class RotationError(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'operation-failed'
        super().__init__(self.code)


def safe_error(error):
    return {'ok': False, 'error': error.code if isinstance(error, RotationError)
            else 'operation-failed'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def command(args, *, payload=None, timeout=60):
    try:
        result = subprocess.run(args, input=payload, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise RotationError('operation-failed') from None
    if result.returncode:
        raise RotationError('operation-failed')
    return result.stdout


def windows_permissions(path, *, establish=False):
    # Paths are code-quoted, never credentials. Explicit DACL contains only the
    # current user's SID; inheritance is removed before any secret file exists.
    literal = "'" + str(path).replace("'", "''") + "'"
    body = '$ErrorActionPreference="Stop"; $p=' + literal + '; '
    body += '$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User; '
    if establish:
        kind = 'Directory' if path.is_dir() else 'File'
        rights = '"FullControl","ContainerInherit,ObjectInherit","None","Allow"' \
            if path.is_dir() else '"FullControl","Allow"'
        body += (
            '$a=[System.IO.' + kind + ']::GetAccessControl($p,'
            '[System.Security.AccessControl.AccessControlSections]::Access); '
            '$a.SetAccessRuleProtection($true,$false); '
            'foreach($old in @($a.Access)){[void]$a.RemoveAccessRuleSpecific($old)}; '
            '$r=New-Object System.Security.AccessControl.FileSystemAccessRule('
            '$sid,' + rights + '); '
            '$a.AddAccessRule($r); [System.IO.' + kind + ']::SetAccessControl($p,$a); '
        )
    body += (
        '$a=Get-Acl -LiteralPath $p; '
        'if($a.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne '
        '$sid.Value){exit 1}; '
        'foreach($r in $a.GetAccessRules($true,$true,'
        '[System.Security.Principal.SecurityIdentifier])){'
        'if($r.AccessControlType -eq "Allow" -and '
        '$r.IdentityReference.Value -ne $sid.Value){exit 1}}; '
        'if(-not $a.Access.Count){exit 1}'
    )
    encoded = base64.b64encode(body.encode('utf-16le')).decode('ascii')
    command(['powershell.exe', '-NoLogo', '-NoProfile', '-NonInteractive',
             '-EncodedCommand', encoded])


def secure_directory(path):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path:
        raise RotationError('private-path')
    created = not path.exists()
    if created:
        path.mkdir(mode=0o700, parents=True)
    if not path.is_dir():
        raise RotationError('private-path')
    if os.name == 'nt':
        windows_permissions(path, establish=created)
    elif path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise RotationError('private-permissions')
    return path


def private_file(path):
    if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise RotationError('private-path')
    if os.name == 'nt':
        windows_permissions(path)
    elif path.stat().st_uid != os.geteuid() or path.stat().st_mode & 0o077:
        raise RotationError('private-permissions')


@contextmanager
def coordinator_lock(directory):
    path = directory / '.rotation.lock'
    if path.is_symlink():
        raise RotationError('private-path')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, 'r+b', buffering=0) as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RotationError('rotation-busy') from None
        # Closing the descriptor releases the lock even after interruption.
        yield


def atomic_write(path, payload, *, preserve_owner=False):
    path = Path(path)
    if path.is_symlink() or path.parent.resolve() != path.parent.absolute():
        raise RotationError('private-path')
    previous = path.stat() if path.exists() else None
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if os.name == 'nt':
                windows_permissions(temporary, establish=True)
            if preserve_owner and previous is not None and os.name != 'nt':
                os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_json(path, data):
    payload = (json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n').encode('utf-8')
    atomic_write(path, payload)


def read_json(path):
    private_file(path)
    if path.stat().st_size > MAX_MESSAGE:
        raise RotationError('invalid-plan')
    return json.loads(path.read_text(encoding='utf-8'))


def normalized_guid(value):
    result = str(value).replace('-', '').lower()
    if not GUID.fullmatch(result):
        raise RotationError('native-identity')
    return result


def validate_inventory(inventory):
    if inventory.get('unsupported') != 0:
        raise RotationError('unsupported-account')
    jellyfin, seerr = inventory.get('jellyfin'), inventory.get('seerr')
    if not isinstance(jellyfin, list) or not jellyfin or not isinstance(seerr, list):
        raise RotationError('invalid-plan')
    seen, known = set(), set()
    for service, rows in [('jellyfin', jellyfin), ('seerr', seerr)]:
        for row in rows:
            identifier = row['id']
            if service == 'jellyfin':
                if normalized_guid(identifier) != identifier:
                    raise RotationError('invalid-plan')
                known.add(identifier)
            elif type(identifier) is not int or identifier < 1:
                raise RotationError('invalid-plan')
            name = row['username']
            if (not isinstance(name, str) or not 1 <= len(name) <= 512
                    or any(ord(c) < 32 for c in name) or (service, identifier) in seen):
                raise RotationError('invalid-plan')
            seen.add((service, identifier))
    for row in seerr:
        if row['jellyfin_id'] is not None and row['jellyfin_id'] not in known:
            raise RotationError('account-link')
    owner = inventory.get('controller_admin_id')
    if not any(row['id'] == owner and row['admin'] and not row['disabled'] for row in jellyfin):
        raise RotationError('native-identity')


def same_inventory(left, right):
    def stable(value):
        result = dict(value)
        result['seerr'] = [row | {'has_password': True} if row['jellyfin_id'] is None else row
                           for row in value['seerr']]
        return result
    return stable(left) == stable(right)


def password():
    while True:
        result = ''.join(secrets.choice(PASSWORD_CHARS) for _ in range(24))
        if (any(c.isupper() for c in result) and any(c.islower() for c in result)
                and any(c.isdigit() for c in result) and any(c in '!@#%+=_-' for c in result)):
            return result


def text_credentials(plan):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';', lineterminator='\n')
    writer.writerow(['service', 'user', 'password'])
    for row in plan['credentials']:
        writer.writerow([row['service'], row['username'], row['password']])
    return output.getvalue().encode('utf-8')


def validate_plan(plan):
    if plan.get('version') != 1 or not re.fullmatch(r'[a-f0-9]{32}', plan.get('id', '')):
        raise RotationError('invalid-plan')
    validate_inventory(plan['inventory'])
    expected = {('jellyfin', row['id']): row['username'] for row in plan['inventory']['jellyfin']}
    expected.update({('seerr', row['id']): row['username']
                     for row in plan['inventory']['seerr'] if row['jellyfin_id'] is None})
    seen, values = set(), set()
    for row in plan['credentials']:
        identity = row['service'], row['id']
        value = row['password']
        if (identity in seen or expected.get(identity) != row['username']
                or not isinstance(value, str) or len(value) != 24 or value in values
                or any(c not in PASSWORD_CHARS for c in value)):
            raise RotationError('invalid-plan')
        seen.add(identity)
        values.add(value)
    if seen != set(expected):
        raise RotationError('invalid-plan')


def prepare(directory, inventory):
    validate_inventory(inventory)
    directory = secure_directory(directory)
    journal = directory / 'rotation.private.json'
    if journal.exists():
        plan = read_json(journal)
        validate_plan(plan)
        if not same_inventory(plan['inventory'], inventory):
            raise RotationError('inventory-changed')
    else:
        if (directory / 'credentials.txt').exists():
            raise RotationError('credential-file')
        rows, used = [], set()
        for service in ('jellyfin', 'seerr'):
            for row in inventory[service]:
                if service == 'seerr' and row['jellyfin_id'] is not None:
                    continue
                value = password()
                while value in used:
                    value = password()
                used.add(value)
                rows.append({'service': service, 'id': row['id'],
                             'username': row['username'], 'password': value})
        plan = {'version': 1, 'id': uuid.uuid4().hex, 'inventory': inventory, 'credentials': rows}
        # Persist the plan first so a crash before TXT creation can recover the
        # exact generated passwords. Apply separately requires both durable files.
        save_json(journal, plan)
    target = directory / 'credentials.txt'
    expected = text_credentials(plan)
    if target.exists():
        private_file(target)
        if target.read_bytes() != expected:
            raise RotationError('credential-file')
    else:
        atomic_write(target, expected)
    return plan


def apply_local(directory, transport, *, verify=False, local_env=None):
    directory = secure_directory(directory)
    plan = read_json(directory / 'rotation.private.json')
    validate_plan(plan)
    target = directory / 'credentials.txt'
    private_file(target)
    if target.read_bytes() != text_credentials(plan):
        raise RotationError('credential-file')
    result = transport({'operation': 'verify' if verify else 'apply', 'plan': plan,
                        'protected_client': True})
    updates = result.pop('private_env', None)
    if updates is not None:
        if local_env is None:
            raise RotationError('private-path')
        update_env(local_env, updates)
    result = {key: result[key] for key in ('complete', 'passwords', 'identities')
              if type(result.get(key)) is int}
    save_json(directory / 'result.private.json', result)
    return result


def password_targets(plan):
    for credential in plan['credentials']:
        service, identifier = credential['service'], credential['id']
        source = next(row for row in plan['inventory'][service] if row['id'] == identifier)
        yield credential | {'disabled': source.get('disabled', False)}
        if service == 'jellyfin':
            for linked in plan['inventory']['seerr']:
                if linked['jellyfin_id'] == identifier and linked['has_password']:
                    yield {'service': 'seerr', 'id': linked['id'], 'username': linked['username'],
                           'password': credential['password'], 'disabled': False}


def apply_remote(plan, native, directory, *, verify=False):
    validate_plan(plan)
    directory = secure_directory(directory)
    journal_path = directory / (plan['id'] + '.json')
    fingerprint = digest(plan)
    journal = read_json(journal_path) if journal_path.exists() else {
        'fingerprint': fingerprint, 'done': [], 'stage': 'prepared'}
    if journal.get('fingerprint') != fingerprint:
        raise RotationError('journal-mismatch')
    native.check_preconditions(journal['stage'])
    if not same_inventory(native.inventory(), plan['inventory']):
        raise RotationError('inventory-changed')
    targets = list(password_targets(plan))
    if journal['stage'] == 'complete' and not verify:
        return rotation_result(plan, native, targets)
    if verify and journal['stage'] != 'complete':
        raise RotationError('preconditions')
    save_json(journal_path, journal)
    if not verify:
        native.ensure_controller_key(plan['id'])
    for row in targets:
        identity = row['service'] + ':' + str(row['id'])
        if not verify and identity not in journal['done']:
            journal['stage'] = ('owner-password' if row['service'] == 'seerr' and row['id'] == 1
                                else 'rotating')
            save_json(journal_path, journal)
            native.rotate(row['service'], row['id'], row['password'])
        native.verify_password(row['service'], row['id'], row['username'], row['password'],
                               disabled=row['disabled'])
        if identity not in journal['done']:
            journal['done'].append(identity)
            save_json(journal_path, journal)
    if not verify:
        administrator = next(row for row in plan['credentials'] if row['service'] == 'jellyfin'
                             and row['id'] == plan['inventory']['controller_admin_id'])
        native.persist_admin_password(administrator['password'])
        journal['stage'] = 'clearing-sessions'
        save_json(journal_path, journal)
        native.clear_sessions()
    native.verify_preserved(plan['inventory'])
    journal['stage'] = 'complete'
    save_json(journal_path, journal)
    return rotation_result(plan, native, targets)


def rotation_result(plan, native, targets):
    result = {'complete': 1, 'passwords': len(targets), 'identities': len(plan['credentials'])}
    if hasattr(native, 'private_env_updates'):
        result['private_env'] = native.private_env_updates()
    return result


def update_env(path, updates):
    if set(updates) - {'HOMESERVER_JELLYFIN_API_KEY', 'HOMESERVER_JELLYFIN_ADMIN_PASSWORD'}:
        raise RotationError('invalid-operation')
    original = path.read_text(encoding='utf-8')
    lines, seen = [], set()
    for line in original.splitlines(keepends=True):
        key = line.split('=', 1)[0].strip() if '=' in line else ''
        if key in updates:
            if key in seen:
                raise RotationError('invalid-plan')
            seen.add(key)
            lines.append(key + '=' + json.dumps(updates[key]) + '\n')
        else:
            lines.append(line)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    for key in updates.keys() - seen:
        lines.append(key + '=' + json.dumps(updates[key]) + '\n')
    atomic_write(path, ''.join(lines).encode('utf-8'), preserve_owner=True)


def clear_seerr_database(path, *, stopped):
    if not stopped:
        raise RotationError('seerr-running')
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file():
        raise RotationError('private-path')
    with closing(sqlite3.connect(path, timeout=10)) as database, database:
        columns = {row[1] for row in database.execute('PRAGMA table_info(user)')}
        session_columns = {row[1] for row in database.execute('PRAGMA table_info(session)')}
        if (not {'id', 'json', 'expiredAt', 'destroyedAt'} <= session_columns
                or not {'jellyfinAuthToken', 'resetPasswordGuid', 'recoveryLinkExpirationDate'}
                <= columns):
            raise RotationError('database-schema')
        database.execute('BEGIN IMMEDIATE')
        database.execute('DELETE FROM session')
        database.execute('UPDATE user SET jellyfinAuthToken=NULL, resetPasswordGuid=NULL, '
                         'recoveryLinkExpirationDate=NULL')
        if (database.execute('SELECT count(*) FROM session').fetchone()[0] != 0
                or database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]):
            raise RotationError('database-schema')


def set_owner_hash(path, hashed, *, stopped):
    if not stopped:
        raise RotationError('seerr-running')
    if (path.is_symlink() or path.resolve() != path.absolute() or not path.is_file()
            or not re.fullmatch(r'\$2[ab]\$12\$[./A-Za-z0-9]{53}', hashed)):
        raise RotationError('private-path')
    with closing(sqlite3.connect(path, timeout=10)) as database, database:
        database.execute('BEGIN IMMEDIATE')
        if database.execute('SELECT count(*) FROM user WHERE id=1').fetchone()[0] != 1:
            raise RotationError('native-identity')
        database.execute('UPDATE user SET password=? WHERE id=1', (hashed,))
        if database.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RotationError('database-schema')


def native_password_hash(value):
    source = (
        'const fs=require("fs");const bcrypt=require("bcrypt");'
        'const p=JSON.parse(fs.readFileSync(0,"utf8")).password;'
        'bcrypt.hash(p,12).then(async h=>{if(!await bcrypt.compare(p,h))process.exit(1);'
        'process.stdout.write(JSON.stringify({hash:h}));}).catch(()=>process.exit(1));'
    )
    output = command([
        'docker', 'run', '--rm', '-i', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '128m',
        '--user', '1000:1000', '--workdir', '/app', '--entrypoint', 'node', IMAGES['seerr'],
        '-e', source,
    ], payload=json.dumps({'password': value}).encode('utf-8'))
    hashed = json.loads(output)['hash']
    if not isinstance(hashed, str) or not re.fullmatch(r'\$2[ab]\$12\$[./A-Za-z0-9]{53}', hashed):
        raise RotationError('native-api')
    return hashed


def ssh_transport(payload):
    response = command([
        'ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'ConnectTimeout=8', HOST, 'python3',
        (PROJECT / 'scripts/security-credentials.py').as_posix(), '--remote',
    ], payload=json.dumps(payload).encode('utf-8'), timeout=1800)
    if len(response) > MAX_MESSAGE:
        raise RotationError('transport')
    result = json.loads(response)
    if result.get('ok') is False:
        raise RotationError(result.get('error'))
    return result


def main():
    parser = argparse.ArgumentParser(description='Private one-off credential rotation')
    actions = parser.add_mutually_exclusive_group(required=True)
    for action in ('prepare', 'apply', 'verify', 'remote'):
        actions.add_argument('--' + action, action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--local-env', type=Path)
    args = parser.parse_args()
    if args.remote:
        # RemoteNative is defined below; direct terminal invocation is forbidden.
        if sys.stdin.isatty() or os.name != 'posix' or os.geteuid() != 0:
            raise RotationError('preconditions')
        raw = sys.stdin.buffer.read(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE:
            raise RotationError('invalid-plan')
        request = json.loads(raw)
        if request.get('protected_client') is not True:
            raise RotationError('preconditions')
        os.umask(0o077)
        journal = secure_directory(PRIVATE / 'rotation-journal')
        with coordinator_lock(journal):
            native = RemoteNative()
            operation = request.get('operation')
            if operation == 'inventory':
                native.check_preconditions('inventory')
                return native.inventory()
            if operation not in {'apply', 'verify'}:
                raise RotationError('invalid-operation')
            return apply_remote(request['plan'], native, journal, verify=operation == 'verify')
    if not args.output:
        raise RotationError('private-path')
    directory = secure_directory(args.output)
    with coordinator_lock(directory):
        if args.prepare:
            inventory = ssh_transport({'operation': 'inventory', 'protected_client': True})
            plan = prepare(directory, inventory)
            return {'prepared': len(plan['credentials']),
                    'linked': sum(row['jellyfin_id'] is not None for row in inventory['seerr'])}
        if args.local_env is None:
            raise RotationError('private-path')
        private_file(args.local_env.absolute())
        return apply_local(directory, ssh_transport, verify=args.verify, local_env=args.local_env)


# The native adapter is intentionally below the pure coordinator so tests can
# exercise journaling and filesystem safety without Docker or private data.

REQUIRED_RECEIPT = (
    'checkpoint_verified', 'migration_verified', 'private_admin_access_verified',
    'public_edge_maintenance_verified', 'jellyfin_session_logging_suppressed',
    'seerr_dedicated_jellyfin_key_verified', 'all_jellyfin_accounts_are_human',
)
IMAGES = {
    'jellyfin': 'jellyfin/jellyfin@sha256:'
    '357724bf0ae27a672c7cbaa899db2d9abeb13dbd8657ccce750258a4c059d037',
    'seerr': 'ghcr.io/seerr-team/seerr@sha256:'
    '27602401178d54f1964442287b9f23f67a3fa2252645ee8065839ea1c3f69e45',
}
SESSION_LOG_CATEGORY = 'Emby.Server.Implementations.Session.SessionManager'


def validate_receipt(receipt, *, now):
    if (receipt.get('version') != 1 or receipt.get('host') != 'homelab'
            or type(receipt.get('verified_at')) not in {int, float}
            or not 0 <= now - receipt['verified_at'] <= 3600
            or any(receipt.get(key) is not True for key in REQUIRED_RECEIPT)):
        raise RotationError('preconditions')


def read_env(path):
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', key) or key in result:
            raise RotationError('invalid-plan')
        if value.startswith('"'):
            value = json.loads(value)
        elif value.startswith("'"):
            if not value.endswith("'"):
                raise RotationError('invalid-plan')
            value = value[1:-1]
        if not isinstance(value, str):
            raise RotationError('invalid-plan')
        result[key] = value
    return result


class NativeStatus(RotationError):
    def __init__(self, status):
        self.status = status
        super().__init__('native-api')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise NativeStatus(code)


class NativeApi:
    def __init__(self, base, *, key='', seerr=False):
        if base not in {'http://127.0.0.1:8096/jellyfin', SEERR_NATIVE}:
            raise RotationError('native-api')
        self.base, self.key, self.seerr = base, key, seerr
        self.cookies = {}
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None, *, headers=None, authenticated=True):
        if not path.startswith('/') or path.startswith('//') or '#' in path or '\\' in path:
            raise RotationError('native-api')
        if self.seerr and method not in {'GET', 'HEAD'} and '_csrf' not in self.cookies:
            self.request('GET', '/settings/public', authenticated=False)
        outgoing = {'Accept': 'application/json'}
        if authenticated and self.key:
            if self.seerr:
                outgoing['X-Api-Key'] = self.key
            else:
                if not re.fullmatch(r'[A-Za-z0-9._~+\-]{1,2048}', self.key):
                    raise RotationError('native-api')
                outgoing['Authorization'] = (
                    'MediaBrowser Client="HomeServerSecurity", Device="Private rotation", '
                    'DeviceId="homeserver-security-controller", Version="1", '
                    f'Token="{self.key}"')
        if self.cookies:
            outgoing['Cookie'] = '; '.join(key + '=' + value for key, value in self.cookies.items())
        if self.seerr and method not in {'GET', 'HEAD'} and 'XSRF-TOKEN' in self.cookies:
            outgoing['X-CSRF-Token'] = unquote(self.cookies['XSRF-TOKEN'])
        outgoing.update(headers or {})
        data = None if body is None else json.dumps(body).encode('utf-8')
        if data is not None:
            outgoing['Content-Type'] = 'application/json'
        request = Request(self.base + path, data=data, headers=outgoing, method=method)
        try:
            with self.opener.open(request, timeout=15) as response:
                for value in response.headers.get_all('Set-Cookie', []):
                    parsed = SimpleCookie()
                    parsed.load(value)
                    for key, morsel in parsed.items():
                        if key in {'_csrf', 'XSRF-TOKEN', 'connect.sid'}:
                            self.cookies[key] = morsel.value
                raw = response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise RotationError('native-api')
                return None if not raw else json.loads(raw)
        except HTTPError as error:
            status = error.code
            error.close()
            raise NativeStatus(status) from None
        except (URLError, ValueError, OSError):
            raise RotationError('native-api') from None


def inspect_container(service):
    return json.loads(command(['docker', 'inspect', 'homeserver-' + service + '-1']))[0]


def wait_ready(api, path, *, attempts=90):
    for _ in range(attempts):
        try:
            api.request('GET', path)
            return
        except RotationError:
            time.sleep(2)
    raise RotationError('native-api')


def revoke_devices(api, identifiers):
    requested = set(identifiers)

    def remaining():
        return {row['Id'] for row in api.request('GET', '/Devices').get('Items', [])}

    for identifier in requested & remaining():
        try:
            api.request('DELETE', '/Devices?Id=' + quote(identifier, safe=''))
        except NativeStatus as error:
            # Password updates already revoke devices; Jellyfin returns 400 for
            # an ID removed between this read and deletion. Verify absence first.
            if error.status != 400 or identifier in remaining():
                raise
    if requested & remaining():
        raise RotationError('native-api')


def verify_seerr_logout(session, cookie):
    if not isinstance(cookie, str) or not re.fullmatch(r'[A-Za-z0-9%._~:+/=-]{1,4096}', cookie):
        raise RotationError('native-api')
    # connect-typeorm 2.0.0 soft-deletes asynchronously. Replay the original
    # cookie explicitly, even if logout cleared or replaced the client's cookie.
    for attempt in range(8):
        try:
            session.request('GET', '/auth/me', authenticated=False,
                            headers={'Cookie': 'connect.sid=' + cookie})
        except NativeStatus as error:
            # Seerr v3.5.0 isAuthenticated() uses 403 for absent sessions.
            if error.status == 403:
                return
            raise
        if attempt < 7:
            time.sleep(0.25)
    raise RotationError('native-api')


class RemoteNative:
    def __init__(self):
        self.env_path = PROJECT / '.env'
        self.values = read_env(self.env_path)
        self.settings_path = Path('/srv/appdata/seerr/settings.json')
        self.seerr_db = Path('/srv/appdata/seerr/db/db.sqlite3')
        self.settings = json.loads(self.settings_path.read_text(encoding='utf-8'))
        self.jellyfin = NativeApi('http://127.0.0.1:8096/jellyfin',
                                 key=self.values.get('HOMESERVER_JELLYFIN_API_KEY', ''))
        self.seerr = NativeApi(SEERR_NATIVE, seerr=True,
                              key=self.settings.get('main', {}).get('apiKey', ''))
        if not self.jellyfin.key or not self.seerr.key:
            raise RotationError('preconditions')
        self.private_updates = {}
        self.old_devices = []
        self.old_tokens = []
        self.before_history = None

    def check_preconditions(self, stage):
        receipt = read_json(PRIVATE / 'rotation-preconditions.private.json')
        validate_receipt(receipt, now=time.time())
        if socket.gethostname() != 'homelab':
            raise RotationError('preconditions')
        containers = {}
        for service, expected in IMAGES.items():
            container = inspect_container(service)
            if (container['Config']['Image'] != expected
                    or container['Config']['Labels'].get('com.docker.compose.service') != service
                    or receipt.get('images', {}).get(service) != expected):
                raise RotationError('preconditions')
            target = '/config' if service == 'jellyfin' else '/app/config'
            mounts = [row for row in container['Mounts'] if row['Destination'] == target]
            if (len(mounts) != 1 or mounts[0]['Type'] != 'bind'
                    or mounts[0]['Source'] != '/srv/appdata/' + service):
                raise RotationError('preconditions')
            if not container['State']['Running']:
                # A killed helper can resume exactly its recorded Seerr cleanup.
                if service == 'seerr' and stage in {'clearing-sessions', 'owner-password'}:
                    command(['docker', 'start', 'homeserver-seerr-1'])
                    wait_ready(self.seerr, '/status')
                    container = inspect_container(service)
                else:
                    raise RotationError('preconditions')
            if service == 'seerr':
                networks = container['NetworkSettings']['Networks']
                native_network = [network for name, network in networks.items()
                                  if name == 'homeserver_public_seerr']
                if (len(native_network) != 1
                        or native_network[0].get('IPAddress') != '172.30.240.18'):
                    raise RotationError('preconditions')
            containers[service] = container
        database = Path('/srv/appdata/control/control.sqlite')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            row = db.execute('SELECT state,last_seen_at,cycle_deadline_at,initialized '
                             'FROM worker_heartbeat WHERE id=1').fetchone()
        if not (row and row[0] == 'maintenance' and 0 <= time.time() - row[1] <= 30
                and row[2] is None and row[3] == 1):
            raise RotationError('preconditions')
        logging = Path('/srv/appdata/jellyfin/config/logging.json')
        config = json.loads(logging.read_text(encoding='utf-8'))
        level = config.get('Serilog', {}).get('MinimumLevel', {}).get('Override', {}).get(
            SESSION_LOG_CATEGORY)
        started = containers['jellyfin']['State']['StartedAt']
        # Python accepts RFC3339 nano-second precision after truncation.
        started = re.sub(r'(\.\d{6})\d+', r'\1', started).replace('Z', '+00:00')
        if (level not in {'Warning', 'Error', 'Fatal'}
                or datetime.fromisoformat(started).timestamp() < logging.stat().st_mtime):
            raise RotationError('preconditions')
        keys = self.jellyfin.request('GET', '/Auth/Keys')['Items']
        integration_key = self.settings.get('jellyfin', {}).get('apiKey')
        if not integration_key or not any(
            row.get('AccessToken') == integration_key for row in keys
        ):
            raise RotationError('preconditions')
        self.jellyfin.request('GET', '/System/Info')
        owner = self.seerr.request('GET', '/auth/me')
        if owner.get('id') != 1:
            raise RotationError('preconditions')
        devices = self.jellyfin.request('GET', '/Devices')
        self.old_devices = [row['Id'] for row in devices.get('Items', [])]
        with closing(sqlite3.connect(
            self.seerr_db.as_uri() + '?mode=ro', uri=True, timeout=5,
        )) as db:
            self.old_tokens = [row[0] for row in db.execute(
                'SELECT jellyfinAuthToken FROM user WHERE jellyfinAuthToken IS NOT NULL') if row[0]]
            self.before_history = db.execute(
                'SELECT id,requestedById FROM media_request ORDER BY id').fetchall()

    def inventory(self):
        users = self.jellyfin.request('GET', '/Users')
        result = {'jellyfin': [], 'seerr': [], 'unsupported': 0, 'controller_admin_id': None}
        administrator = self.values.get('HOMESERVER_ADMIN_USERNAME', 'root')
        for row in users:
            policy = row.get('Policy', {})
            provider = policy.get('AuthenticationProviderId')
            if provider and provider not in {
                'Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider',
                'Emby.Server.Implementations.Library.DefaultAuthenticationProvider',
            }:
                result['unsupported'] += 1
            identifier = normalized_guid(row['Id'])
            result['jellyfin'].append({
                'id': identifier, 'username': row['Name'],
                'admin': policy.get('IsAdministrator') is True,
                'disabled': policy.get('IsDisabled') is True,
            })
            if row['Name'] == administrator:
                result['controller_admin_id'] = identifier
        offset = 0
        while True:
            page = self.seerr.request('GET', f'/user?take=100&skip={offset}')
            rows = page.get('results', [])
            if not isinstance(rows, list):
                raise RotationError('native-api')
            for row in rows:
                if row.get('userType') not in {2, 3}:
                    result['unsupported'] += 1
                linked = row.get('jellyfinUserId')
                if row.get('userType') == 3 and not linked:
                    result['unsupported'] += 1
                has_password = self.seerr.request('GET', f"/user/{row['id']}/settings/password")
                result['seerr'].append({
                    'id': row['id'], 'username': row['email'], 'permissions': row['permissions'],
                    'jellyfin_id': normalized_guid(linked) if linked else None,
                    'has_password': has_password.get('hasPassword') is True,
                })
            offset += len(rows)
            if len(rows) < 100:
                break
            if offset > 10000:
                raise RotationError('native-api')
        result['jellyfin'].sort(key=lambda row: row['id'])
        result['seerr'].sort(key=lambda row: row['id'])
        return result

    def ensure_controller_key(self, identifier):
        label = 'HomeServer-controller-security-' + identifier
        keys = self.jellyfin.request('GET', '/Auth/Keys')['Items']
        matches = [row for row in keys if row.get('AppName') == label]
        if not matches:
            self.jellyfin.request('POST', '/Auth/Keys?App=' + quote(label, safe=''))
            keys = self.jellyfin.request('GET', '/Auth/Keys')['Items']
            matches = [row for row in keys if row.get('AppName') == label]
        if len(matches) != 1 or not isinstance(matches[0].get('AccessToken'), str):
            raise RotationError('native-api')
        key = matches[0]['AccessToken']
        if key == self.settings['jellyfin']['apiKey']:
            raise RotationError('preconditions')
        update_env(self.env_path, {'HOMESERVER_JELLYFIN_API_KEY': key})
        self.jellyfin.key = key
        self.jellyfin.request('GET', '/Users')
        self.private_updates['HOMESERVER_JELLYFIN_API_KEY'] = key

    def rotate(self, service, identifier, value):
        if service == 'jellyfin':
            identifier = normalized_guid(identifier)
            user = self.jellyfin.request('GET', '/Users/' + identifier)
            if (normalized_guid(user['Id']) != identifier
                    or not isinstance(user.get('Policy'), dict)):
                raise RotationError('native-identity')
            # Jellyfin v12.2 maps -1 to no permanent lockout; 0 means three
            # attempts. Public login cooldowns are enforced by the guard.
            policy = user['Policy'] | {'IsHidden': True, 'LoginAttemptsBeforeLockout': -1}
            self.jellyfin.request('POST', '/Users/' + identifier + '/Policy', policy)
            self.jellyfin.request('POST', '/Users/Password?userId=' + identifier,
                                  {'NewPw': value, 'ResetPassword': False})
        elif service == 'seerr' and type(identifier) is int and identifier > 0:
            if identifier == 1:
                # The API key acts as owner id1, whose self-change can require
                # an unknown current local password. Use the exact native bcrypt
                # implementation, update only that hash while the store is stopped.
                hashed = native_password_hash(value)
                command(['docker', 'stop', '--time', '60', 'homeserver-seerr-1'], timeout=90)
                if inspect_container('seerr')['State']['Running']:
                    raise RotationError('seerr-running')
                try:
                    set_owner_hash(self.seerr_db, hashed, stopped=True)
                finally:
                    command(['docker', 'start', 'homeserver-seerr-1'])
                wait_ready(self.seerr, '/status')
            else:
                self.seerr.request('POST', f'/user/{identifier}/settings/password',
                                   {'newPassword': value})
        else:
            raise RotationError('invalid-plan')

    def verify_password(self, service, identifier, username, value, disabled=False):
        if disabled:
            user = self.jellyfin.request('GET', '/Users/' + normalized_guid(identifier))
            if not user['Policy']['IsDisabled']:
                raise RotationError('native-identity')
            return  # Never enable a disabled account merely to verify its password.
        if service == 'jellyfin':
            device = 'homeserver-security-verification-' + uuid.uuid4().hex
            metadata = {'Authorization': 'MediaBrowser Client="HomeServerSecurity", '
                        f'Device="Private verification", DeviceId="{device}", Version="1"'}
            auth = self.jellyfin.request('POST', '/Users/AuthenticateByName',
                                         {'Username': username, 'Pw': value},
                                         authenticated=False, headers=metadata)
            try:
                if normalized_guid(auth['User']['Id']) != identifier:
                    raise RotationError('native-identity')
            finally:
                # DELETE also revokes the fresh verification token and any device session.
                self.jellyfin.request('DELETE', '/Devices?Id=' + quote(device, safe=''))
        else:
            session = NativeApi(SEERR_NATIVE, seerr=True)
            auth = session.request('POST', '/auth/local', {'email': username, 'password': value})
            cookie = session.cookies.get('connect.sid')
            try:
                if auth.get('id') != identifier:
                    raise RotationError('native-identity')
            finally:
                session.request('POST', '/auth/logout')
                verify_seerr_logout(session, cookie)

    def persist_admin_password(self, value):
        update_env(self.env_path, {'HOMESERVER_JELLYFIN_ADMIN_PASSWORD': value})
        self.private_updates['HOMESERVER_JELLYFIN_ADMIN_PASSWORD'] = value

    def clear_sessions(self):
        revoke_devices(self.jellyfin, self.old_devices)
        for token in self.old_tokens:
            probe = NativeApi('http://127.0.0.1:8096/jellyfin', key=token)
            try:
                probe.request('GET', '/Users/Me')
            except NativeStatus as error:
                if error.status != 401:
                    raise
            else:
                raise RotationError('native-api')
        command(['docker', 'stop', '--time', '60', 'homeserver-seerr-1'], timeout=90)
        if inspect_container('seerr')['State']['Running']:
            raise RotationError('seerr-running')
        try:
            clear_seerr_database(self.seerr_db, stopped=True)
        finally:
            command(['docker', 'start', 'homeserver-seerr-1'])
        wait_ready(self.seerr, '/status')

    def verify_preserved(self, before):
        if not same_inventory(self.inventory(), before):
            raise RotationError('inventory-changed')
        users = self.jellyfin.request('GET', '/Users')
        for row in users:
            policy = row.get('Policy')
            if not isinstance(policy, dict):
                raise RotationError('native-identity')
            lockout = policy.get('LoginAttemptsBeforeLockout')
            if (policy.get('IsHidden') is not True
                    or type(lockout) is not int or lockout != -1):
                raise RotationError('native-identity')
        remaining = {row['Id'] for row in self.jellyfin.request('GET', '/Devices').get('Items', [])}
        if remaining & set(self.old_devices):
            raise RotationError('native-api')
        with closing(sqlite3.connect(
            self.seerr_db.as_uri() + '?mode=ro', uri=True, timeout=5,
        )) as db:
            after = db.execute('SELECT id,requestedById FROM media_request ORDER BY id').fetchall()
            sessions = db.execute('SELECT count(*) FROM session '
                                  'WHERE destroyedAt IS NULL AND expiredAt > ?',
                                  (int(time.time() * 1000),)).fetchone()[0]
        if self.before_history != after or sessions:
            raise RotationError('native-identity')

    def private_env_updates(self):
        values = read_env(self.env_path)
        return {key: values[key] for key in (
            'HOMESERVER_JELLYFIN_API_KEY', 'HOMESERVER_JELLYFIN_ADMIN_PASSWORD')}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except BaseException as error:
        # Do not let traceback rendering, HTTP response bodies, command stderr or
        # interruption exceptions disclose account names, passwords or API keys.
        print(json.dumps(safe_error(error)))
        raise SystemExit(1) from None
