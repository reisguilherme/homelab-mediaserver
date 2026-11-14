"""A sealed TVDB binding can prove an audited series with a missing native TMDB."""

from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.worker.audited_imports import AuditedManifestImporter
from homeserver_control.worker.validation import ValidationError


async def resolve_native(native, job, *, episode_patch=None):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == '/api/v3/series/84':
            return httpx.Response(200, json=native)
        assert request.url.path == '/api/v3/episode/101'
        return httpx.Response(200, json={
            'id': 101, 'seriesId': 84, 'seasonNumber': 1, 'episodeNumber': 1,
            **(episode_patch or {}),
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        helper = SimpleNamespace(
            radarr_url='http://sonarr', radarr_headers={'X-Api-Key': 'fixture'}, client=client,
        )
        worker = AuditedManifestImporter(
            store=None, manifest_loader=None, permits=None, torrent_store=None,
            helpers={}, gateway_url='http://gateway', arr_token='fixture',
        )
        result = await worker._native(helper, job, {
            'kind': 'episode', 'native_series_id': 84,
            'native_episode_ids': [101], 'scopes': ['S01E01'],
        })
    return result, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_tmdb', [None, 0])
async def test_sealed_tvdb_binding_allows_absent_native_tmdb(missing_tmdb):
    native = {'id': 84, 'tmdbId': missing_tmdb, 'tvdbId': 345246}
    result, calls = await resolve_native(native, {'tmdb_id': 72844, 'tvdb_id': 345246})
    assert result[0] == native  # Do not invent a canonical native TMDB value.
    assert result[1] == [{'id': 101, 'seriesId': 84, 'seasonNumber': 1, 'episodeNumber': 1}]
    assert calls == ['/api/v3/series/84', '/api/v3/episode/101']


@pytest.mark.asyncio
async def test_matching_positive_native_tmdb_retains_existing_resolution():
    native = {'id': 84, 'tmdbId': 72844}
    result, _calls = await resolve_native(native, {'tmdb_id': 72844})
    assert result[0] == native


@pytest.mark.asyncio
@pytest.mark.parametrize('native_patch,job_patch', [
    ({'tmdbId': 72845}, {}),
    ({'tmdbId': '0'}, {}),
    ({'tmdbId': False}, {}),
    ({'tmdbId': True}, {}),
    ({'tmdbId': 0.0}, {}),
    ({'tmdbId': -1}, {}),
    ({'tvdbId': 345247}, {}),
    ({'tvdbId': None}, {}),
    ({'tvdbId': '345246'}, {}),
    ({'tvdbId': 345246.0}, {}),
    ({'tvdbId': True}, {'tvdb_id': True}),
    ({}, {'tvdb_id': None}),
    ({}, {'tvdb_id': 0}),
    ({}, {'tvdb_id': '345246'}),
    ({'id': 85}, {}),
    ({'id': 84.0}, {}),
])
async def test_missing_tmdb_never_overrides_identity_conflicts(native_patch, job_patch):
    native = {'id': 84, 'tmdbId': 0, 'tvdbId': 345246, **native_patch}
    job = {'tmdb_id': 72844, 'tvdb_id': 345246, **job_patch}
    with pytest.raises(ValidationError, match='audited native series identity changed'):
        await resolve_native(native, job)


@pytest.mark.asyncio
@pytest.mark.parametrize('episode_patch', [
    {'id': 102}, {'seriesId': 85}, {'seasonNumber': 2}, {'episodeNumber': 2},
])
async def test_tvdb_fallback_still_checks_exact_episode_and_scope(episode_patch):
    with pytest.raises(ValidationError, match='audited native episode identity changed'):
        await resolve_native(
            {'id': 84, 'tmdbId': 0, 'tvdbId': 345246},
            {'tmdb_id': 72844, 'tvdb_id': 345246}, episode_patch=episode_patch,
        )
