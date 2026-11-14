import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import Permit
from homeserver_control.persistence.audited_imports import AuditedImportStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.audited_imports import AuditedManifestImporter
from homeserver_control.worker.finalization import MovieFinalizer
from homeserver_control.worker.subtitle_language import SubtitlePolicy


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_present', [False, True])
@pytest.mark.parametrize('tracked_download_present', [False, True])
async def test_movie_pin_imports_exactly_then_preserves_bazarr_handoff(
    tmp_path, monkeypatch, provider_present, tracked_download_present,
):
    torrent = (b'd4:infod6:lengthi3e4:name9:movie.mkv12:piece lengthi16384e6:pieces20:'
               + b'a' * 20 + b'ee')
    metadata = inspect_torrent(torrent)
    permit = Permit('movie-permit', 'fixture-token', metadata.infohash, '/data/torrents',
        datetime.now(UTC) + timedelta(hours=1), category='radarr', reservation_id='movie-reserve',
        metadata_sha256=metadata.metadata_sha256, selected_files=('movie.mkv',),
        budget_bytes=3, state='confirmed')
    database = tmp_path / 'control.sqlite'
    store = AuditedImportStore(database)
    artifacts = TorrentArtifactStore(database)
    artifacts.put(permit, torrent)
    manifest = {'schema_version': 1, 'mode': 'audited_manifest', 'manifest_id': 'movie-v12',
        'manifest_sha256': 'a' * 64, 'jobs': [{
            'job_id': 'movie-job', 'candidate_id': 'b' * 64, 'permit_id': permit.permit_id,
            'reservation_id': permit.reservation_id, 'category': 'radarr', 'entry_id': 'movie-1',
            'metadata_sha256': permit.metadata_sha256, 'tmdb_id': 123,
            'release_title': 'Movie.2024.2160p.Original', 'expected_payload_bytes': 3,
            'padding_bytes': 0, 'payload_files': [{'path': 'movie.mkv', 'size_bytes': 3}],
            'files': [{'path': 'movie.mkv', 'size_bytes': 3, 'kind': 'movie',
                       'native_movie_id': 7}], 'excluded_files': [],
            'subtitle_policy': {'profile_name': 'Dublado', 'original_language': 'en'},
        }]}
    root = tmp_path / 'torrents'
    library = tmp_path / 'movies'
    root.mkdir()
    library.mkdir()
    source = root / 'movie.mkv'
    source.write_bytes(b'vid')
    ctx = SimpleNamespace(imported=False, posts=[], bazarr_checks=[])

    def handler(request):
        path = request.url.path
        if path == '/api/v2/torrents/info':
            return httpx.Response(200, json=[{'hash': permit.infohash, 'category': 'radarr',
                'save_path': '/data/torrents', 'content_path': '/data/torrents/movie.mkv',
                'progress': 1, 'amount_left': 0}])
        if path == '/api/v2/torrents/files':
            return httpx.Response(200, json=[{'name': 'movie.mkv', 'size': 3, 'progress': 1}])
        if path == '/api/v3/movie/7':
            return httpx.Response(200, json={'id': 7, 'tmdbId': 123, 'qualityProfileId': 3,
                'path': '/data/media/movies/Movie', 'movieFile':
                {'relativePath': 'movie.mkv'} if ctx.imported else None})
        if path == '/api/v3/config/mediamanagement':
            return httpx.Response(200, json={'copyUsingHardlinks': True, 'importExtraFiles': False})
        if path == '/api/v3/manualimport':
            # Native CDH is disabled in audited mode: no tracked download may exist.
            if request.url.params.get('downloadId') and not tracked_download_present:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{'path': '/data/torrents/movie.mkv',
                'quality': {'quality': {'id': 0}}, 'languages': [{'id': 1, 'name': 'English'}]}])
        if path == '/api/v3/command':
            import json
            ctx.posts.append(json.loads(request.content))
            assert ctx.posts[-1]['files'][0]['movieId'] == 7
            assert ctx.posts[-1]['files'][0]['path'] == '/data/torrents/movie.mkv'
            assert ctx.posts[-1]['files'][0]['downloadId'] == permit.infohash
            (library / 'Movie').mkdir()
            os.link(source, library / 'Movie' / 'movie.mkv')
            ctx.imported = True
            return httpx.Response(201, json={'id': 42})
        raise AssertionError(path)

    policy = SubtitlePolicy(skip_original_audio_languages=())
    helper = MovieFinalizer(
        repository=SimpleNamespace(path=database), permits=SimpleNamespace(), torrent_root=root,
        gateway_url='http://gateway', arr_token='fixture', radarr_url='http://radarr',
        radarr_api_key='fixture', media_root=library, subtitle_policy=policy,
        import_uid=1000, import_gid=1000, client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(helper, '_import_directory', lambda _p, _raw: library)
    monkeypatch.setattr(helper, '_can_hardlink_video', lambda *_a, **_kw: True)

    def installed(video, _permit, _policy, preferred):
        assert ctx.imported and video.stat().st_ino == source.stat().st_ino
        ctx.bazarr_checks.append(preferred)
        return video.with_suffix('.pt-BR.srt') if provider_present else None

    monkeypatch.setattr(helper, '_installed_bazarr_subtitle', installed)
    monkeypatch.setattr('homeserver_control.domain.media_probe.probe_media', lambda _p:
        MediaProbe(3840, 2160, ('eng',), (), {'streams': [
            {'codec_type': 'audio', 'tags': {'language': 'eng'}}]}, 7000))
    permits = SimpleNamespace(get_for_reservation=lambda *_a, **_kw: permit,
        probe_reservation_active=lambda _p: True, placement_valid=lambda _p, **_kw: True)
    worker = AuditedManifestImporter(store=store, manifest_loader=lambda: manifest,
        permits=permits, torrent_store=artifacts, helpers={'radarr': helper},
        gateway_url='http://gateway', arr_token='fixture')
    await worker.run_once()
    assert len(ctx.posts) == 1
    assert ctx.posts[0]['name'] == 'ManualImport' and ctx.posts[0]['importMode'] == 'Copy'
    assert ctx.bazarr_checks
    pending = store.pending_files(now=1e20)
    if provider_present:
        assert not pending
    else:
        assert len(pending) == 1 and pending[0]['state'] == 'subtitle_pending'
        assert pending[0]['imported_path']
