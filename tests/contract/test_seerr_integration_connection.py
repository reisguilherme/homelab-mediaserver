import json

import httpx
import pytest

from homeserver_control.configuration.media_servers import (
    bootstrap_seerr,
    reconcile_seerr,
    reconcile_seerr_security,
)


@pytest.mark.asyncio
async def test_seerr_connections_move_existing_instances_to_fixed_integration_proxy():
    instances = {service: [{'id': 42, 'name': f'HomeServer {service.title()}',
                            'hostname': service, 'port': port, 'baseUrl': f'/{service}',
                            'activeProfileId': 7, 'activeProfileName': 'HomeServer',
                            'manual': 'preserved'}]
                 for service, port in [('radarr', 7878), ('sonarr', 8989)]}
    writes = []
    connection = {'ip': 'jellyfin', 'port': 8096, 'urlBase': '/jellyfin', 'useSsl': False,
                  'apiKey': 'seerr-dedicated-fixture',
                  'libraries': [{'id': 'movies', 'name': 'Movies', 'enabled': True}]}

    def native(request):
        path = request.url.path
        if request.url.host in {'radarr', 'sonarr'}:
            assert path == f'/{request.url.host}/api/v3/qualityprofile'
            return httpx.Response(200, json=[{'id': 7, 'name': 'HomeServer'}])
        if path == '/api/v1/settings/jellyfin':
            if request.method == 'POST':
                payload = json.loads(request.content)
                assert set(payload) == {'ip', 'port', 'urlBase', 'useSsl'}
                connection.update(payload)
            return httpx.Response(200, json=connection)
        if path == '/api/v1/settings/public':
            return httpx.Response(200, json={'initialized': True})
        service = path.split('/')[4]
        if request.method == 'PUT':
            assert path == f'/api/v1/settings/{service}/42'
            payload = json.loads(request.content)
            assert 'id' not in payload
            writes.append(service)
            instances[service] = [payload | {'id': 42}]
        else:
            assert request.method == 'GET'
        return httpx.Response(200, json=instances[service])

    settings = {'HOMESERVER_JELLYFIN_URL': 'http://jellyfin:8096/jellyfin'}
    for service, port in [('radarr', 7878), ('sonarr', 8989)]:
        settings[f'HOMESERVER_{service.upper()}_URL'] = f'http://{service}:{port}/{service}'
        settings[f'HOMESERVER_{service.upper()}_API_KEY'] = f'{service}-synthetic'
    transport = httpx.MockTransport(native)
    async with httpx.AsyncClient(base_url='http://seerr', transport=transport) as client:
        first = await reconcile_seerr(settings, client, 'apply', transport=transport)
        second = await reconcile_seerr(settings, client, 'apply', transport=transport)
    assert first.changes and not second.changes
    assert writes == ['radarr', 'sonarr']
    assert connection['ip'] == 'integration-proxy'
    assert connection['port'] == 8787 and connection['urlBase'] == '/jellyfin'
    assert connection['apiKey'] == 'seerr-dedicated-fixture'
    for service, rows in instances.items():
        assert len(rows) == 1
        row = rows[0]
        assert (row['hostname'], row['port'], row['baseUrl'], row['useSsl']) == (
            'integration-proxy', 8787, f'/{service}', False,
        )
        assert row['id'] == 42 and row['manual'] == 'preserved'
        assert row['activeProfileId'] == 7 and row['preventSearch'] is True


@pytest.mark.asyncio
async def test_initialized_seerr_does_not_recreate_keys_or_authenticate_owner():
    def native(request):
        assert request.method == 'GET'
        assert request.url.path == '/api/v1/settings/public'
        return httpx.Response(200, json={'initialized': True})

    async with httpx.AsyncClient(
        base_url='http://seerr', transport=httpx.MockTransport(native),
    ) as client:
        settings = {'HOMESERVER_SEERR_API_KEY': 'fixture'}
        assert await bootstrap_seerr(settings, client, 'apply') is None


@pytest.mark.asyncio
async def test_seerr_public_security_merges_only_auth_settings_and_preserves_permissions():
    state = {
        'main': {'newPlexLogin': True, 'defaultPermissions': 32,
                 'defaultQuotas': {'movie': {'quotaLimit': 7}}, 'localLogin': True},
        'network': {'csrfProtection': False, 'trustProxy': False,
                    'proxy': {'enabled': False}, 'apiRequestTimeout': 10000},
    }
    writes = []

    def native(request):
        key = request.url.path.rsplit('/', 1)[1]
        if request.method == 'POST':
            payload = json.loads(request.content)
            assert set(payload) == ({'newPlexLogin'} if key == 'main'
                                    else {'csrfProtection', 'trustProxy'})
            writes.append(key)
            state[key].update(payload)
        return httpx.Response(200, json=state[key])

    env = {'HOMESERVER_PUBLIC_SEERR_ORIGIN': 'https://requests.example:8443'}
    async with httpx.AsyncClient(base_url='http://seerr',
                                transport=httpx.MockTransport(native)) as client:
        assert await reconcile_seerr_security(env, client, 'plan')
        assert not writes
        assert await reconcile_seerr_security(env, client, 'apply')
        assert not await reconcile_seerr_security(env, client, 'apply')
    assert writes == ['main', 'network']
    assert state['main']['newPlexLogin'] is False
    assert state['main']['defaultPermissions'] == 32
    assert state['main']['defaultQuotas'] == {'movie': {'quotaLimit': 7}}
    assert state['network']['csrfProtection'] is True
    assert state['network']['trustProxy'] is True
