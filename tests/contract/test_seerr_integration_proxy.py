"""The Seerr connection can request titles without configuring or searching Arr."""

import importlib.util
from copy import deepcopy

import httpx
import pytest
import pytest_asyncio


def test_integration_proxy_is_a_standalone_module():
    assert importlib.util.find_spec('homeserver_control.api.integration_proxy') is not None


@pytest.fixture
def native():
    movie = {
        'id': 11, 'tmdbId': 101, 'title': 'Existing movie', 'year': 2020,
        'path': '/data/media/movies/Existing movie (2020)', 'qualityProfileId': 1,
        'tags': [], 'monitored': False, 'images': [{'url': 'https://metadata.example/poster'}],
    }
    series = {
        'id': 22, 'tvdbId': 202, 'title': 'Existing series',
        'titleSlug': 'existing-series',
        'path': '/data/media/tv/Existing series', 'qualityProfileId': 1,
        'languageProfileId': 1,
        'tags': [], 'monitored': True,
        'seasons': [{'seasonNumber': 1, 'monitored': True},
                    {'seasonNumber': 2, 'monitored': False}],
    }
    lookup = deepcopy(series) | {
        'folder': 'Lookup folder', 'remotePoster': 'https://metadata.example/lookup-poster',
        'alternateTitles': [{'title': 'Lookup alternate'}],
        'statistics': {'episodeFileCount': 7},
    }
    lookup['seasons'][0]['statistics'] = {'episodeFileCount': 7}
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host in {'radarr', 'sonarr'}
        assert request.headers['X-Api-Key'] == 'native-key'
        assert 'apikey' not in request.url.params
        service = request.url.host
        suffix = request.url.path.removeprefix('/' + service + '/api/v3')
        resources = {
            '/qualityprofile': [{'id': 1, 'name': 'HomeServer'}], '/tag': [{'id': 3}],
            '/languageprofile': [{'id': 1, 'name': 'English'}],
            '/rootfolder': [{'id': 1, 'path': '/data/media/movies' if service == 'radarr'
                            else '/data/media/tv'}],
            '/movie/11': movie, '/movie': [movie], '/movie/lookup': [movie],
            '/series/22': series, '/series': [series], '/series/lookup': [lookup],
            '/episode/33': {'id': 33, 'seriesId': 22, 'seasonNumber': 1},
            '/episode/34': {'id': 34, 'seriesId': 22, 'seasonNumber': 2},
            '/episode': [{'id': 33, 'seriesId': 22, 'seasonNumber': 1},
                         {'id': 34, 'seriesId': 22, 'seasonNumber': 2}],
            '/system/status': {'version': '4.0.0'}, '/queue': {'records': []},
        }
        if request.method == 'GET':
            return httpx.Response(200, json=deepcopy(resources[suffix]))
        return httpx.Response(201 if request.method == 'POST' else 200, json={'id': 77})

    return handle, calls, movie, series, lookup


@pytest_asyncio.fixture
async def client(native):
    from homeserver_control.api.integration_proxy import create_app

    async with httpx.AsyncClient(transport=httpx.MockTransport(native[0])) as upstream:
        app = create_app(client=upstream, trusted_peer_ips=('192.0.2.5',))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=('192.0.2.5', 1234)),
            base_url='http://integration-proxy:8787',
        ) as caller:
            yield caller


@pytest.mark.asyncio
async def test_seerr_query_key_is_forwarded_only_as_header(client, native):
    response = await client.get('/radarr/api/v3/movie?apikey=native-key&tmdbId=101')
    assert response.status_code == 200
    assert dict(native[1][-1].url.params) == {'tmdbId': '101'}
    assert response.headers.get('location') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path', [
    ('DELETE', '/radarr/api/v3/movie/11'), ('POST', '/radarr/api/v3/command'),
    ('GET', '/sonarr/api/v3/config/host'), ('GET', '/sonarr/api/v3/downloadclient'),
    ('GET', '/radarr/api/v3/release'), ('POST', '/sonarr/api/v3/tag'),
    ('GET', '/radarr/api/v3/movie%2f..%2fconfig'),
    ('GET', '/radarr/api/v3/%2563ommand'), ('GET', '/radarr//api/v3/movie'),
])
async def test_forbidden_routes_never_contact_native(client, native, method, path):
    response = await client.request(method, path, headers={'X-Api-Key': 'native-key'})
    assert response.status_code in {400, 403, 404}
    assert native[1] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('query', [
    'apikey=native-key&apikey=native-key', 'apikey=native-key&url=http://router/',
    'apikey=native-key&tmdbId=101&tmdbId=102', 'apikey=native-key&tmdbId=../config',
])
async def test_query_ambiguity_and_destinations_are_rejected(client, native, query):
    response = await client.get('/radarr/api/v3/movie?' + query)
    assert response.status_code == 403
    assert not native[1]


@pytest.mark.asyncio
async def test_peer_spoofing_cannot_authorize_request(native):
    from homeserver_control.api.integration_proxy import create_app

    async with httpx.AsyncClient(transport=httpx.MockTransport(native[0])) as upstream:
        app = create_app(client=upstream, trusted_peer_ips=('192.0.2.5',))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=('192.0.2.99', 1)),
            base_url='http://integration-proxy',
        ) as caller:
            response = await caller.get('/radarr/api/v3/movie?apikey=native-key', headers={
                'X-Real-IP': '192.0.2.5', 'X-Forwarded-For': '192.0.2.5',
                'Forwarded': 'for=192.0.2.5',
            })
    assert response.status_code == 403
    assert not native[1]


def movie_add():
    return {'title': 'New movie', 'tmdbId': 303, 'year': 2026, 'titleSlug': '303',
            'qualityProfileId': 1, 'profileId': 1, 'minimumAvailability': 'released',
            'rootFolderPath': '/data/media/movies', 'monitored': True, 'tags': [3],
            'addOptions': {'searchForMovie': False}}


@pytest.mark.asyncio
async def test_movie_add_preserves_gateway_admission(client, native):
    response = await client.post('/radarr/api/v3/movie?apikey=native-key', json=movie_add())
    assert response.status_code == 201
    assert native[1][-1].method == 'POST'
    import json
    forwarded = json.loads(native[1][-1].content)
    assert forwarded['rootFolderPath'] == '/data/media/movies'
    assert forwarded['addOptions']['searchForMovie'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'rootFolderPath': '/etc'}, {'path': '/data/media/movies/../../config'},
    {'remotePoster': 'http://router/admin'}, {'images': [{'url': 'http://router/'}]},
    {'addOptions': {'searchForMovie': True}}, {'qualityProfileId': 999},
    {'profileId': 999}, {'tags': [999]}, {'tmdbId': True},
    {'titleSlug': 'forged-slug'},
    {'minimumAvailability': {}},
])
async def test_movie_mutation_injection_never_reaches_native_write(client, native, change):
    response = await client.post('/radarr/api/v3/movie?apikey=native-key',
                                 json=movie_add() | change)
    assert response.status_code == 403
    assert all(call.method == 'GET' for call in native[1])


@pytest.mark.asyncio
async def test_put_uses_existing_native_entity_and_rejects_path_changes(client, native):
    body = deepcopy(native[2]) | {'monitored': True, 'tags': [3]}
    response = await client.put('/radarr/api/v3/movie?apikey=native-key', json=body)
    assert response.status_code == 200
    native[1].clear()
    response = await client.put('/radarr/api/v3/movie?apikey=native-key',
                                json=body | {'path': '/etc'})
    assert response.status_code == 403
    assert all(call.method == 'GET' for call in native[1])


@pytest.mark.asyncio
async def test_sonarr_existing_request_can_monitor_selected_seasons(client, native):
    import json

    body = deepcopy(native[3])
    body['seasons'][1]['monitored'] = True
    response = await client.put('/sonarr/api/v3/series?apikey=native-key', json=body)
    assert response.status_code == 200
    assert json.loads(native[1][-1].content)['titleSlug'] == 'existing-series'


@pytest.mark.asyncio
async def test_existing_sonarr_lookup_returns_canonical_entity_for_seerr_update(client, native):
    response = await client.get('/sonarr/api/v3/series/lookup?apikey=native-key&term=tvdb:202')
    assert response.status_code == 200
    assert response.json() == [native[3]]
    body = response.json()[0]
    body['seasons'][1]['monitored'] = True
    response = await client.put('/sonarr/api/v3/series?apikey=native-key', json=body)
    assert response.status_code == 200
    assert native[1][-1].method == 'PUT'


@pytest.mark.asyncio
async def test_canonical_sonarr_lookup_does_not_allow_forged_lookup_fields(client, native):
    response = await client.get('/sonarr/api/v3/series/lookup?apikey=native-key&term=tvdb:202')
    body = response.json()[0] | {'folder': 'Forged folder'}
    native[1].clear()
    response = await client.put('/sonarr/api/v3/series?apikey=native-key', json=body)
    assert response.status_code == 403
    assert all(call.method == 'GET' for call in native[1])


@pytest.mark.asyncio
@pytest.mark.parametrize('change,status', [
    ({'id': 23}, 502), ({'tvdbId': 203}, 502), ({'path': '/etc'}, 403),
])
async def test_existing_sonarr_lookup_fails_closed_on_canonical_drift(
    client, native, change, status,
):
    native[3].update(change)
    response = await client.get('/sonarr/api/v3/series/lookup?apikey=native-key&term=tvdb:202')
    assert response.status_code == status
    assert all(call.method == 'GET' for call in native[1])


@pytest.mark.asyncio
async def test_new_sonarr_lookup_without_native_id_preserves_lookup_metadata(client, native):
    native[4].pop('id')
    response = await client.get('/sonarr/api/v3/series/lookup?apikey=native-key&term=tvdb:202')
    assert response.status_code == 200
    assert response.json() == [native[4]]
    assert all(not call.url.path.endswith('/series/22') for call in native[1])


@pytest.mark.asyncio
async def test_sonarr_add_preserves_selected_seasons_and_disables_search(client, native):
    import json

    body = {'title': 'Existing series', 'tvdbId': 202, 'qualityProfileId': 1,
            'rootFolderPath': '/data/media/tv', 'monitored': True, 'tags': [],
            'seasonFolder': True, 'seriesType': 'standard', 'monitorNewItems': 'none',
            'seasons': [{'seasonNumber': 1, 'monitored': True},
                        {'seasonNumber': 2, 'monitored': False}],
            'addOptions': {'ignoreEpisodesWithFiles': True, 'searchForMissingEpisodes': False}}
    response = await client.post('/sonarr/api/v3/series?apikey=native-key', json=body)
    assert response.status_code == 201
    forwarded = json.loads(native[1][-1].content)
    assert forwarded['seasons'] == body['seasons']
    assert forwarded['addOptions'] == body['addOptions']


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'path': '/etc'}, {'tvdbId': 999}, {'remotePoster': 'http://router/'},
    {'titleSlug': 'forged-slug'}, {'titleSlug': '202'},
    {'seasons': [{'seasonNumber': 1, 'monitored': False},
                 {'seasonNumber': 2, 'monitored': True}]},
    {'seasons': [{'seasonNumber': 1, 'monitored': True},
                 {'seasonNumber': 99, 'monitored': True}]},
])
async def test_series_update_cannot_change_identity_or_unmonitor_content(client, native, change):
    response = await client.put('/sonarr/api/v3/series?apikey=native-key',
                                json=deepcopy(native[3]) | change)
    assert response.status_code == 403
    assert all(call.method == 'GET' for call in native[1])


@pytest.mark.asyncio
async def test_duplicate_or_conflicting_credentials_are_rejected(client, native):
    for target, headers in [
        ('/radarr/api/v3/movie', [('X-Api-Key', 'native-key'), ('X-Api-Key', 'native-key')]),
        ('/radarr/api/v3/movie?apikey=native-key', {'X-Api-Key': 'native-key'}),
        ('/radarr/api/v3/movie?apikey=native-key', {'Authorization': 'Bearer unrelated'}),
    ]:
        response = await client.get(target, headers=headers)
        assert response.status_code == 403
    assert not native[1]


@pytest.mark.asyncio
async def test_episode_monitor_only_accepts_existing_monitored_season(client, native):
    response = await client.put('/sonarr/api/v3/episode/monitor?apikey=native-key',
                                json={'episodeIds': [33], 'monitored': True})
    assert response.status_code == 200
    native[1].clear()
    response = await client.put('/sonarr/api/v3/episode/monitor?apikey=native-key',
                                json={'episodeIds': [34], 'monitored': True})
    assert response.status_code == 403
    assert all(call.method == 'GET' for call in native[1])


@pytest.mark.asyncio
async def test_duplicate_json_and_oversized_body_never_reach_native(client, native):
    for content, status in [(b'{"tmdbId":1,"tmdbId":2}', 400), (b'x' * 131073, 413)]:
        response = await client.post('/radarr/api/v3/movie?apikey=native-key',
                                     content=content, headers={'Content-Type': 'application/json'})
        assert response.status_code == status
    assert not native[1]


@pytest.mark.asyncio
async def test_native_redirect_is_not_followed_or_returned():
    from homeserver_control.api.integration_proxy import create_app

    seen = []
    def redirect(request):
        seen.append(request)
        return httpx.Response(302, headers={'Location': 'http://router/admin'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as upstream:
        app = create_app(client=upstream, trusted_peer_ips=('192.0.2.5',))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=('192.0.2.5', 1)),
            base_url='http://integration-proxy',
        ) as caller:
            response = await caller.get('/radarr/api/v3/movie?apikey=native-key')
    assert response.status_code == 502
    assert response.headers.get('location') is None
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_dns_failure_cannot_admit_unverified_peer(native, monkeypatch):
    import asyncio
    import socket
    from unittest.mock import AsyncMock

    from homeserver_control.api.integration_proxy import create_app

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo',
                        AsyncMock(side_effect=socket.gaierror('fixture unavailable')))
    async with httpx.AsyncClient(transport=httpx.MockTransport(native[0])) as upstream:
        app = create_app(client=upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=('192.0.2.5', 1)),
            base_url='http://integration-proxy',
        ) as caller:
            response = await caller.get('/radarr/api/v3/movie?apikey=native-key')
    assert response.status_code == 403
    assert not native[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,expected', [('timeout', 503), ('oversize', 502), ('bad-json', 502)])
async def test_upstream_failures_are_bounded_and_sanitized(fault, expected):
    from homeserver_control.api.integration_proxy import MAX_RESPONSE, create_app

    def failure(request):
        if fault == 'timeout':
            raise httpx.ReadTimeout('sensitive-native-key-internal-message', request=request)
        return httpx.Response(200, content=(b'x' * (MAX_RESPONSE + 1)
                                           if fault == 'oversize' else b'not-json'))

    async with httpx.AsyncClient(transport=httpx.MockTransport(failure)) as upstream:
        app = create_app(client=upstream, trusted_peer_ips=('192.0.2.5',))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=('192.0.2.5', 1)),
            base_url='http://integration-proxy',
        ) as caller:
            response = await caller.get('/radarr/api/v3/movie?apikey=sensitive-native-key')
    assert response.status_code == expected
    assert 'sensitive-native-key' not in response.text
