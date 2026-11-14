"""Seerr's stored Jellyfin key reaches only the native integration routes it uses."""

import base64

import httpx
import pytest
import pytest_asyncio

USER = 'a' * 32
ITEM = 'b' * 32
DEVICE = base64.b64encode(b'BOT_seerr_viewer').decode()


def authorization(*, token=True, device=DEVICE):
    value = f'MediaBrowser Client="Seerr", Device="Seerr", DeviceId="{device}", Version="3.5.0"'
    return {'Authorization': value + (', Token="seerr-key"' if token else '')}


@pytest.fixture
def calls():
    return []


@pytest_asyncio.fixture
async def client(calls):
    from homeserver_control.api.integration_proxy import create_app

    def native(request):
        calls.append(request)
        assert str(request.url).startswith('http://jellyfin:8096/jellyfin/')
        assert 'x-forwarded-for' not in request.headers
        assert 'x-real-ip' not in request.headers
        if request.url.path == '/jellyfin/Devices' and request.method == 'GET':
            return httpx.Response(200, json={'Items': [
                {'Id': DEVICE, 'AppName': 'Seerr', 'LastUserName': 'viewer'},
                {'Id': 'some-tv', 'AppName': 'Jellyfin TV'},
            ]})
        if request.method == 'DELETE':
            return httpx.Response(204)
        if request.url.path == '/jellyfin/UserImage':
            return httpx.Response(200, content=b'avatar-bytes', headers={
                'content-type': 'image/png', 'last-modified': 'Sun, 18 Oct 2026 12:00:00 GMT'})
        return httpx.Response(200, json={'ok': True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as upstream:
        app = create_app(client=upstream, trusted_peer_ips=('192.0.2.5',))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=('192.0.2.5', 1)),
            base_url='http://integration-proxy:8787',
        ) as caller:
            yield caller


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [
    '/System/Info', '/Users', '/Users/Me', f'/Users/{USER}', '/Library/MediaFolders',
    f'/Users/{USER}/Views', f'/Shows/{ITEM}/Seasons',
    f'/Shows/{ITEM}/Episodes?seasonId={ITEM}&fields=MediaSources',
    f'/Items?ids={ITEM}&fields=ProviderIds,MediaSources,Width,Height,IsHD,DateCreated',
    f'/Items/Latest?Limit=12&ParentId={ITEM}&userId=Me',
    '/Items?SortBy=SortName&SortOrder=Ascending&IncludeItemTypes=Series,Movie,Others'
    f'&Recursive=true&StartIndex=0&ParentId={ITEM}&collapseBoxSetItems=false',
])
async def test_real_seerr_catalog_calls_reach_fixed_jellyfin(client, calls, path):
    response = await client.get('/jellyfin' + path, headers=authorization())
    assert response.status_code == 200
    assert len(calls) == 1
    assert 'Token="seerr-key"' in calls[0].headers['authorization']


@pytest.mark.asyncio
async def test_native_login_discards_seerr_supplied_origin_headers(client, calls):
    response = await client.post('/jellyfin/Users/AuthenticateByName',
                                 json={'Username': 'viewer', 'Pw': 'fixture-password'},
                                 headers=authorization(token=False) | {
                                     'X-Forwarded-For': '127.0.0.1', 'X-Real-IP': '127.0.0.1'})
    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [
    ('GET', '/Auth/Keys'), ('POST', '/Auth/Keys?App=evil'),
    ('POST', f'/Users/{USER}/Policy'), ('POST', f'/Users/{USER}/Password'),
    ('DELETE', f'/Items/{ITEM}'), ('POST', '/System/Shutdown'),
    ('GET', '/System/Configuration'), ('GET', '/System/Logs'),
    ('POST', '/ScheduledTasks/Running/some-task'), ('GET', '/Plugins'),
    ('GET', '/Devices'), ('POST', '/QuickConnect/Initiate'),
    ('GET', '/Items?url=http://router/admin'),
    ('GET', f'/Items?ids={ITEM}&fields=Path,ServerId,MetadataSettings'),
    ('GET', '/Users/%2e%2e/System/Configuration'),
    ('GET', f'/UserImage?UserId={USER}&url=http://router/'),
])
async def test_jellyfin_privileged_routes_never_reach_backend(client, calls, method, path):
    response = await client.request(method, '/jellyfin' + path, headers=authorization())
    assert response.status_code == 403
    assert not calls


@pytest.mark.asyncio
async def test_avatar_metadata_and_bytes_use_only_fixed_user_image_route(client, calls):
    for method in ('GET', 'HEAD'):
        response = await client.request(method, f'/jellyfin/UserImage?UserId={USER}')
        assert response.status_code == 200
        assert response.headers['content-type'] == 'image/png'
        assert response.headers['last-modified'] == 'Sun, 18 Oct 2026 12:00:00 GMT'
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_logout_deletes_only_verified_seerr_device(client, calls):
    response = await client.delete('/jellyfin/Devices', params={'Id': DEVICE},
                                   headers=authorization(device='seerr'))
    assert response.status_code == 204
    assert [request.method for request in calls] == ['GET', 'DELETE']
    calls.clear()
    response = await client.delete('/jellyfin/Devices?Id=some-tv',
                                   headers=authorization(device='seerr'))
    assert response.status_code == 403
    assert all(request.method == 'GET' for request in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize('headers', [
    {'Authorization': 'Bearer seerr-key'},
    authorization() | {'X-Emby-Token': 'other-key'},
    {'Authorization': 'MediaBrowser Client="Other", Device="Seerr", DeviceId="BOT_seerr", '
     'Version="1", Token="seerr-key"'},
    {'Authorization': 'MediaBrowser Client="Seerr", Device="Seerr", DeviceId="BOT_seerr", '
     'Version="1", Token="seerr-key", Token="second-key"'},
])
async def test_ambiguous_or_unexpected_jellyfin_credentials_fail_closed(client, calls, headers):
    response = await client.get('/jellyfin/Users', headers=headers)
    assert response.status_code == 403
    assert not calls


@pytest.mark.asyncio
async def test_login_body_cannot_embed_upstream_or_privileged_fields(client, calls):
    response = await client.post('/jellyfin/Users/AuthenticateByName',
                                 headers=authorization(token=False), json={
                                     'Username': 'viewer', 'Pw': 'fixture-password',
                                     'IsAdministrator': True, 'url': 'http://router/'})
    assert response.status_code == 403
    assert not calls
