import importlib.util
import json

import httpx
import pytest


def auth_headers(token):
    assert importlib.util.find_spec('homeserver_common.jellyfin_auth') is not None
    from homeserver_common.jellyfin_auth import jellyfin_headers

    return jellyfin_headers(token)


def test_modern_auth_keeps_token_and_stable_client_identity():
    headers = auth_headers('synthetic-token')
    assert set(headers) == {'Authorization'}
    authorization = headers['Authorization']
    assert authorization.startswith('MediaBrowser ')
    assert 'Token="synthetic-token"' in authorization
    for field in ('Client="HomeServer"', 'Device="HomeServer"',
                  'DeviceId="homeserver-internal"', 'Version="1"'):
        assert field in authorization
    assert headers == auth_headers('synthetic-token')


@pytest.mark.parametrize('token', ['', 'x\r\ny', 'x"y', 'x\\y', 'x\x00y',
                                   ' x', 'x y', 'x\ty', 'é', None, 42])
def test_invalid_token_fails_without_echoing_credential(token):
    with pytest.raises(ValueError, match='^Invalid Jellyfin credential$'):
        auth_headers(token)


@pytest.mark.asyncio
async def test_sessions_client_authenticates_when_legacy_headers_are_rejected():
    from homeserver_control.adapters.jellyfin import JellyfinAdapter

    def native(request):
        if ('x-emby-token' in request.headers
                or 'Token="synthetic-token"' not in request.headers.get('authorization', '')):
            return httpx.Response(401)
        return httpx.Response(200, json=[
            {'Client': 'TV', 'PlayState': {'PlayMethod': 'DirectPlay'}},
        ])

    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        result = await JellyfinAdapter(
            base_url='http://jellyfin:8096/jellyfin', api_key='synthetic-token', client=client,
        ).sessions()
    assert result == [{'client': 'TV', 'remote': False, 'mode': 'direct'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('dedicated,expected', [('jellyfin-only', 'jellyfin-only'),
                                               ('', 'shared-existing')])
@pytest.mark.parametrize('service', ['jellyfin', 'seerr', 'discovery'])
async def test_jellyfin_password_override_does_not_change_other_native_credentials(
    dedicated, expected, service,
):
    from homeserver_control.configuration.media_servers import bootstrap_jellyfin, bootstrap_seerr
    from homeserver_control.configuration.reconcile import discover_native_credentials

    logins = []

    def native(request):
        if request.url.path == '/System/Info/Public':
            return httpx.Response(200, json={'StartupWizardCompleted': True})
        if request.url.path == '/api/v1/settings/public':
            return httpx.Response(200, json={'initialized': False})
        if request.url.path == '/Users/AuthenticateByName':
            payload = json.loads(request.content)
            assert payload['Pw'] == expected
            logins.append('jellyfin')
            return httpx.Response(200, json={'AccessToken': 'native-token'})
        if request.url.path == '/api/v1/auth/jellyfin':
            payload = json.loads(request.content)
            assert payload['password'] == expected
            logins.append('seerr')
            return httpx.Response(200, json={})
        if request.url.path == '/api/v1/settings/main':
            return httpx.Response(200, json={'apiKey': 'seerr-key'})
        raise AssertionError('unexpected native request')

    settings = {'HOMESERVER_ADMIN_PASSWORD': 'shared-existing',
                'HOMESERVER_JELLYFIN_ADMIN_PASSWORD': dedicated}
    transport = httpx.MockTransport(native)
    async with httpx.AsyncClient(base_url='http://native', transport=transport) as client:
        if service == 'discovery':
            await discover_native_credentials(settings, transport=transport)
        else:
            bootstrap = bootstrap_jellyfin if service == 'jellyfin' else bootstrap_seerr
            assert await bootstrap(settings, client, 'apply') is None
    assert logins == (['jellyfin', 'seerr'] if service == 'discovery' else [service])
    assert settings['HOMESERVER_ADMIN_PASSWORD'] == 'shared-existing'
