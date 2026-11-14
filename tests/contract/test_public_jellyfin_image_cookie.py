"""Native image loads use a tightly scoped cookie, never anonymous library access."""
from __future__ import annotations

from http.cookies import SimpleCookie

import httpx
import pytest
from test_public_access import fixture, forwarded, viewer

NAME = '__Host-hs-jf-image'
IMAGE = '/jellyfin/Items/0123456789abcdef0123456789abcdef/Images/Primary'


def check_cookie(response, *, deleted=False):
    cookie = SimpleCookie()
    cookie.load(response.headers['set-cookie'])
    assert set(cookie) == {NAME}
    item = cookie[NAME]
    assert item.value == ('' if deleted else 'viewer-token')
    assert item['secure'] and item['httponly'] and item['samesite'] == 'strict'
    assert item['path'] == '/' and not item['domain']
    assert item['max-age'] == ('0' if deleted else '1800')


@pytest.mark.asyncio
@pytest.mark.parametrize('credentials', [{'Authorization': 'MediaBrowser Token="viewer-token"'},
                                      {'X-Emby-Token': 'viewer-token'}, {}])
async def test_explicit_native_session_issues_safe_cookie(tmp_path, credentials):
    path = '/Items' if credentials else '/Items?api_key=viewer-token'
    async with fixture(tmp_path, viewer) as client:
        response = await client.get('/authorize/jellyfin', headers=forwarded(path, **credentials))
    assert response.status_code == 204
    check_cookie(response)


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('path', [IMAGE, IMAGE.replace('Primary', 'Backdrop/0'),
    IMAGE.replace('0123456789abcdef0123456789abcdef', '01234567-89ab-cdef-0123-456789abcdef')])
async def test_cookie_only_image_revalidates_native_user_without_refreshing_ttl(
        tmp_path, method, path):
    async with fixture(tmp_path, viewer) as client:
        response = await client.get('/authorize/jellyfin', headers=forwarded(
            path, method, Cookie=f'{NAME}=viewer-token'))
    assert response.status_code == 204
    assert response.headers['x-verified-authorization'] == 'MediaBrowser Token="viewer-token"'
    assert 'set-cookie' not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize('path,method', [('/Items', 'GET'), ('/UserViews', 'GET'),
    ('/Users/viewer/Images/Primary', 'GET'), ('/Sessions/Logout', 'POST'),
    (IMAGE, 'DELETE'), (IMAGE, 'POST'), (IMAGE + '/extra', 'GET'),
    (IMAGE.replace('Primary', 'Unknown'), 'GET'),
    (IMAGE.replace('0123456789abcdef0123456789abcdef', 'bad'), 'GET'),
    ('/System/Configuration', 'GET')])
async def test_cookie_cannot_authorize_any_other_route_or_mutation(tmp_path, path, method):
    async with fixture(tmp_path, lambda _: pytest.fail('No native request')) as client:
        response = await client.get('/authorize/jellyfin', headers=forwarded(
            path, method, Cookie=f'{NAME}=viewer-token'))
    assert response.status_code in {401, 403}
    assert 'set-cookie' not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize('cookie', [f'{NAME}=viewer-token; {NAME}=viewer-token',
    f'{NAME}=viewer-token; {NAME}=other-token', f'{NAME}=', f'{NAME}="viewer-token"',
    f'{NAME}=viewer%2Dtoken', f'{NAME}', f'{NAME} =viewer-token'])
async def test_duplicate_or_malformed_cookie_fails_closed(tmp_path, cookie):
    async with fixture(tmp_path, lambda _: pytest.fail('No native request')) as client:
        response = await client.get('/authorize/jellyfin', headers=forwarded(IMAGE, Cookie=cookie))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_duplicate_cookie_headers_and_conflicting_explicit_identity_are_denied(tmp_path):
    async with fixture(tmp_path, lambda _: pytest.fail('No native request')) as client:
        for headers in [list(forwarded(IMAGE, Cookie=f'{NAME}=viewer-token').items())
                        + [('Cookie', f'{NAME}=viewer-token')],
                        forwarded(IMAGE, Cookie=f'{NAME}=viewer-token',
                                  Authorization='MediaBrowser Token="other-token"')]:
            assert (await client.get('/authorize/jellyfin', headers=headers)).status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,expected', [
    (401, {}, 401), (403, {}, 403), (200, {}, 403),
    (200, {'Id': 'admin', 'Policy': {'IsAdministrator': True, 'IsDisabled': False}}, 403),
    (200, {'Id': 'viewer', 'Policy': {'IsAdministrator': False, 'IsDisabled': True}}, 403),
])
async def test_revoked_api_key_admin_and_disabled_cookie_remain_denied(
        tmp_path, status, body, expected):
    async with fixture(tmp_path, lambda _: httpx.Response(status, json=body)) as client:
        response = await client.get('/authorize/jellyfin', headers=forwarded(
            IMAGE, Cookie=f'{NAME}=viewer-token'))
    assert response.status_code == expected and 'set-cookie' not in response.headers


@pytest.mark.asyncio
async def test_unrelated_routes_ignore_image_cookie_and_logout_clears_it(tmp_path):
    async with fixture(tmp_path, viewer) as client:
        response = await client.get('/authorize/jellyfin', headers=forwarded(
            '/Items', Cookie=f'{NAME}=malformed token',
            Authorization='MediaBrowser Token="viewer-token"'))
        assert response.status_code == 204
        check_cookie(response)
        logout = await client.get('/authorize/jellyfin', headers=forwarded(
            '/Sessions/Logout', 'POST', Authorization='MediaBrowser Token="viewer-token"'))
    assert logout.status_code == 204
    check_cookie(logout, deleted=True)


@pytest.mark.asyncio
async def test_verified_login_emits_cookie_but_failed_and_admin_login_do_not(tmp_path):
    for mode in ('viewer', 'wrong', 'admin'):
        def native(request, mode=mode):
            if request.url.path.endswith('/Users/AuthenticateByName'):
                return httpx.Response(401 if mode == 'wrong' else 200,
                                      json={'AccessToken': 'viewer-token'})
            if mode == 'admin':
                return httpx.Response(200, json={'Id': 'admin', 'Policy': {
                    'IsAdministrator': True, 'IsDisabled': False}})
            return viewer(request)
        async with fixture(tmp_path, native) as client:
            response = await client.post('/login/jellyfin', headers=forwarded(
                '/Users/AuthenticateByName', 'POST'), json={'Username': mode, 'Pw': 'synthetic'})
        if mode == 'viewer':
            assert response.status_code == 200
            check_cookie(response)
        else:
            assert response.status_code in {401, 403} and 'set-cookie' not in response.headers
