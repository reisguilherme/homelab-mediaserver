"""Exercise the shipped Nginx ingress, real ASGI guard and native HTTP fixtures."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn


def unused_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait_port(port, process=None):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process and process.poll() is not None:
            pytest.fail('Nginx rejected the isolated fixture configuration')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.02)
    pytest.fail('Fixture listener did not start')


@contextmanager
def nginx_command(temp):
    nginx = os.environ.get('HOMESERVER_TEST_NGINX') or shutil.which('nginx')
    image = ('nginx@sha256:0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94')
    if nginx:
        command = [nginx]
    elif shutil.which('docker') and os.name == 'posix':
        available = subprocess.run(['docker', 'image', 'inspect', image],
                                   capture_output=True, timeout=5)
        if available.returncode:
            pytest.skip('The pinned Nginx fixture image is not available locally')
        command = ['docker', 'run', '--rm', '--network', 'host', '--read-only',
                   '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                   '--user', str(os.getuid()), '--entrypoint', 'nginx',
                   '--mount', f'type=bind,source={temp},target={temp}',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', image]
    else:
        pytest.skip('A real Nginx binary or the pinned fixture Docker image is required')
    yield command


@contextmanager
def start_public_proxy(temp):
    from homeserver_control.api.public_access import PublicAccessSettings, create_app

    with nginx_command(temp) as command:
        pass
    calls = []
    login_payloads = []
    # Nginx can unlink request temp files while retaining an open descriptor.
    # inotify detects creation even when directory snapshots cannot see them.
    body_dir = temp / 'body'
    body_dir.mkdir()
    libc = ctypes.CDLL('libc.so.6', use_errno=True)
    notify = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    assert notify >= 0
    assert libc.inotify_add_watch(notify, os.fsencode(body_dir), 0x100) >= 0

    def body_events():
        try:
            return os.read(notify, 65536)
        except BlockingIOError:
            return b''

    class Native(BaseHTTPRequestHandler):
        def respond(self):
            length = int(self.headers.get('content-length', '0'))
            body = self.rfile.read(length) if length else b''
            calls.append((self.command, self.path))
            headers = {key.lower(): value for key, value in self.headers.items()}
            status, cookies = 200, []
            payload = {'path': self.path, 'method': self.command,
                       'headers': headers, 'upstream': self.server.label}
            if self.path == '/jellyfin/Users/Me':
                auth = self.headers.get('authorization', '')
                if 'viewer-token' in auth or 'admin-token' in auth:
                    payload = {'Id': 'viewer', 'Policy': {'IsDisabled': False,
                        'IsAdministrator': 'admin-token' in auth}}
                else:
                    status, payload = 401, {'error': 'Invalid session'}
            elif self.path == '/jellyfin/Users/AuthenticateByName':
                credentials = json.loads(body)
                if credentials['Pw'] != 'correct':
                    status, payload = 401, {'error': 'Invalid credentials'}
                else:
                    payload = {'AccessToken': credentials['Username'] + '-token'}
            elif self.path == '/api/v1/auth/me':
                cookie = self.headers.get('cookie', '')
                if any(f'connect.sid={name}' in cookie
                       for name in ('viewer', 'admin', 'advanced', 'moderator')):
                    permissions = 2 if 'admin' in cookie else (
                        32 | (8192 if 'advanced' in cookie else 0)
                        | (16 if 'moderator' in cookie else 0))
                    payload = {'id': 4, 'permissions': permissions}
                else:
                    status, payload = 403, {'error': 'Invalid session'}
            elif self.path in {'/api/v1/auth/local', '/api/v1/auth/jellyfin'}:
                credentials = json.loads(body)
                login_payloads.append(credentials)
                if (self.headers.get('x-xsrf-token') != 'fixture-csrf'
                        or self.headers.get('cookie') != '_csrf=fixture-seed'):
                    status, payload = 403, {'error': 'Invalid CSRF'}
                elif credentials['password'] != 'correct':
                    status, payload = 403, {'error': 'Invalid credentials'}
                else:
                    name = credentials.get('username') or credentials.get('email')
                    cookies = [f'connect.sid={name}; Path=/; HttpOnly; SameSite=Lax',
                               'XSRF-TOKEN=fixture-csrf; Path=/; SameSite=Strict']
                    payload = {'id': 4, 'permissions': 32}
            elif self.path == '/api/v1/settings/discover':
                payload = [{'id': 1, 'type': 3, 'enabled': True}]
            elif self.path == '/api/v1/overrideRule/advancedRequest':
                if 'connect.sid=advanced' not in self.headers.get('cookie', ''):
                    status, payload = 403, {'error': 'Permission required'}
                else:
                    payload = {'profileId': 1, 'rootFolder': '/fixture/media'}
            elif self.path in {'/api/v1/request/42/approve', '/api/v1/request/42/decline'}:
                # Seerr v3.5.0 request.ts applies MANAGE_REQUESTS on these POST routes.
                if (self.command != 'POST'
                        or 'connect.sid=moderator' not in self.headers.get('cookie', '')):
                    status, payload = 403, {'error': 'Native MANAGE_REQUESTS permission required'}
                else:
                    payload = {'id': 42, 'status': 2 if self.path.endswith('/approve') else 3}
            elif self.path.split('?')[0] == '/jellyfin/socket':
                self.send_response(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                key = self.headers.get('sec-websocket-key', '')
                accept = base64.b64encode(hashlib.sha1(
                    (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
                self.send_header('Sec-WebSocket-Accept', accept.decode())
                self.end_headers()
                return
            elif 'range' in headers:
                status = 206
            encoded = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Content-Type', 'application/json')
            for cookie in cookies:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(encoded)

        do_GET = respond
        do_HEAD = respond
        do_POST = respond
        do_DELETE = respond
        do_PUT = respond
        do_PATCH = respond
        do_OPTIONS = respond

        def log_message(self, *_args):
            pass

    servers, threads = [], []
    for label in ('jellyfin', 'seerr', 'relay'):
        native = ThreadingHTTPServer(('127.0.0.1', 0), Native)
        native.label = label
        thread = threading.Thread(target=native.serve_forever, kwargs={'poll_interval': 0.05},
                                  daemon=True)
        thread.start()
        servers.append(native)
        threads.append(thread)
    jf, seerr, relay = [server.server_port for server in servers]
    guard_port, jellyfin_port, seerr_port = unused_port(), unused_port(), unused_port()
    app = create_app(PublicAccessSettings(
        jellyfin_url=f'http://127.0.0.1:{jf}/jellyfin', seerr_url=f'http://127.0.0.1:{seerr}',
        state_path=temp / 'limits.sqlite3', trusted_proxy_ips=('127.0.0.1',),
        seerr_origin='https://private-requests.mywire.org:54443',
    ))
    guard = uvicorn.Server(uvicorn.Config(
        app, host='127.0.0.1', port=guard_port,
        access_log=False, log_level='critical', proxy_headers=False))
    guard_thread = threading.Thread(target=guard.run, daemon=True)
    guard_thread.start()
    process = None
    try:
        wait_port(guard_port)
        config = Path('deploy/public-proxy/nginx.conf').read_text()
        for old, new in {
            'worker_processes 2;': 'worker_processes 1;',
            'pid /tmp/nginx.pid;': f'pid {temp}/nginx.pid;',
            'listen 8097': f'listen 127.0.0.1:{jellyfin_port}',
            'listen 5056': f'listen 127.0.0.1:{seerr_port}',
            '192.168.1.24': '127.0.0.2',
            '172.30.240.50:8080': f'127.0.0.1:{guard_port}',
            'jellyfin:8096': f'127.0.0.1:{jf}',
            'seerr:5055': f'127.0.0.1:{seerr}',
            '172.30.240.34:8083': f'127.0.0.1:{relay}',
        }.items():
            config = config.replace(old, new)
        config = config.replace(
            'http {', f'http {{\nclient_body_temp_path {temp}/body;\n'
            f'proxy_temp_path {temp}/proxy;\nfastcgi_temp_path {temp}/fastcgi;\n'
            f'scgi_temp_path {temp}/scgi;\nuwsgi_temp_path {temp}/uwsgi;')
        config_path = temp / 'nginx.conf'
        config_path.write_text(config)
        checked = subprocess.run([*command, '-t', '-p', str(temp), '-c', str(config_path)],
                                 capture_output=True, text=True, timeout=5)
        assert checked.returncode == 0, checked.stderr
        with (temp / 'nginx-process.log').open('w') as output:
            process = subprocess.Popen([*command, '-p', str(temp), '-c', str(config_path),
                                        '-g', 'daemon off;'], stdout=output, stderr=output)
        wait_port(jellyfin_port, process)

        def request(method, path, *, service='jellyfin', headers=None, body=None,
                    source='127.0.0.2', duplicate_headers=()):
            port = jellyfin_port if service == 'jellyfin' else seerr_port
            host = 'private-stream.mywire.org' if service == 'jellyfin' else (
                'private-requests.mywire.org')
            supplied = {'Host': host, 'X-Forwarded-For': '203.0.113.9', **(headers or {})}
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5,
                                                    source_address=(source, 0))
            try:
                if duplicate_headers:
                    connection.putrequest(method, path, skip_host=True)
                    for name, value in [*supplied.items(), *duplicate_headers]:
                        connection.putheader(name, value)
                    connection.endheaders()
                else:
                    connection.request(method, path, body=body, headers=supplied)
                response = connection.getresponse()
                return SimpleNamespace(status=response.status, headers=dict(response.getheaders()),
                                       cookies=response.headers.get_all('Set-Cookie'),
                                       body=response.read())
            finally:
                connection.close()

        def stop_guard():
            guard.should_exit = True
            guard_thread.join(timeout=5)
            assert not guard_thread.is_alive()

        yield SimpleNamespace(request=request, calls=calls, stop_guard=stop_guard,
                              body_events=body_events, login_payloads=login_payloads)
    finally:
        if process:
            process.terminate()
            process.wait(timeout=5)
        guard.should_exit = True
        guard_thread.join(timeout=5)
        os.close(notify)
        for server, thread in zip(servers, threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


@pytest.fixture(scope='module')
def public_proxy(tmp_path_factory):
    with start_public_proxy(tmp_path_factory.mktemp('public-nginx')) as proxy:
        yield proxy


@pytest.mark.parametrize('prefix', ['', '/jellyfin'])
def test_native_web_login_discovery_hides_users_without_upstream(public_proxy, prefix):
    before = len(public_proxy.calls)
    response = public_proxy.request('GET', prefix + '/Users/Public')
    assert response.status == 200
    assert json.loads(response.body) == []
    assert len(public_proxy.calls) == before
    assert public_proxy.request('GET', prefix + '/System/Info/Public').status == 200
    assert public_proxy.request('GET', prefix + '/web/index.html').status == 200


@pytest.mark.parametrize('path', ['/Items', '/Videos/id/stream.mkv', '/socket'])
def test_anonymous_and_admin_sessions_cannot_cross_public_proxy(public_proxy, path):
    assert public_proxy.request('GET', path).status == 401
    response = public_proxy.request('GET', path, headers={'X-Emby-Token': 'admin-token'})
    assert response.status == 403


def test_native_login_body_and_token_are_mediated(public_proxy):
    response = public_proxy.request('POST', '/Jellyfin/uSers/AuthenticateByName/',
        body=json.dumps({'Username': 'viewer', 'Pw': 'correct'}),
        headers={'Content-Type': 'application/json',
                 'X-Original-URI': '/System/Info/Public', 'X-Original-Method': 'GET'})
    assert response.status == 200
    assert json.loads(response.body)['AccessToken'] == 'viewer-token'
    response = public_proxy.request('POST', '/Users/AuthenticateByName',
        body=json.dumps({'Username': 'admin', 'Pw': 'correct'}),
        headers={'Content-Type': 'application/json'})
    assert response.status == 403
    assert b'admin-token' not in response.body


@pytest.mark.parametrize('service', ['jellyfin', 'seerr'])
def test_bounded_login_credentials_never_spill_to_nginx_body_files(public_proxy, service):
    if service == 'jellyfin':
        path = '/Users/AuthenticateByName'
        body = {'Username': 'viewer', 'Pw': 'correct'}
        headers = {'Content-Type': 'application/json'}
    else:
        path = '/api/v1/auth/jellyfin'
        body = {'username': 'viewer', 'password': 'correct'}
        headers = {'Content-Type': 'application/json', 'Cookie': '_csrf=fixture-seed',
                   'X-XSRF-TOKEN': 'fixture-csrf',
                   'Origin': 'https://private-requests.mywire.org:54443'}
    # Valid bounded JSON above Nginx's default 8K body buffer.
    public_proxy.body_events()
    response = public_proxy.request('POST', path, service=service, headers=headers,
                                    body=json.dumps(body) + ' ' * 12000)
    assert response.status == 200
    assert public_proxy.body_events() == b''


def test_spoofed_headers_are_overwritten_and_legacy_token_becomes_modern(public_proxy):
    response = public_proxy.request('GET', '/Items?Fields=MediaStreams', headers={
        'X-Emby-Token': 'viewer-token', 'X-Real-IP': '127.0.0.1',
        'X-Original-Method': 'POST', 'X-Original-URI': '/System/Restart',
        'Forwarded': 'for=127.0.0.1', 'X-Verified-Authorization': 'admin-token',
        'Remote-User': 'admin', 'X-Forwarded-User': 'admin', 'X-API-Key': 'admin',
    })
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload['upstream'] == 'relay'
    headers = payload['headers']
    assert headers['authorization'] == 'MediaBrowser Token="viewer-token"'
    assert headers['x-real-ip'] == headers['x-forwarded-for'] == '203.0.113.9'
    for name in ('forwarded', 'x-verified-authorization', 'x-emby-token', 'x-api-key',
                 'remote-user', 'x-forwarded-user'):
        assert name not in headers


def test_range_and_websocket_handshake_keep_verified_identity(public_proxy):
    response = public_proxy.request('GET', '/Videos/id/stream.mkv?api_key=viewer-token',
        headers={'Range': 'bytes=100-200', 'If-Range': 'fixture-etag'})
    assert response.status == 206
    headers = json.loads(response.body)['headers']
    assert headers['range'] == 'bytes=100-200'
    assert headers['if-range'] == 'fixture-etag'
    assert headers['authorization'] == 'MediaBrowser Token="viewer-token"'
    response = public_proxy.request('GET', '/socket?ApiKey=viewer-token', headers={
        'Upgrade': 'websocket', 'Connection': 'Upgrade', 'Sec-WebSocket-Version': '13',
        'Sec-WebSocket-Key': 'Zml4dHVyZS13ZWJzb2NrZXQ=',
    })
    assert response.status == 101
    assert response.headers['Upgrade'].lower() == 'websocket'


@pytest.mark.parametrize('method,path', [
    ('DELETE', '/Items/id'), ('POST', '/Items/id/Refresh'), ('POST', '/System/Restart'),
    ('POST', '/Users/ForgotPassword'), ('POST', '/QuickConnect/Initiate'),
    ('GET', '/Items/%2e%2e/Users'), ('GET', '/Items%2f..%2fUsers'),
])
def test_mutations_and_encoded_bypasses_never_reach_native(public_proxy, method, path):
    before = len(public_proxy.calls)
    response = public_proxy.request(method, path, headers={'X-Emby-Token': 'viewer-token'})
    assert response.status == 403
    assert len(public_proxy.calls) == before


def test_untrusted_peer_cannot_impersonate_edge(public_proxy):
    response = public_proxy.request('GET', '/System/Info/Public', source='127.0.0.1',
                                   headers={'X-Forwarded-For': '192.168.1.24'})
    assert response.status == 403


def test_unknown_host_and_internal_auth_location_are_not_public(public_proxy):
    with pytest.raises(http.client.RemoteDisconnected):
        public_proxy.request('GET', '/System/Info/Public', headers={'Host': 'unknown.invalid'})
    assert public_proxy.request('GET', '/_authorize').status == 404


def test_seerr_login_csrf_session_and_admin_checks(public_proxy):
    headers = {'Origin': 'https://private-requests.mywire.org:54443',
               'Cookie': '_csrf=fixture-seed; connect.sid=previous',
               'X-XSRF-TOKEN': 'fixture-csrf', 'Content-Type': 'application/json'}
    response = public_proxy.request('POST', '/api/v1/auth/jellyfin', service='seerr',
        headers=headers, body=json.dumps({'username': 'viewer', 'password': 'correct'}))
    assert response.status == 200
    assert any(cookie.startswith('connect.sid=viewer') and 'Secure' in cookie
               for cookie in response.cookies)
    assert any(cookie.startswith('XSRF-TOKEN=') and 'HttpOnly' not in cookie
               for cookie in response.cookies)
    response = public_proxy.request('POST', '/api/v1/auth/jellyfin', service='seerr',
        headers=headers, body=json.dumps({'username': 'admin', 'password': 'correct'}))
    assert response.status == 403
    assert not response.cookies


def test_seerr_native_browser_login_loads_homepage_and_preserves_admin_boundary(public_proxy):
    headers = {'Origin': 'https://private-requests.mywire.org:54443',
               'Cookie': '_csrf=fixture-seed', 'X-XSRF-TOKEN': 'fixture-csrf',
               'Content-Type': 'application/json'}
    response = public_proxy.request('POST', '/api/v1/auth/jellyfin', service='seerr',
        headers=headers, body=json.dumps({
            'username': 'viewer', 'password': 'correct', 'email': 'viewer'}))
    assert response.status == 200
    assert public_proxy.login_payloads[-1] == {'username': 'viewer', 'password': 'correct'}
    cookie = next(value.split(';', 1)[0] for value in response.cookies
                  if value.startswith('connect.sid='))
    authenticated = {'Cookie': cookie, 'Origin': headers['Origin']}
    for path in ('/api/v1/auth/me', '/api/v1/settings/discover',
                 '/api/v1/discover/trending', '/api/v1/request/count',
                 '/api/v1/media?filter=allavailable&take=20&sort=mediaAdded',
                 '/api/v1/service/sonarr/lookup/123', '/api/v1/service/sonarr',
                 '/api/v1/service/sonarr/1', '/api/v1/service/radarr', '/api/v1/service/radarr/1'):
        response = public_proxy.request('GET', path, service='seerr', headers=authenticated)
        assert response.status == 200
        if path.endswith('/settings/discover'):
            assert json.loads(response.body) == [{'id': 1, 'type': 3, 'enabled': True}]
    assert public_proxy.request('HEAD', '/api/v1/settings/discover', service='seerr',
                                headers=authenticated).status == 200
    assert public_proxy.request('GET', '/api/v1/settings/discover',
                                service='seerr').status == 401
    assert public_proxy.request('GET', '/api/v1/settings/discover', service='seerr',
                                headers={'Cookie': 'connect.sid=admin'}).status == 403
    before = len(public_proxy.calls)
    for method, path in [('POST', '/api/v1/settings/discover'),
                         ('GET', '/api/v1/settings/discover/reset'),
                         ('HEAD', '/api/v1/settings/discover/reset'),
                         ('POST', '/api/v1/service/sonarr'),
                         ('POST', '/api/v1/service/sonarr/lookup/123'),
                         ('DELETE', '/api/v1/service/radarr/1'),
                         ('DELETE', '/api/v1/media/1'),
                         ('GET', '/api/v1/overrideRule'), ('POST', '/api/v1/overrideRule'),
                         ('DELETE', '/api/v1/overrideRule/1')]:
        assert public_proxy.request(method, path, service='seerr',
                                    headers=authenticated).status == 403
    assert len(public_proxy.calls) == before


def test_seerr_advanced_evaluation_defers_to_native_permission_and_enforces_origin(public_proxy):
    path = '/api/v1/overrideRule/advancedRequest'
    headers = {'Cookie': 'connect.sid=advanced',
               'Origin': 'https://private-requests.mywire.org:54443',
               'Content-Type': 'application/json'}
    response = public_proxy.request('POST', path, service='seerr', headers=headers,
        body=json.dumps({'mediaType': 'movie', 'tmdbId': 123, 'requestUser': 4, 'is4k': False}))
    assert response.status == 200
    assert json.loads(response.body)['profileId'] == 1
    headers['Cookie'] = 'connect.sid=viewer'
    assert public_proxy.request('POST', path, service='seerr',
                                headers=headers, body='{}').status == 403
    assert public_proxy.calls[-1] == ('POST', path)
    before = len(public_proxy.calls)
    headers['Origin'] = 'https://evil.invalid'
    assert public_proxy.request('POST', path, service='seerr',
                                headers=headers, body='{}').status == 403
    assert len(public_proxy.calls) == before


def test_seerr_request_cookie_cannot_bypass_origin_or_admin_policy(public_proxy):
    headers = {'Origin': 'https://private-requests.mywire.org:54443',
               'Cookie': 'connect.sid=viewer', 'Content-Type': 'application/json'}
    response = public_proxy.request('POST', '/api/v1/request', service='seerr',
                                   headers=headers, body='{}')
    assert response.status == 200
    headers['Origin'] = 'https://evil.invalid'
    assert public_proxy.request('POST', '/api/v1/request', service='seerr',
                                headers=headers, body='{}').status == 403
    assert public_proxy.request('GET', '/api/v1/request', service='seerr',
                                headers={'Cookie': 'connect.sid=admin'}).status == 403


@pytest.mark.parametrize('decision,status', [('approve', 2), ('decline', 3)])
def test_seerr_moderation_defers_to_native_permission_and_preserves_public_boundary(
        public_proxy, decision, status):
    path = '/api/v1/request/42/' + decision
    origin = 'https://private-requests.mywire.org:54443'
    moderator = {'Cookie': 'connect.sid=moderator', 'Origin': origin}
    response = public_proxy.request('POST', path, service='seerr', headers=moderator)
    assert response.status == 200
    assert json.loads(response.body) == {'id': 42, 'status': status}
    response = public_proxy.request('POST', path, service='seerr',
        headers={'Cookie': 'connect.sid=viewer', 'Origin': origin})
    assert response.status == 403
    assert json.loads(response.body) == {'error': 'Native MANAGE_REQUESTS permission required'}
    assert public_proxy.calls[-1] == ('POST', path)
    for headers in ({'Cookie': 'connect.sid=admin', 'Origin': origin},
                    {'Cookie': 'connect.sid=moderator', 'Origin': 'https://evil.example'},
                    {'Cookie': 'connect.sid=moderator'}, {'Origin': origin}):
        before = public_proxy.calls.count(('POST', path))
        response = public_proxy.request('POST', path, service='seerr', headers=headers)
        assert response.status in {401, 403}
        assert public_proxy.calls.count(('POST', path)) == before
    assert public_proxy.request('GET', '/api/v1/request?filter=pending', service='seerr',
                                headers=moderator).status == 200
    for method, route in [('DELETE', '/api/v1/request/42'), ('PUT', '/api/v1/request/42'),
                          ('POST', '/api/v1/request/42/retry'),
                          ('POST', '/api/v1/user/4/settings/permissions')]:
        before = public_proxy.calls.count((method, route))
        assert public_proxy.request(method, route, service='seerr', headers=moderator).status == 403
        assert public_proxy.calls.count((method, route)) == before


def test_guard_unavailable_never_forwards_anonymous_or_authenticated_catalog(tmp_path):
    with start_public_proxy(tmp_path) as proxy:
        proxy.stop_guard()
        before = len(proxy.calls)
        for headers in ({}, {'X-Emby-Token': 'viewer-token'}):
            response = proxy.request('GET', '/Items', headers=headers)
            assert response.status >= 500
        assert len(proxy.calls) == before


def test_duplicate_credential_headers_do_not_select_an_identity(public_proxy):
    before = len(public_proxy.calls)
    response = public_proxy.request('GET', '/Items', duplicate_headers=[
        ('Authorization', 'MediaBrowser Token="viewer-token"'),
        ('Authorization', 'MediaBrowser Token="admin-token"'),
    ])
    assert response.status in {400, 403}
    assert len(public_proxy.calls) == before
    response = public_proxy.request('GET', '/api/v1/request', service='seerr', duplicate_headers=[
        ('Cookie', 'connect.sid=viewer'), ('Cookie', 'connect.sid=admin'),
    ])
    assert response.status in {400, 401, 403}
    assert len(public_proxy.calls) == before


def test_login_variants_share_limits_and_do_not_limit_playback(public_proxy):
    headers = {'X-Forwarded-For': '203.0.113.99', 'Content-Type': 'application/json'}
    for path in ('/Users/AuthenticateByName', '/users/authenticatebyname',
                 '/jellyfin/Users/AuthenticateByName', '/Users/%41uthenticateByName',
                 '/jellyfin/users/authenticatebyname/'):
        response = public_proxy.request('POST', path, headers=headers,
                                       body='{"Username":"limited","Pw":"wrong"}')
        assert response.status == 401
    response = public_proxy.request('POST', '/Users/AuthenticateByName', headers=headers,
                                   body='{"Username":"other","Pw":"correct"}')
    assert response.status == 429
    assert int(response.headers['retry-after']) > 0
    headers['X-Emby-Token'] = 'viewer-token'
    assert public_proxy.request('GET', '/Items', headers=headers).status == 200


@contextmanager
def start_private_proxy(temp, service):
    with nginx_command(temp) as command:
        pass

    class Echo(BaseHTTPRequestHandler):
        def respond(self):
            data = json.dumps({'path': self.path, 'method': self.command,
                               'headers': dict(self.headers)}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = respond
        do_POST = respond
        do_DELETE = respond

        def log_message(self, *_args):
            pass

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Echo)
    thread = threading.Thread(target=upstream.serve_forever, kwargs={'poll_interval': 0.05},
                              daemon=True)
    thread.start()
    port = unused_port()
    target = 8096 if service == 'jellyfin' else 5055
    config = Path(f'deploy/{service}-proxy/nginx.conf').read_text()
    for old, new in {
        'worker_processes 2;': 'worker_processes 1;',
        'pid /tmp/nginx.pid;': f'pid {temp}/nginx.pid;',
        f'listen {target};': f'listen 127.0.0.1:{port};',
        '172.30.240.1:https': '127.0.0.3:https',
        '172.30.240.17:https': '127.0.0.3:https',
        'jellyfin:8096': f'127.0.0.1:{upstream.server_port}',
        'seerr:5055': f'127.0.0.1:{upstream.server_port}',
        'jellyfin-relay:8083': f'127.0.0.1:{upstream.server_port}',
        'control-api:8080': f'127.0.0.1:{upstream.server_port}',
    }.items():
        config = config.replace(old, new)
    config = config.replace('http {', f'http {{\nclient_body_temp_path {temp}/body;\n'
                            f'proxy_temp_path {temp}/proxy;\nfastcgi_temp_path {temp}/fastcgi;\n'
                            f'scgi_temp_path {temp}/scgi;\nuwsgi_temp_path {temp}/uwsgi;')
    config_path = temp / 'nginx.conf'
    config_path.write_text(config)
    process = None
    try:
        checked = subprocess.run([*command, '-t', '-p', str(temp), '-c', str(config_path)],
                                 capture_output=True, text=True, timeout=5)
        assert checked.returncode == 0, checked.stderr
        with (temp / 'nginx.log').open('w') as output:
            process = subprocess.Popen([*command, '-p', str(temp), '-c', str(config_path),
                                        '-g', 'daemon off;'], stdout=output, stderr=output)
        wait_port(port, process)

        def request(source, scheme='https', path='/private-test', host='homeserver.example.ts.net'):
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5,
                                                    source_address=(source, 0))
            try:
                connection.request('GET', path, headers={
                    'Host': host, 'X-Forwarded-Proto': scheme,
                    'X-Forwarded-For': '203.0.113.3', 'X-Real-IP': '203.0.113.4',
                    'Forwarded': 'for=203.0.113.5',
                    'Authorization': 'MediaBrowser Token="private-admin"',
                })
                response = connection.getresponse()
                return SimpleNamespace(status=response.status,
                    headers=dict(response.getheaders()), body=response.read())
            finally:
                connection.close()
        yield request
    finally:
        if process:
            process.terminate()
            process.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('service', ['jellyfin', 'seerr'])
def test_private_proxy_trusts_only_explicit_https_peers_and_keeps_admin(tmp_path, service):
    with start_private_proxy(tmp_path, service) as request:
        for source, scheme in [('127.0.0.1', 'https'), ('127.0.0.2', 'http'),
                               ('127.0.0.3', 'https')]:
            response = request(source)
            if service == 'seerr' and scheme == 'http':
                assert response.status == 308
                assert response.headers['Location'] == (
                    'https://homelab.coin-capella.ts.net/private-test')
                continue
            assert response.status == 200
            payload = json.loads(response.body)
            headers = {key.lower(): value for key, value in payload['headers'].items()}
            assert headers['x-forwarded-for'] == headers['x-real-ip'] == source
            assert headers['x-forwarded-proto'] == scheme
            assert headers['authorization'] == 'MediaBrowser Token="private-admin"'
            assert 'forwarded' not in headers


def test_private_seerr_plain_http_redirects_to_fixed_https_origin(tmp_path):
    with start_private_proxy(tmp_path, 'seerr') as request:
        response = request('127.0.0.1', scheme='http', path='/login?next=%2F',
                           host='attacker.example')
        assert response.status == 308
        assert response.headers['Location'] == 'https://homelab.coin-capella.ts.net/login?next=%2F'


def test_jellyfin_browser_image_cookie_is_propagated_without_exposing_catalog(tmp_path):
    from http.cookies import SimpleCookie

    with start_public_proxy(tmp_path) as proxy:
        login = proxy.request('POST', '/jellyfin/Users/AuthenticateByName',
            body=json.dumps({'Username': 'viewer', 'Pw': 'correct'}),
            headers={'Content-Type': 'application/json'})
        assert login.status == 200
        cookie = SimpleCookie()
        for value in login.cookies or []:
            cookie.load(value)
        image_cookie = cookie['__Host-hs-jf-image']
        assert image_cookie.value == 'viewer-token'
        assert image_cookie['secure'] and image_cookie['httponly']
        assert image_cookie['path'] == '/' and image_cookie['samesite'] == 'strict'
        assert image_cookie['max-age'] == '1800' and not image_cookie['domain']
        # An already authenticated client also receives the cookie on its next API read.
        bootstrap = proxy.request('GET', '/jellyfin/Items',
                                  headers={'X-Emby-Token': 'viewer-token'})
        assert bootstrap.status == 200
        assert any('__Host-hs-jf-image=viewer-token' in value for value in bootstrap.cookies or [])
        path = '/jellyfin/Items/' + '1' * 32 + '/Images/Primary?quality=90&tag=fixture'
        assert proxy.request('GET', path).status == 401
        image = proxy.request('GET', path, headers={'Cookie': '__Host-hs-jf-image=viewer-token'})
        assert image.status == 200
        forwarded = json.loads(image.body)['headers']
        assert 'Token="viewer-token"' in forwarded['authorization']
        assert 'cookie' not in forwarded
        assert image.headers['Cache-Control'] == 'private, no-store'
        assert image.headers['X-Content-Type-Options'] == 'nosniff'
        assert 'noindex' in image.headers['X-Robots-Tag']
        for route, status in (('/jellyfin/Items', 401), ('/jellyfin/Videos/id/stream.mkv', 401),
                              ('/jellyfin/System/Info', 403)):
            assert proxy.request('GET', route,
                headers={'Cookie': '__Host-hs-jf-image=viewer-token'}).status == status
        for token, expected in (('revoked', 401), ('admin-token', 403)):
            assert proxy.request('GET', path,
                headers={'Cookie': '__Host-hs-jf-image=' + token}).status == expected
        logout = proxy.request('POST', '/jellyfin/Sessions/Logout',
                               headers={'X-Emby-Token': 'viewer-token'})
        assert logout.status == 200
        assert any('__Host-hs-jf-image=' in value and 'Max-Age=0' in value
                   for value in logout.cookies or [])
