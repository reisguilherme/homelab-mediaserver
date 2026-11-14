from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest


@asynccontextmanager
async def fixture(tmp_path, handler, *, peer='172.30.0.8', clock=None):
    from homeserver_control.api.public_access import PublicAccessSettings, create_app

    settings = PublicAccessSettings(
        jellyfin_url='http://jellyfin:8096/jellyfin', seerr_url='http://seerr:5055',
        trusted_proxy_ips=('172.30.0.8',), state_path=tmp_path / 'limits.sqlite3',
        seerr_origin='https://requests.example:54443',
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
        app = create_app(settings, client=upstream, clock=clock or (lambda: 1000.0))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(peer, 12345)),
            base_url='http://guard',
        ) as browser:
            yield browser


def forwarded(path='/Items', method='GET', ip='203.0.113.9', **extra):
    return {'X-Original-URI': path, 'X-Original-Method': method,
            'X-Real-IP': ip, **extra}


def viewer(request):
    assert str(request.url) == 'http://jellyfin:8096/jellyfin/Users/Me'
    assert 'Token="viewer-token"' in request.headers['authorization']
    return httpx.Response(200, json={'Id': 'viewer', 'Policy': {
        'IsAdministrator': False, 'IsDisabled': False}})


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [
    '/jellyfin/UserViews?userId=viewer',
    '/jellyfin/UserViews/GroupingOptions?userId=viewer',
    '/jellyfin/Users/viewer/Views',
    '/jellyfin/Users/viewer/GroupingOptions',
])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
async def test_user_views_modern_and_legacy_reads_keep_native_viewer_identity(
        tmp_path, path, method):
    async with fixture(tmp_path, viewer) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            path, method, Authorization='MediaBrowser Token="viewer-token"'))
    assert result.status_code == 204
    assert result.headers['x-verified-authorization'] == 'MediaBrowser Token="viewer-token"'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/jellyfin/UserViews', '/jellyfin/UserViews/GroupingOptions'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('identity,status', [('absent', 401), ('administrator', 403)])
async def test_user_views_modern_reads_require_ordinary_native_identity(tmp_path, path, method,
                                                                     identity, status):
    def native(_request):
        assert identity == 'administrator', 'Anonymous requests must not reach Jellyfin'
        return httpx.Response(200, json={'Id': 'admin', 'Policy': {
            'IsAdministrator': True, 'IsDisabled': False}})
    authorization = {'Authorization': 'MediaBrowser Token="admin-token"'} if (
        identity == 'administrator') else {}
    async with fixture(tmp_path, native) as client:
        result = await client.get('/authorize/jellyfin',
                                  headers=forwarded(path, method, **authorization))
    assert result.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/jellyfin/UserViews', '/jellyfin/UserViews/GroupingOptions'])
@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
async def test_user_views_modern_mutations_remain_denied(tmp_path, path, method):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            path, method, Authorization='MediaBrowser Token="viewer-token"'))
    assert result.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/jellyfin/UserViews/Anything',
                                  '/jellyfin/UserViews/GroupingOptions/Anything'])
async def test_user_views_modern_prefix_does_not_allow_other_routes(tmp_path, path):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            path, Authorization='MediaBrowser Token="viewer-token"'))
    assert result.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/Items', '/Videos/id/stream.m3u8?api_key=viewer-token',
                                  '/jellyfin/socket?api_key=viewer-token'])
async def test_native_viewer_catalog_stream_and_socket_authorized(tmp_path, path):
    async with fixture(tmp_path, viewer) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            path, Authorization='MediaBrowser Token="viewer-token"'))
    assert result.status_code == 204
    assert result.headers['x-verified-authorization'] == 'MediaBrowser Token="viewer-token"'


@pytest.mark.asyncio
@pytest.mark.parametrize('path,method', [
    ('/Items/id', 'DELETE'), ('/jellyfin/Users/New', 'POST'),
    ('/System/Restart', 'POST'), ('/Users', 'GET'), ('/Users/Public', 'GET'),
    ('/Auth/Keys', 'GET'), ('/Plugins', 'GET'), ('/Items/id/Refresh', 'POST'),
    ('/QuickConnect/Initiate', 'POST'), ('/Users/ForgotPassword', 'POST'),
    ('/Items/../Users', 'GET'), ('/Items/%2e%2e/Users', 'GET'),
    ('/Items%2f..%2fUsers', 'GET'), ('//Items', 'GET'), ('http://evil/Items', 'GET'),
    ('/Items\\..\\Users', 'GET'), ('/Items;admin', 'GET'),
])
async def test_public_mutation_admin_and_path_bypasses_denied(tmp_path, path, method):
    def never(_request):
        pytest.fail('Forbidden path must not reach native service')
    async with fixture(tmp_path, never) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            path, method, Authorization='MediaBrowser Token="viewer-token"'))
    assert result.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('policy', [
    {'IsAdministrator': True, 'IsDisabled': False},
    {'IsAdministrator': False, 'IsDisabled': True}, {},
])
async def test_admin_disabled_and_malformed_user_denied_even_for_websocket(tmp_path, policy):
    async with fixture(tmp_path, lambda _: httpx.Response(
            200, json={'Id': 'user', 'Policy': policy})) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            '/socket?api_key=viewer-token'))
    assert result.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('path,headers', [
    ('/Items?api_key=one&api_key=one', {}),
    ('/Items?api_key=one', {'X-Emby-Token': 'two'}),
    ('/Items', {'Authorization': 'Bearer token'}),
    ('/Items', {'Authorization': 'MediaBrowser Token="one", Token="two"'}),
    ('/Items?apikey=service-key', {}),
])
async def test_ambiguous_or_unsupported_credentials_fail_closed(tmp_path, path, headers):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(path, **headers))
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_api_key_without_native_user_is_not_public_identity(tmp_path):
    async with fixture(tmp_path, lambda _: httpx.Response(200, json={})) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            '/Items', **{'X-Emby-Token': 'service-key'}))
    assert result.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('path,status', [('/System/Info/Public', 204),
    ('/web/index.html', 204), ('/web/main.abcd.js', 204), ('/Items', 401),
    ('/Items/id/Images/Primary', 401), ('/socket', 401), ('/web/../Users', 403)])
async def test_only_explicit_discovery_and_assets_are_anonymous(tmp_path, path, status):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(path))
    assert result.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize('peer,ip', [('198.51.100.9', '203.0.113.9'),
                                    ('172.30.0.8', '1.2.3.4, 127.0.0.1'),
                                    ('172.30.0.8', 'invalid')])
async def test_real_peer_and_single_client_address_required(tmp_path, peer, ip):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call'), peer=peer) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            '/System/Info/Public', ip=ip))
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_duplicate_forwarded_identity_rejected(tmp_path):
    headers = list(forwarded('/System/Info/Public').items()) + [('X-Real-IP', '127.0.0.1')]
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.get('/authorize/jellyfin', headers=headers)
    assert result.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('permissions', [2, 4, 8, 1048576, 268435456])
async def test_seerr_privileged_cookie_cannot_request_publicly(tmp_path, permissions):
    def native(request):
        assert str(request.url) == 'http://seerr:5055/api/v1/auth/me'
        assert request.headers['cookie'] == 'connect.sid=admin'
        return httpx.Response(200, json={'id': 4, 'permissions': permissions})
    async with fixture(tmp_path, native) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            '/api/v1/request', Cookie='connect.sid=admin'))
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_seerr_regular_cookie_and_request_permissions_preserved(tmp_path):
    async with fixture(tmp_path, lambda _: httpx.Response(
            200, json={'id': 4, 'permissions': 32 | 128})) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            '/api/v1/request', 'POST', Cookie='connect.sid=viewer',
            Origin='https://requests.example:54443'))
    assert result.status_code == 204


@pytest.mark.asyncio
@pytest.mark.parametrize('path,method', [
    ('/api/v1/auth/me', 'GET'), ('/api/v1/request?filter=pending', 'GET'),
    ('/api/v1/request', 'POST'), ('/api/v1/request/1/approve', 'POST'),
    ('/api/v1/request/987654/decline', 'POST'),
])
async def test_seerr_request_moderator_keeps_session_and_exact_native_actions(
        tmp_path, path, method):
    async with fixture(tmp_path, lambda _: httpx.Response(
            200, json={'id': 4, 'permissions': 32 | 16})) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            path, method, Cookie='connect.sid=moderator', Origin='https://requests.example:54443'))
    assert result.status_code == 204


@pytest.mark.asyncio
async def test_seerr_request_moderator_login_preserves_native_cookie(tmp_path):
    def native(request):
        if request.url.path == '/api/v1/auth/jellyfin':
            return httpx.Response(200, json={'id': 4, 'permissions': 48},
                                  headers={'Set-Cookie': 'connect.sid=moderator; Path=/; HttpOnly'})
        assert request.url.path == '/api/v1/auth/me'
        assert request.headers['cookie'] == 'connect.sid=moderator'
        return httpx.Response(200, json={'id': 4, 'permissions': 48})
    async with fixture(tmp_path, native) as client:
        result = await client.post('/login/seerr', headers=forwarded(
            '/api/v1/auth/jellyfin', 'POST', Origin='https://requests.example:54443'),
            json={'username': 'moderator', 'password': 'fixture-password'})
    assert result.status_code == 200
    assert 'connect.sid=moderator' in result.headers['set-cookie']


@pytest.mark.asyncio
@pytest.mark.parametrize('user_id,permissions', [(1, 48), (4, 48 | 2), (4, 48 | 4),
    (4, 48 | 8), (4, 48 | 1048576), (4, 48 | 268435456)])
async def test_seerr_moderation_never_admits_owner_or_other_privileged_bits(
        tmp_path, user_id, permissions):
    async with fixture(tmp_path, lambda _: httpx.Response(
            200, json={'id': user_id, 'permissions': permissions})) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            '/api/v1/request/42/approve', 'POST', Cookie='connect.sid=moderator',
            Origin='https://requests.example:54443'))
    assert result.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [
    ('GET', '/api/v1/request/42/approve'), ('HEAD', '/api/v1/request/42/decline'),
    ('PUT', '/api/v1/request/42/approve'), ('PATCH', '/api/v1/request/42/approve'),
    ('DELETE', '/api/v1/request/42/decline'), ('OPTIONS', '/api/v1/request/42/approve'),
    ('POST', '/api/v1/request/0/approve'), ('POST', '/api/v1/request/-1/approve'),
    ('POST', '/api/v1/request/1.0/approve'), ('POST', '/api/v1/request/+1/approve'),
    ('POST', '/api/v1/request/1e3/approve'), ('POST', '/api/v1/request/01/approve'),
    ('POST', '/api/v1/request/%D9%A1/approve'), ('POST', '/api/v1/request/42/approve/extra'),
    ('POST', '/api/v1/request/42/retry'), ('DELETE', '/api/v1/request/42'),
    ('PUT', '/api/v1/request/42'), ('POST', '/api/v1/request/42/approved'),
    ('POST', '/api/v1/user/4/settings/permissions'), ('POST', '/api/v1/settings/main'),
])
async def test_seerr_moderation_exposes_no_other_method_id_or_management_route(
        tmp_path, method, path):
    def no_identity(_request):
        pytest.fail('Denied route must not query identity')
    async with fixture(tmp_path, no_identity) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            path, method, Cookie='connect.sid=moderator', Origin='https://requests.example:54443'))
    assert result.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('cookie,origin,status', [
    ('', 'https://requests.example:54443', 401), ('connect.sid=moderator', '', 403),
    ('connect.sid=moderator', 'https://evil.example', 403),
])
async def test_seerr_moderation_requires_native_cookie_and_exact_origin(
        tmp_path, cookie, origin, status):
    async with fixture(tmp_path, lambda _: pytest.fail('Missing identity or Origin')) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            '/api/v1/request/42/approve', 'POST', Cookie=cookie, Origin=origin))
    assert result.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize('origin', ['', 'https://evil.example'])
async def test_seerr_csrf_checks_origin_before_cookie(tmp_path, origin):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            '/api/v1/request', 'POST', Cookie='connect.sid=viewer', Origin=origin))
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_login_keeps_native_response_only_after_token_user_verification(tmp_path):
    calls = []
    def native(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/Users/AuthenticateByName'):
            assert json.loads(request.content) == {'Username': 'viewer', 'Pw': 'password'}
            assert request.headers['x-forwarded-for'] == '203.0.113.9'
            return httpx.Response(200, json={
                'AccessToken': 'viewer-token', 'User': {'Id': 'viewer'}})
        return viewer(request)
    async with fixture(tmp_path, native) as client:
        result = await client.post('/login/jellyfin', headers=forwarded(
            '/jellyfin/Users/AuthenticateByName', 'POST'),
            json={'Username': 'viewer', 'Pw': 'password'})
    assert result.status_code == 200
    assert result.json()['AccessToken'] == 'viewer-token'
    assert calls == ['/jellyfin/Users/AuthenticateByName', '/jellyfin/Users/Me']


@pytest.mark.asyncio
async def test_login_admin_token_and_cookie_never_return_to_client(tmp_path):
    def native(request):
        if request.method == 'POST':
            return httpx.Response(200, json={'AccessToken': 'admin-token'},
                                  headers={'set-cookie': 'secret=value'})
        return httpx.Response(200, json={'Id': 'admin', 'Policy': {
            'IsAdministrator': True, 'IsDisabled': False}})
    async with fixture(tmp_path, native) as client:
        result = await client.post('/login/jellyfin', headers=forwarded(
            '/Users/AuthenticateByName', 'POST'), json={'Username': 'admin', 'Pw': 'password'})
    assert result.status_code == 403
    assert 'set-cookie' not in result.headers
    assert 'admin-token' not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize('permissions,status', [(32, 200), (2, 403)])
async def test_seerr_login_validates_new_cookie_not_previous_cookie(tmp_path, permissions, status):
    def native(request):
        if request.method == 'POST':
            assert request.url.path == '/api/v1/auth/jellyfin'
            assert 'cookie' not in request.headers
            return httpx.Response(200, json={'id': 4, 'permissions': 32}, headers={
                'set-cookie': 'connect.sid=new; Path=/; HttpOnly; SameSite=Lax'})
        assert request.headers['cookie'] == 'connect.sid=new'
        return httpx.Response(200, json={'id': 4, 'permissions': permissions})
    async with fixture(tmp_path, native) as client:
        result = await client.post('/login/seerr', headers=forwarded(
            '/api/v1/auth/jellyfin', 'POST', Cookie='connect.sid=previous',
            Origin='https://requests.example:54443'), json={'username': 'viewer', 'password': 'pw'})
    assert result.status_code == status
    if status == 200:
        assert 'Secure' in result.headers['set-cookie']
    else:
        assert 'set-cookie' not in result.headers


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('hostname', '127.0.0.1'), ('port', 8096),
    ('urlBase', '/jellyfin'), ('useSsl', True), ('serverType', 2)])
@pytest.mark.parametrize('email', [False, True])
async def test_seerr_setup_parameters_never_reach_backend(tmp_path, field, value, email):
    payload = {'username': 'u', 'password': 'p', field: value}
    if email:
        payload['email'] = 'u'
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.post('/login/seerr', headers=forwarded(
            '/api/v1/auth/jellyfin', 'POST', Origin='https://requests.example:54443'), json=payload)
    assert result.status_code == 400


@pytest.mark.asyncio
async def test_seerr_browser_username_email_is_accepted_and_not_forwarded(tmp_path):
    def native(request):
        if request.method == 'POST':
            assert request.url.path == '/api/v1/auth/jellyfin'
            assert json.loads(request.content) == {'username': 'viewer', 'password': 'pw'}
            return httpx.Response(200, json={'id': 4}, headers={
                'set-cookie': 'connect.sid=new; Path=/; HttpOnly'})
        assert request.headers['cookie'] == 'connect.sid=new'
        return httpx.Response(200, json={'id': 4, 'permissions': 32})
    async with fixture(tmp_path, native) as client:
        result = await client.post('/login/seerr', headers=forwarded(
            '/api/v1/auth/jellyfin', 'POST', Origin='https://requests.example:54443'),
            json={'username': 'viewer', 'password': 'pw', 'email': 'viewer'})
    assert result.status_code == 200
    assert 'connect.sid=new' in result.headers['set-cookie']


@pytest.mark.asyncio
@pytest.mark.parametrize('email', [None, True, [], {}, '', 'different', 'x' * 257])
async def test_seerr_optional_email_cannot_introduce_another_identity(tmp_path, email):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.post('/login/seerr', headers=forwarded(
            '/api/v1/auth/jellyfin', 'POST', Origin='https://requests.example:54443'),
            json={'username': 'viewer', 'password': 'pw', 'email': email})
    assert result.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('path', ['/api/v1/settings/discover',
    '/api/v1/media?filter=allavailable&take=20&sort=mediaAdded',
    '/api/v1/service/sonarr/lookup/123', '/api/v1/service/sonarr',
    '/api/v1/service/sonarr/1', '/api/v1/service/radarr', '/api/v1/service/radarr/1'])
async def test_seerr_discover_configuration_is_an_authenticated_read(tmp_path, method, path):
    async with fixture(tmp_path, lambda _: httpx.Response(
            200, json={'id': 4, 'permissions': 32})) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            path, method, Cookie='connect.sid=viewer'))
        anonymous = await client.get('/authorize/seerr', headers=forwarded(
            path, method))
    assert result.status_code == 204
    assert anonymous.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize('path,method', [('/api/v1/settings/discover', 'POST'),
    ('/api/v1/settings/discover/reset', 'GET'), ('/api/v1/settings/discover/reset', 'HEAD'),
    ('/api/v1/settings/main', 'GET'), ('/api/v1/media/1', 'DELETE'),
    ('/api/v1/media/1', 'GET'), ('/api/v1/media', 'POST'),
    ('/api/v1/service/sonarr', 'POST'), ('/api/v1/service/sonarr/lookup/123', 'POST'),
    ('/api/v1/service/sonarr/lookup/123/delete', 'GET'),
    ('/api/v1/service/sonarr/lookup/not-numeric', 'GET'), ('/api/v1/service/radarr', 'PUT'),
    ('/api/v1/service/radarr/1', 'DELETE'), ('/api/v1/service/radarr/not-numeric', 'GET'),
    ('/api/v1/overrideRule', 'GET'), ('/api/v1/overrideRule', 'POST'),
    ('/api/v1/overrideRule/1', 'DELETE'), ('/api/v1/overrideRule/advancedRequest', 'GET'),
    ('/api/v1/overrideRule/advancedRequest/reset', 'POST')])
async def test_seerr_discover_admin_operations_stay_blocked(tmp_path, path, method):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            path, method, Cookie='connect.sid=viewer', Origin='https://requests.example:54443'))
    assert result.status_code == 403


@pytest.mark.asyncio
async def test_seerr_advanced_request_evaluation_preserves_native_permission(tmp_path):
    async with fixture(tmp_path, lambda _: httpx.Response(
            200, json={'id': 4, 'permissions': 32 | 8192})) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            '/api/v1/overrideRule/advancedRequest', 'POST', Cookie='connect.sid=advanced',
            Origin='https://requests.example:54443'))
    assert result.status_code == 204


@pytest.mark.asyncio
async def test_login_body_bounded_before_parsing(tmp_path):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.post('/login/jellyfin', headers=forwarded(
            '/Users/AuthenticateByName', 'POST'), content=b'x' * 20000)
    assert result.status_code == 413


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [302, 500])
async def test_upstream_redirect_or_failure_never_counts_as_wrong_password(tmp_path, status):
    async with fixture(tmp_path, lambda _: httpx.Response(
            status, headers={'location': 'http://internal/'}, text='backend error')) as client:
        for _ in range(8):
            result = await client.post('/login/jellyfin', headers=forwarded(
                '/Users/AuthenticateByName', 'POST'), json={'Username': 'u', 'Pw': 'pw'})
            assert result.status_code == 503


@pytest.mark.asyncio
async def test_ip_failure_lockout_expires_and_progresses_across_restart(tmp_path):
    now = [1000.0]
    async def attempt(client, account='viewer', ip='203.0.113.9'):
        return await client.post('/login/jellyfin', headers=forwarded(
            '/Users/AuthenticateByName', 'POST', ip), json={'Username': account, 'Pw': 'bad'})
    async with fixture(tmp_path, lambda _: httpx.Response(401, text='Invalid credentials'),
                       clock=lambda: now[0]) as client:
        for index in range(5):
            assert (await attempt(client, f'user{index}')).status_code == 401
        blocked = await attempt(client, 'another')
        assert blocked.status_code == 429
        assert int(blocked.headers['retry-after']) == 900
    now[0] += 901
    async with fixture(tmp_path, lambda _: httpx.Response(401), clock=lambda: now[0]) as client:
        for index in range(5):
            assert (await attempt(client, f'other{index}')).status_code == 401
        assert int((await attempt(client)).headers['retry-after']) == 3600


@pytest.mark.asyncio
async def test_normalized_account_rate_limit_blocks_distributed_origins(tmp_path):
    async with fixture(tmp_path, lambda _: httpx.Response(401)) as client:
        for index in range(5):
            result = await client.post('/login/jellyfin', headers=forwarded(
                '/Users/AuthenticateByName', 'POST', f'203.0.113.{index + 1}'),
                json={'Username': ' Viewer ' if index % 2 else 'viewer', 'Pw': 'bad'})
            assert result.status_code == 401
        result = await client.post('/login/jellyfin', headers=forwarded(
            '/Users/AuthenticateByName', 'POST', '198.51.100.9'),
            json={'Username': 'VIEWER', 'Pw': 'bad'})
    assert result.status_code == 429
    assert b'viewer' not in (tmp_path / 'limits.sqlite3').read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [
    '/Videos/id/master.m3u8?ApiKey=viewer-token', '/Videos/id/live.m3u8',
    '/Videos/id/source/Subtitles/1/Stream.vtt',
    '/Videos/id/source/Subtitles/1/0/Stream.srt',
    '/Videos/id/source/Subtitles/1/subtitles.m3u8', '/FallbackFont/Fonts/font.ttf',
])
async def test_current_native_stream_subtitle_and_font_routes(tmp_path, path):
    async with fixture(tmp_path, viewer) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            path, Authorization='MediaBrowser Token="viewer-token"'))
    assert result.status_code == 204


@pytest.mark.asyncio
async def test_seerr_anonymous_csrf_cookie_allows_login_screen(tmp_path):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            '/login', Cookie='_csrf=seed; XSRF-TOKEN=token'))
    assert result.status_code == 204


@pytest.mark.asyncio
async def test_seerr_native_csrf_login_preserves_csrf_but_never_prior_identity(tmp_path):
    def native(request):
        if request.method == 'POST':
            assert request.headers['cookie'] == '_csrf=seed'
            assert request.headers['x-xsrf-token'] == 'csrf-token'
            return httpx.Response(200, json={'id': 4, 'permissions': 32}, headers=[
                ('set-cookie', 'connect.sid=new; Path=/; HttpOnly; SameSite=Lax'),
                ('set-cookie', 'XSRF-TOKEN=newcsrf; Path=/; SameSite=Strict'),
            ])
        return httpx.Response(200, json={'id': 4, 'permissions': 32})
    async with fixture(tmp_path, native) as client:
        result = await client.post('/login/seerr', headers=forwarded(
            '/api/v1/auth/local', 'POST', Cookie='_csrf=seed; connect.sid=old',
            Origin='https://requests.example:54443', **{'X-XSRF-TOKEN': 'csrf-token'}),
            json={'email': 'viewer@example.invalid', 'password': 'pw'})
    assert result.status_code == 200
    assert all('Secure' in cookie for cookie in result.headers.get_list('set-cookie'))
    assert 'HttpOnly' not in next(cookie for cookie in result.headers.get_list('set-cookie')
                                  if cookie.startswith('XSRF-TOKEN='))


@pytest.mark.asyncio
async def test_duplicate_json_identity_rejected_before_native_login(tmp_path):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        result = await client.post('/login/jellyfin', headers=forwarded(
            '/Users/AuthenticateByName', 'POST'),
            content=b'{"Username":"one","Username":"two","Pw":"bad"}')
    assert result.status_code == 400


@pytest.mark.asyncio
async def test_upstream_response_size_limit(tmp_path):
    async with fixture(tmp_path, lambda _: httpx.Response(200, content=b'x' * 300000)) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            '/Items', **{'X-Emby-Token': 'viewer-token'}))
    assert result.status_code == 503


@pytest.mark.asyncio
async def test_forwarded_spoof_cannot_change_ipv6_login_origin(tmp_path):
    def native(request):
        assert request.headers['x-forwarded-for'] == '2001:db8::123'
        assert 'forwarded' not in request.headers
        return httpx.Response(401)
    async with fixture(tmp_path, native) as client:
        result = await client.post('/login/jellyfin', headers=forwarded(
            '/Users/AuthenticateByName', 'POST', '2001:db8::123',
            **{'Forwarded': 'for=127.0.0.1', 'X-Forwarded-For': '127.0.0.1'}),
            json={'Username': 'viewer', 'Pw': 'bad'})
    assert result.status_code == 401


def test_inflight_second_failure_does_not_clear_an_active_ip_block(tmp_path):
    from homeserver_control.api.public_limits import LoginLimited, LoginLimits
    limits = LoginLimits(tmp_path / 'state.sqlite')
    for index in range(4):
        lease = limits.reserve('jellyfin', '203.0.113.9', str(index), 1000)
        limits.finish(lease, True, 1000)
    one = limits.reserve('jellyfin', '203.0.113.9', 'one', 1001)
    two = limits.reserve('jellyfin', '203.0.113.9', 'two', 1001)
    limits.finish(one, True, 1001)
    limits.finish(two, True, 1001)
    with pytest.raises(LoginLimited) as blocked:
        limits.reserve('jellyfin', '203.0.113.9', 'three', 1001)
    assert blocked.value.retry_after == 900


def test_lockout_recurrence_resets_after_a_day_without_failures(tmp_path):
    from homeserver_control.api.public_limits import LoginLimited, LoginLimits
    limits = LoginLimits(tmp_path / 'state.sqlite')
    for now in (1000, 90000):
        for index in range(5):
            lease = limits.reserve('jellyfin', '203.0.113.9', str(index), now)
            limits.finish(lease, True, now)
        with pytest.raises(LoginLimited) as blocked:
            limits.reserve('jellyfin', '203.0.113.9', 'other', now)
        assert blocked.value.retry_after == 900


def test_global_attempt_ceiling_and_expiring_concurrency_leases(tmp_path):
    from homeserver_control.api.public_limits import LoginLimited, LoginLimits
    limits = LoginLimits(tmp_path / 'state.sqlite')
    for index in range(60):
        lease = limits.reserve('jellyfin', f'203.0.113.{index}', str(index), 1000)
        limits.finish(lease, False, 1000)
    with pytest.raises(LoginLimited) as blocked:
        limits.reserve('seerr', '198.51.100.8', 'different', 1000)
    assert blocked.value.retry_after == 60
    limits.reserve('jellyfin', '203.0.113.9', 'leased', 1061)
    with pytest.raises(LoginLimited):
        limits.reserve('jellyfin', '198.51.100.9', 'leased', 1062)
    limits.reserve('jellyfin', '198.51.100.9', 'leased', 1092)


@pytest.mark.asyncio
async def test_stale_cookie_still_allows_anonymous_login_and_assets(tmp_path):
    async with fixture(tmp_path, lambda _: pytest.fail('No upstream call')) as client:
        for path in ('/login', '/_next/static/chunks/main.js'):
            result = await client.get('/authorize/seerr', headers=forwarded(
                path, Cookie='connect.sid=expired'))
            assert result.status_code == 204


@pytest.mark.asyncio
async def test_native_authorization_metadata_can_contain_quoted_comma(tmp_path):
    async with fixture(tmp_path, viewer) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            '/Items', Authorization='MediaBrowser Client="TV, Mobile", Token="viewer-token"'))
    assert result.status_code == 204


@pytest.mark.asyncio
async def test_dns_trust_fails_closed_then_recovers_after_proxy_recreation(tmp_path, monkeypatch):
    import socket
    from types import SimpleNamespace

    from homeserver_control.api import public_access

    now = [1000]
    resolved = [None]
    async def lookup(host, _port, **_kwargs):
        assert host == 'public-proxy'
        if resolved[0] is None:
            raise socket.gaierror('fixture DNS unavailable')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (resolved[0], 8080))]
    monkeypatch.setattr(public_access, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
    async with httpx.AsyncClient() as upstream:
        guard = public_access.PublicAccess(public_access.PublicAccessSettings(
            state_path=tmp_path / 'dns.sqlite'), upstream, lambda: now[0])
        assert await guard.trusted_peers() == set()
        resolved[0] = '172.30.0.8'
        assert await guard.trusted_peers() == set()
        now[0] += 6
        assert await guard.trusted_peers() == {'172.30.0.8'}
        now[0] += 31
        resolved[0] = '172.30.0.9'
        assert await guard.trusted_peers() == {'172.30.0.9'}
        now[0] += 31
        resolved[0] = None
        assert await guard.trusted_peers() == set()


@pytest.mark.asyncio
@pytest.mark.parametrize('path,status', [('/Users/viewer', 204), ('/Users/another', 403),
    ('/Users/viewer/Items', 204), ('/Users/another/Items', 403)])
async def test_jellyfin_profile_routes_are_bound_to_verified_user(tmp_path, path, status):
    async with fixture(tmp_path, viewer) as client:
        result = await client.get('/authorize/jellyfin', headers=forwarded(
            path, **{'X-Emby-Token': 'viewer-token'}))
    assert result.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize('path,status', [('/api/v1/user/4', 204),
    ('/api/v1/user/4/quota', 204), ('/api/v1/user/2', 403),
    ('/imageproxy/tmdb/t/p/w500/poster.jpg', 204), ('/api/v1/request/count', 204)])
async def test_seerr_native_catalog_images_and_own_profile(tmp_path, path, status):
    async with fixture(tmp_path, lambda _: httpx.Response(
            200, json={'id': 4, 'permissions': 32})) as client:
        result = await client.get('/authorize/seerr', headers=forwarded(
            path, Cookie='connect.sid=viewer'))
    assert result.status_code == status
