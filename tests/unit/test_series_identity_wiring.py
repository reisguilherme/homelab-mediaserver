"""Worker construction shares authenticated series identity across native consumers."""

from copy import deepcopy

import httpx
import pytest

from homeserver_control.worker.__main__ import _build_cycle


@pytest.mark.asyncio
async def test_profile_binding_reaches_acquisition_and_finalization(tmp_path, monkeypatch):
    native = {
        'id': 124, 'tmdbId': 0, 'tvdbId': 293012, 'qualityProfileId': 8,
        'title': "Jorel's Brother", 'monitored': True,
        'path': '/data/media/tv/fixture',
    }
    episode = {'id': 7001, 'seriesId': 124, 'seasonNumber': 1, 'episodeNumber': 1}
    clients = []

    def handle(request):
        assert request.method == 'GET'
        if request.url.host == 'seerr':
            if request.url.path == '/api/v1/request':
                return httpx.Response(200, json={'results': [{
                    'id': 127, 'status': 2, 'profileId': 8, 'serverId': 0, 'is4k': False,
                    'media': {'tmdbId': 74606, 'tvdbId': 293012, 'externalServiceId': 124,
                              'mediaType': 'tv'},
                    'seasons': [{'seasonNumber': 1, 'status': 2}],
                }]})
            if request.url.path == '/api/v1/tv/74606':
                return httpx.Response(200, json={'id': 74606, 'name': 'Irmão do Jorel'})
            assert request.url.path == '/api/v1/settings/sonarr'
            return httpx.Response(200, json=[{
                'id': 0, 'hostname': 'sonarr', 'port': 8989, 'baseUrl': '',
                'useSsl': False, 'is4k': False,
            }])
        assert request.url.host == 'sonarr'
        if request.url.path == '/api/v3/qualityprofile':
            return httpx.Response(200, json=[{'id': 8, 'name': 'Standard'}])
        if request.url.path == '/api/v3/series':
            return httpx.Response(200, json=[deepcopy(native)])
        if request.url.path == '/api/v3/series/124':
            return httpx.Response(200, json=deepcopy(native))
        assert request.url.path == '/api/v3/episode'
        assert request.url.params['seriesId'] == '124'
        return httpx.Response(200, json=[episode])

    original_client = httpx.AsyncClient

    def make_client(**kwargs):
        client = original_client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    for name, value in {
        'SEERR_URL': 'http://seerr:5055', 'SEERR_API_KEY': 'fixture',
        'SONARR_URL': 'http://sonarr:8989', 'SONARR_API_KEY': 'fixture',
        'ARR_TOKEN': 'fixture', 'CAPACITY_SNAPSHOT': str(tmp_path / 'capacity.json'),
        'RECOVERY_MODE': str(tmp_path / 'RECOVERY_MODE'),
    }.items():
        monkeypatch.setenv('HOMESERVER_' + name, value)
    for name in ('RADARR_URL', 'RADARR_API_KEY', 'AUDITED_IMPORT_MANIFEST'):
        monkeypatch.delenv('HOMESERVER_' + name, raising=False)
    monkeypatch.setattr('homeserver_control.worker.__main__.httpx.AsyncClient', make_client)
    try:
        cycle = _build_cycle(tmp_path / 'control.sqlite')
        assert cycle is not None
        requested = await cycle.source.list_approved(1)
        assert await cycle.request_profiles.reconcile(requested) == set()
        context = {}
        assert await cycle.series_finalizer._episodes(74606, media_context=context) == [episode]
        assert context['tmdbId'] == 74606
        assert context['_homeserver_subtitle_titles'] == ('Irmão do Jorel',)
        resolved = cycle.series_acquirer.series_identity.resolve([native], 74606)
        assert resolved is not None and resolved['id'] == 124 and resolved['tmdbId'] == 74606
        assert native['tmdbId'] == 0
    finally:
        for client in clients:
            await client.aclose()
