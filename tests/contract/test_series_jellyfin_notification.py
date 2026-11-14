"""Notify Jellyfin only after an accepted episode is verified in the library."""

import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from homeserver_common.jellyfin_auth import jellyfin_headers
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.series_finalization import SeriesFinalizer


@pytest.fixture
def episode(tmp_path):
    repository = ReservationRepository(tmp_path / 'control.sqlite')
    repository.initialize()
    reserved = repository.reserve(
        request_id='seerr:41:1', source_id='41:1', media_key='season:tmdb:101:1',
        filesystem_id='fixture', budget_bytes=1000, free_bytes=100_000, total_bytes=200_000,
    )
    permits = PermitRegistry(repository.path)
    permit = permits.issue(
        infohash='a' * 40, metadata_sha256='b' * 64, destination='/data/torrents',
        category='sonarr', reservation_id=reserved.reservation_id, scope_key='S01E01',
        selected_files=('Fixture.S01E01/Fixture.S01E01.mkv',
                        'Fixture.S01E01/Fixture.S01E01.pt-BR.srt'),
        budget_bytes=1000, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _: {'accepted': True},
    )
    assert repository.claim_episode_import(permit.permit_id)
    repository.record_episode_import(permit.permit_id, '7')
    torrents = tmp_path / 'torrents'
    source = torrents / permit.selected_files[0]
    source.parent.mkdir(parents=True)
    source.write_bytes(b'verified fixture video')
    (torrents / permit.selected_files[1]).write_text(
        '1\n00:00:01,000 --> 00:00:02,000\nLegenda\n', encoding='utf-8',
    )
    media = tmp_path / 'media'
    video = media / 'Fixture/Season 01/Fixture.S01E01.mkv'
    video.parent.mkdir(parents=True)
    os.link(source, video)
    state = SimpleNamespace(
        repository=repository, permits=permits, permit=permit, reservation=reserved.reservation_id,
        torrents=torrents, media=media, source=source, video=video, has_file=True,
        notification_status=204, notifications=[], native_imports=[],
    )

    def handler(request):
        if request.url.host == 'jellyfin':
            assert request.method == 'POST'
            assert request.url.path == '/jellyfin/Library/Media/Updated'
            assert dict(request.url.params) == {}
            assert request.headers['Authorization'] == (
                jellyfin_headers('fixture-key')['Authorization']
            )
            assert 'x-emby-token' not in request.headers
            assert repository.episode_import_state(permit.permit_id) == 'accepted'
            assert os.path.samestat(source.stat(), video.stat())
            state.notifications.append(json.loads(request.content))
            return httpx.Response(state.notification_status)
        if request.url.path == '/api/v3/series':
            return httpx.Response(200, json=[{'id': 1, 'tmdbId': 101}])
        if request.url.path == '/api/v3/episode':
            return httpx.Response(200, json=[{
                'id': 12, 'seriesId': 1, 'seasonNumber': 1, 'episodeNumber': 1,
                'hasFile': state.has_file, 'episodeFileId': 9 if state.has_file else 0,
            }])
        if request.url.path == '/api/v3/episodefile/9':
            return httpx.Response(200, json={
                'path': '/data/media/tv/Fixture/Season 01/Fixture.S01E01.mkv',
            })
        if request.url.path == '/api/v2/torrents/info':
            return httpx.Response(200, json=[{
                'hash': permit.infohash, 'progress': 0.5, 'amount_left': 10,
                'content_path': '/data/torrents/Fixture.S01E01',
            }])
        if request.url.path == '/api/v3/command':
            state.native_imports.append(request)
            return httpx.Response(201, json={'id': 8})
        raise AssertionError(f'unexpected fixture route: {request.method} {request.url.path}')

    state.handler = handler
    return state


def finalizer(episode, client, *, constructor_connection=False):
    connection = {'jellyfin_url': 'http://jellyfin:8096/jellyfin',
                  'jellyfin_api_key': 'fixture-key'}
    helper = SeriesFinalizer(
        repository=episode.repository, permits=episode.permits,
        torrent_root=episode.torrents, media_root=episode.media,
        gateway_url='http://download-gateway:8081', arr_token='fixture-arr',
        sonarr_url='http://sonarr:8989', sonarr_api_key='fixture-sonarr', client=client,
        **(connection if constructor_connection else {}),
    )
    if not constructor_connection:
        helper.jellyfin_url = connection['jellyfin_url']
        helper.jellyfin_api_key = connection['jellyfin_api_key']
    return helper


@pytest.mark.asyncio
async def test_series_constructor_forwards_optional_jellyfin_connection(episode):
    async with httpx.AsyncClient(transport=httpx.MockTransport(episode.handler)) as client:
        helper = finalizer(episode, client, constructor_connection=True)
    assert helper.jellyfin_url == 'http://jellyfin:8096/jellyfin'
    assert helper.jellyfin_api_key == 'fixture-key'


@pytest.mark.asyncio
async def test_verified_episode_notifies_exact_path_before_complete_once(episode):
    async with httpx.AsyncClient(transport=httpx.MockTransport(episode.handler)) as client:
        helper = finalizer(episode, client)
        assert await helper.finalize('season:tmdb:101:1', episode.reservation) == 'complete'
        assert await helper.finalize('season:tmdb:101:1', episode.reservation) == 'complete'
    assert episode.notifications == [{'Updates': [{
        'Path': str(episode.video.resolve()), 'UpdateType': 'Modified',
    }]}]
    assert episode.repository.episode_import_state(episode.permit.permit_id) == 'complete'
    assert episode.native_imports == []


@pytest.mark.asyncio
async def test_failed_notification_retries_without_another_native_import(episode):
    episode.notification_status = 401
    async with httpx.AsyncClient(transport=httpx.MockTransport(episode.handler)) as client:
        helper = finalizer(episode, client)
        with pytest.raises(httpx.HTTPStatusError):
            await helper.finalize('season:tmdb:101:1', episode.reservation)
        assert episode.repository.episode_import_state(episode.permit.permit_id) == 'accepted'
        episode.notification_status = 204
        assert await helper.finalize('season:tmdb:101:1', episode.reservation) == 'complete'
    assert len(episode.notifications) == 2
    assert episode.native_imports == []


@pytest.mark.asyncio
async def test_downloading_episode_does_not_notify_jellyfin(episode):
    with episode.repository._connect() as connection:
        connection.execute('DELETE FROM episode_imports WHERE permit_id=?',
                           (episode.permit.permit_id,))
    episode.has_file = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(episode.handler)) as client:
        helper = finalizer(episode, client)
        assert await helper.finalize('season:tmdb:101:1', episode.reservation) == 'downloading'
    assert episode.notifications == episode.native_imports == []


@pytest.mark.asyncio
async def test_unverified_library_copy_does_not_notify_jellyfin(episode):
    episode.video.unlink()
    episode.video.write_bytes(episode.source.read_bytes())
    async with httpx.AsyncClient(transport=httpx.MockTransport(episode.handler)) as client:
        helper = finalizer(episode, client)
        assert await helper.finalize('season:tmdb:101:1', episode.reservation) == 'import_uncertain'
    assert episode.notifications == episode.native_imports == []
    assert episode.repository.episode_import_state(episode.permit.permit_id) == 'accepted'
