"""Reproduce real pack subtitle failures using only temporary fixtures and mock HTTP."""
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import OPENSUBTITLES_SERIES_FINALIZER_SOURCE
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.subdl import SubDLSource
from homeserver_control.worker.subtitle_language import SubtitlePolicy
from homeserver_control.worker.validation import ValidationError, ValidationResult


def _subtitle(duration=3600):
    lines = []
    for number in range(1, 11):
        start = int(duration * (0.01 + (number - 1) * 0.105))
        def stamp(seconds):
            return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d},000'
        lines.append(f'{number}\n{stamp(start)} --> {stamp(start + 2)}\nLegenda {number}\n')
    return '\n'.join(lines).encode()


def _fixture(tmp_path, monkeypatch, *, embedded=None, candidate=None, srt=None,
             rank=(2, 1080, 0, 0, 0, 0), duration=3600, pack_title='Fixture Show S01'):
    repo = ReservationRepository(tmp_path / 'control.sqlite')
    repo.initialize()
    reservation = repo.reserve(request_id='seerr:fixture:1', source_id='fixture:1',
        media_key='season:tmdb:99999:1', filesystem_id='fixture', budget_bytes=1000,
        free_bytes=10000, total_bytes=20000).reservation_id
    permits = PermitRegistry(repo.path)
    relative = 'pack/S01E01 - Pilot.mkv'
    parent = permits.issue(infohash='a' * 40, metadata_sha256='b' * 64,
        destination='/data/torrents', category='sonarr', reservation_id=reservation,
        scope_key='S01PACK', selected_files=(relative,), budget_bytes=1000,
        expires_at=datetime.now(UTC) + timedelta(hours=1))
    if rank is not None:
        permits.set_quality(parent.token, rank)
    permits.bind_season_pack(parent.token, episode_files={'S01E01': (relative,)})
    permits.authorize(token=parent.token, infohash=parent.infohash,
        destination=parent.destination, metadata_sha256=parent.metadata_sha256,
        effect=lambda _: {'accepted': True})
    child = permits.get_for_reservation(reservation, scope_key='S01E01')
    root = tmp_path / 'torrents'
    video = root / relative
    video.parent.mkdir(parents=True)
    video.write_bytes(b'video')
    streams = [{'codec_type': 'audio', 'tags': {'language': 'eng'}}]
    if embedded is not None:
        streams.append(embedded)
    probe = MediaProbe(1920, 1080, ('eng',), ('por',) if embedded else (),
        {'streams': streams}, duration_seconds=duration)
    def validate(path, **_kwargs):
        return ValidationResult(Path(path), 5, probe)
    monkeypatch.setattr('homeserver_control.worker.series_finalization.validate_media', validate)
    monkeypatch.setattr('homeserver_control.worker.finalization.validate_media', validate)
    content = srt or _subtitle()
    downloaded = []
    def handler(request):
        if request.url.host == 'api.subdl.com':
            assert request.url.params['tmdb_id'] == '99999'
            assert request.url.params['season_number'] == '1'
            assert request.url.params['episode_number'] == '1'
            subs = [] if candidate is None else [{'language': 'BR_PT', 'season': 1, 'episode': 1,
                'unpack_files': [{'language': 'BR_PT', 'season': 1, 'episode': 1,
                    'format': 'srt', 'size': len(content), 'release_name': candidate,
                    'url': '/subtitle/fixture/full'}]}]
            return httpx.Response(200, json={'status': True,
                'results': [{'tmdb_id': 99999, 'type': 'tv'}], 'subtitles': subs})
        if request.url.host == 'dl.subdl.com':
            downloaded.append(request.url.path)
            return httpx.Response(200, content=content)
        if request.url.path == '/api/v3/series':
            return httpx.Response(200, json=[{'id': 1, 'tmdbId': 99999,
                'title': 'Fixture Show', 'genres': ['Animation'],
                'path': '/data/media/tv/Fixture Show'}])
        if request.url.path == '/api/v3/episode':
            return httpx.Response(200, json=[{'id': 11, 'seasonNumber': 1,
                'episodeNumber': 1, 'hasFile': False, 'monitored': True}])
        if request.url.path == '/api/v2/torrents/info':
            return httpx.Response(200, json=[{'hash': parent.infohash, 'progress': 1,
                'amount_left': 0, 'content_path': '/data/torrents/pack', 'name': pack_title}])
        if request.url.path == '/api/v2/torrents/files':
            return httpx.Response(200, json=[{'name': relative, 'size': 5, 'progress': 1}])
        if request.url.path == '/api/v3/config/mediamanagement':
            return httpx.Response(200, json={'copyUsingHardlinks': False})
        raise AssertionError(f'unexpected fixture request {request.method} {request.url.path}')
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    policy = SubtitlePolicy(languages=('pt-BR',), providers=('subdl',))
    worker = SeriesFinalizer(repository=repo, permits=permits, torrent_root=root,
        media_root=tmp_path / 'media', gateway_url='http://gateway', arr_token='fixture',
        sonarr_url='http://sonarr', sonarr_api_key='fixture', client=client,
        subtitle_policy=policy,
        subtitle_source=SubDLSource(api_key='fixture', client=client, policy=policy))
    return worker, child, video, downloaded


@pytest.mark.asyncio
async def test_full_embedded_ptbr_is_ready_before_and_after_import(tmp_path, monkeypatch):
    stream = {'codec_type': 'subtitle', 'codec_name': 'subrip',
        'tags': {'language': 'por', 'title': 'Brazilian Portuguese'}, 'disposition': {'forced': 0}}
    worker, child, video, downloaded = _fixture(tmp_path, monkeypatch, embedded=stream)
    async with worker.client:
        result = await worker._validate_download(child, season=1, number=1)
        assert result[1] is True
        assert await worker.finalize('season:tmdb:99999:1', child.reservation_id) == 'import_guard'
        worker._ensure_subtitle_for_video(video, child, 'S01E01')
    assert downloaded == []


@pytest.mark.asyncio
async def test_forced_embedded_ptbr_does_not_satisfy_full_subtitle_requirement(
    tmp_path, monkeypatch,
):
    stream = {'codec_type': 'subtitle', 'codec_name': 'subrip',
        'tags': {'language': 'por', 'title': 'Brazilian Portuguese [Forced]'},
        'disposition': {'forced': 1}}
    worker, child, video, downloaded = _fixture(tmp_path, monkeypatch, embedded=stream)
    async with worker.client:
        result = await worker.finalize('season:tmdb:99999:1', child.reservation_id)
        assert result == 'waiting_subtitles'
        with pytest.raises(ValidationError, match='subtitle vanished'):
            worker._ensure_subtitle_for_video(video, child, 'S01E01')
    assert downloaded == []


@pytest.mark.asyncio
async def test_pack_subtitle_matches_verified_episode_and_media_family(tmp_path, monkeypatch):
    worker, child, _, downloaded = _fixture(tmp_path, monkeypatch,
        candidate='Fixture.Show.S01E01.1080p.BluRay.x265-GROUP')
    async with worker.client:
        assert await worker.finalize('season:tmdb:99999:1', child.reservation_id) == 'import_guard'
    assert downloaded == ['/subtitle/fixture/full']
    assert worker.subtitle_store.get(child.reservation_id, 'S01E01', child.infohash) == _subtitle()


@pytest.mark.asyncio
@pytest.mark.parametrize('candidate,srt,rank', [
    ('Other.Show.S01E01.1080p.BluRay.x265-GROUP', None, (2,1080,0,0,0,0)),
    ('Fixture.Show.S01E02.1080p.BluRay.x265-GROUP', None, (2,1080,0,0,0,0)),
    ('Fixture.Show.S01E01.1080p.WEB-DL.x265-GROUP', None, (2,1080,0,0,0,0)),
    ('Fixture.Show.S01E01.EXTENDED.1080p.BluRay.x265-GROUP', None, (2,1080,0,0,0,0)),
    ('Fixture.Show.S01E01.1080p.BluRay.x265-GROUP', _subtitle(1800), (2,1080,0,0,0,0)),
    ('Fixture.Show.S01E01.1080p.BluRay.x265-GROUP', None, None),
])
async def test_pack_subtitle_keeps_identity_source_edition_and_runtime_guards(
    tmp_path, monkeypatch, candidate, srt, rank,
):
    worker, child, _, _ = _fixture(tmp_path, monkeypatch, candidate=candidate, srt=srt, rank=rank)
    async with worker.client:
        result = await worker.finalize('season:tmdb:99999:1', child.reservation_id)
        assert result == 'waiting_subtitles'
    assert worker.subtitle_store.get(child.reservation_id, 'S01E01', child.infohash) is None


@pytest.mark.asyncio
async def test_pack_subtitle_requires_observed_duration_for_compatible_release(
    tmp_path, monkeypatch,
):
    worker, child, _, downloaded = _fixture(tmp_path, monkeypatch, duration=None,
        candidate='Fixture.Show.S01E01.1080p.BluRay.x265-GROUP')
    async with worker.client:
        result = await worker.finalize('season:tmdb:99999:1', child.reservation_id)
        assert result == 'waiting_subtitles'
    assert downloaded == []


@pytest.mark.asyncio
async def test_pack_subtitle_keeps_edition_when_child_has_short_name(tmp_path, monkeypatch):
    worker, child, _, downloaded = _fixture(tmp_path, monkeypatch,
        pack_title='Fixture Show S01 EXTENDED BluRay',
        candidate='Fixture.Show.S01E01.1080p.BluRay.x265-GROUP')
    async with worker.client:
        result = await worker.finalize('season:tmdb:99999:1', child.reservation_id)
        assert result == 'waiting_subtitles'
    assert downloaded == []


@pytest.mark.asyncio
async def test_explicit_opensub_series_artifact_is_used_before_and_after_import(
    tmp_path, monkeypatch,
):
    worker, child, video, downloaded = _fixture(tmp_path, monkeypatch)
    content = _subtitle()
    worker.subtitle_store.put(child.reservation_id, child.scope_key, child.infohash,
        content, language='BR_PT', source=OPENSUBTITLES_SERIES_FINALIZER_SOURCE)
    library = tmp_path / 'media'
    library.mkdir()
    imported = library / 'Fixture.Show.S01E01.mkv'
    os.link(video, imported)
    async with worker.client:
        result = await worker._validate_download(child, season=1, number=1)
        assert result[1] is True
        assert await worker.finalize('season:tmdb:99999:1', child.reservation_id) == 'import_guard'
        worker._ensure_subtitle_for_video(imported, child, child.scope_key)
    assert downloaded == []
    assert imported.with_suffix('.pt-BR.srt').read_bytes() == content
    assert worker.subtitle_store.get(child.reservation_id, child.scope_key, child.infohash) is None
    assert worker.subtitle_store.get(child.reservation_id, child.scope_key, child.infohash,
        source=OPENSUBTITLES_SERIES_FINALIZER_SOURCE) == content


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['hash', 'scope', 'language', 'content'])
async def test_opensub_series_artifact_keeps_scope_hash_language_and_integrity_guards(
    tmp_path, monkeypatch, mismatch,
):
    worker, child, _, _ = _fixture(tmp_path, monkeypatch)
    digest = 'c' * 40 if mismatch == 'hash' else child.infohash
    scope = 'S01E02' if mismatch == 'scope' else child.scope_key
    language = 'EN' if mismatch == 'language' else 'BR_PT'
    worker.subtitle_store.put(child.reservation_id, scope, digest, _subtitle(),
        language=language, source=OPENSUBTITLES_SERIES_FINALIZER_SOURCE)
    if mismatch == 'content':
        with worker.repository._connect() as connection:
            connection.execute('UPDATE subtitle_artifacts SET content=?', (b'corrupted',))
    async with worker.client:
        result = await worker.finalize('season:tmdb:99999:1', child.reservation_id)
        assert result == 'waiting_subtitles'
