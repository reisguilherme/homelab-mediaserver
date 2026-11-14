"""Exact import intent, descriptor binding, inode readback and subtitle recovery."""
import importlib
import importlib.util
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import Permit
from homeserver_control.persistence.audited_imports import AuditedImportStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.finalization import MovieFinalizer
from homeserver_control.worker.subtitle_language import SubtitlePolicy

SRT = b'1\n00:00:01,000 --> 00:00:02,000\nFixture\n'


def api():
    name = 'homeserver_control.worker.audited_imports'
    assert importlib.util.find_spec(name) is not None, 'audited importer implementation missing'
    return importlib.import_module(name)


def descriptor():
    files = b''
    for name, size in [(b'Show.S01E01E02.mkv', 3), (b'Show.S02E01.mkv', 4), (b'extra.mkv', 2)]:
        files += (b'd6:lengthi' + str(size).encode() + b'e4:pathl'
                  + str(len(name)).encode() + b':' + name + b'ee')
    return (b'd4:infod5:filesl' + files + b'e4:name4:Show12:piece lengthi16384e6:pieces20:'
            + b'a' * 20 + b'ee')


class FixtureHelpers:
    def __init__(self, root, client, category):
        self.torrent_root = root / 'torrents'
        self.media_root = root / category
        self.media_root.mkdir()
        self.radarr_url = 'http://' + category
        self.radarr_headers = {'X-Api-Key': 'fixture'}
        self.client = client
        self.subtitle_policy = SubtitlePolicy(skip_original_audio_languages=())
        self.subtitle_source = None
        self.hardlink = True
        self.import_uid = 1000
        self.import_gid = 1000
        self.jellyfin_url = None
        self.jellyfin_api_key = None

    def _source_path(self, _permit, relative):
        return self.torrent_root / relative

    def _content_path(self, _permit, _raw):
        return self.torrent_root / 'Show'

    def _physical(self, path, _permit):
        return Path(path)

    def _import_directory(self, _permit, _raw):
        return self.media_root

    def _can_hardlink_video(self, _video, **_kwargs):
        return self.hardlink

    def _subtitle_policy_for(self, _media, *, profile_name=None):
        return self.subtitle_policy

    def _installed_bazarr_subtitle(self, *_args):
        raise AssertionError('series must never consult Bazarr')

    async def _notify_jellyfin(self, imported):
        await MovieFinalizer._notify_jellyfin(self, imported)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    content = descriptor()
    inspected = inspect_torrent(content)
    permit = Permit('permit', 'private-token', inspected.infohash, '/data/torrents',
                    datetime.now(UTC) + timedelta(hours=1), category='sonarr',
                    reservation_id='reservation', metadata_sha256=inspected.metadata_sha256,
                    selected_files=tuple(item.path for item in inspected.files),
                    budget_bytes=9, state='confirmed')
    store = AuditedImportStore(tmp_path / 'control.sqlite')
    artifacts = TorrentArtifactStore(tmp_path / 'control.sqlite')
    artifacts.put(permit, content)
    job = {
        'job_id': 'job', 'candidate_id': 'b' * 64, 'permit_id': permit.permit_id,
        'reservation_id': permit.reservation_id, 'category': 'sonarr',
        'metadata_sha256': permit.metadata_sha256, 'entry_id': 'series-1', 'tmdb_id': 123,
        'tvdb_id': 456, 'release_title': 'Show.Complete.720p.WEBRip',
        'expected_payload_bytes': 9, 'padding_bytes': 0,
        'payload_files': [{'path': item.path, 'size_bytes': item.length}
                          for item in inspected.files],
        'files': [
            {'path': inspected.files[0].path, 'size_bytes': 3, 'kind': 'episode',
             'native_series_id': 1, 'native_episode_ids': [11, 12],
             'canonical_episode_ids': [101, 102], 'scopes': ['S01E01', 'S01E02']},
            {'path': inspected.files[1].path, 'size_bytes': 4, 'kind': 'episode',
             'native_series_id': 1, 'native_episode_ids': [21],
             'canonical_episode_ids': [201], 'scopes': ['S02E01']},
        ],
        'excluded_files': [{'path': inspected.files[2].path, 'size_bytes': 2,
                            'reason': 'extra_El_Camino'}],
        'subtitle_policy': {'profile_name': 'Dublado', 'original_language': 'en'},
    }
    manifest = {'schema_version': 1, 'mode': 'audited_manifest', 'manifest_id': 'v12',
                'manifest_sha256': 'c' * 64, 'jobs': [job]}
    store.install_manifest(manifest)
    root = tmp_path / 'torrents' / 'Show'
    root.mkdir(parents=True)
    for item in inspected.files:
        (tmp_path / 'torrents' / item.path).write_bytes(b'v' * item.length)
    ctx = SimpleNamespace(posts=[], native_files={}, complete=True, qbit_changed=False,
                          hardlink_config=True, lose_response=False, wrong_identity=False,
                          copy_import=False, import_on_post=True, subdl_calls=[],
                          notifications=[], notification_status=204)

    def handler(request):
        path = request.url.path
        if path == '/jellyfin/Library/Media/Updated' and request.method == 'POST':
            import json
            payload = json.loads(request.content)
            assert 'Authorization' in request.headers
            assert 'X-Emby-Token' not in request.headers
            assert 'api_key' not in request.url.params
            assert len(payload['Updates']) == 1
            update = payload['Updates'][0]
            assert update['UpdateType'] == 'Modified'
            imported = Path(update['Path'])
            assert imported in ctx.native_files.values()
            source = tmp_path / 'torrents' / 'Show' / imported.name
            assert imported.stat().st_ino == source.stat().st_ino
            # A notification failure must leave the import retryable, not published.
            pending = store.pending_files(now=1e20)
            assert any(row['file']['path'].endswith('/' + imported.name)
                       and row['imported_path'] is None for row in pending)
            ctx.notifications.append(imported)
            return httpx.Response(ctx.notification_status)
        if path == '/api/v2/torrents/info':
            return httpx.Response(200, json=[{
                'hash': permit.infohash, 'category': 'sonarr', 'save_path': '/data/torrents',
                'content_path': '/data/torrents/Show', 'progress': 1 if ctx.complete else .5,
                'amount_left': 0 if ctx.complete else 1,
            }])
        if path == '/api/v2/torrents/files':
            return httpx.Response(200, json=[{
                'name': item.path, 'size': item.length + (1 if ctx.qbit_changed else 0),
                'progress': 1,
            } for item in inspected.files])
        if path == '/api/v3/config/mediamanagement':
            return httpx.Response(200, json={'copyUsingHardlinks': ctx.hardlink_config,
                                           'importExtraFiles': False})
        if path == '/api/v3/series/1':
            return httpx.Response(200, json={'id': 1, 'tmdbId': 999 if ctx.wrong_identity else 123,
                                           'tvdbId': 456, 'path': '/data/media/tv/Show'})
        if path.startswith('/api/v3/episode/'):
            episode = int(path.rsplit('/', 1)[1])
            season, number = {11: (1, 1), 12: (1, 2), 21: (2, 1)}[episode]
            file = ctx.native_files.get(episode)
            return httpx.Response(200, json={'id': episode, 'seriesId': 1,
                'seasonNumber': season, 'episodeNumber': number, 'hasFile': file is not None,
                'episodeFileId': 100 + episode if file else 0})
        if path.startswith('/api/v3/episodefile/'):
            episode = int(path.rsplit('/', 1)[1]) - 100
            return httpx.Response(200, json={'path': '/data/media/tv/'
                                            + ctx.native_files[episode].name})
        if path == '/api/v3/manualimport':
            return httpx.Response(200, json=[{'path': '/data/torrents/' + target['path'],
                'quality': {'quality': {'id': 0}}, 'languages': [{'id': 1, 'name': 'English'}],
                'rejections': [{'reason': 'Unable to parse source'}], 'releaseGroup': 'Fixture'}
                for target in job['files']])
        if path == '/api/v3/command' and request.method == 'POST':
            import json
            payload = json.loads(request.content)
            ctx.posts.append(payload)
            if ctx.import_on_post:
                for row in payload['files']:
                    source = tmp_path / 'torrents' / row['path'].removeprefix('/data/torrents/')
                    dest = tmp_path / 'sonarr' / source.name
                    if not dest.exists():
                        if ctx.copy_import:
                            dest.write_bytes(source.read_bytes())
                        else:
                            os.link(source, dest)
                    for episode in row['episodeIds']:
                        ctx.native_files[episode] = dest
            if ctx.lose_response:
                raise httpx.ReadTimeout('fixture lost response')
            return httpx.Response(201, json={'id': len(ctx.posts)})
        if path.startswith('/api/v3/command/'):
            return httpx.Response(200, json={'status': 'completed'})
        raise AssertionError(f'unexpected request {request.method} {path}')

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = FixtureHelpers(tmp_path, client, 'sonarr')
    registry = SimpleNamespace(get_for_reservation=lambda _rid, **_kw: permit,
        probe_reservation_active=lambda _p: True, placement_valid=lambda _p, **_kw: True)
    monkeypatch.setattr('homeserver_control.domain.media_probe.probe_media', lambda _path:
        MediaProbe(1280, 720, ('eng',), (), {'streams': [
            {'codec_type': 'audio', 'tags': {'language': 'eng'}}]}, 1200))
    return ctx, store, artifacts, helper, registry, manifest, tmp_path


def importer(setup):
    ctx, store, artifacts, helper, registry, manifest, _root = setup
    result = api().AuditedManifestImporter(
        store=store, manifest_loader=lambda: manifest, permits=registry,
        torrent_store=artifacts, helpers={'sonarr': helper}, gateway_url='http://gateway',
        arr_token='fixture', max_files_per_cycle=4,
    )
    return result, ctx, store, helper


@pytest.mark.asyncio
async def test_multiseason_double_episode_exact_import_then_subdl_only(setup):
    worker, ctx, store, helper = importer(setup)

    class SubDL:
        async def fetch(self, **kwargs):
            ctx.subdl_calls.append(kwargs)
            # Requests must happen only after physical hardlink readback.
            assert ctx.native_files
            return SRT

    helper.subtitle_source = SubDL()
    await worker.run_once()
    assert len(ctx.posts) == 2
    assert ctx.posts[0]['name'] == 'ManualImport'
    assert ctx.posts[0]['importMode'] == 'Copy'
    assert ctx.posts[0]['files'][0]['episodeIds'] == [11, 12]
    assert ctx.posts[1]['files'][0]['episodeIds'] == [21]
    assert all('extra.mkv' not in row['path'] for post in ctx.posts for row in post['files'])
    assert not store.pending_files(now=1e20)
    assert [(row['season'], row['episode']) for row in ctx.subdl_calls] == [(1, 1), (2, 1)]
    assert (helper.media_root / 'Show.S01E01E02.pt-BR.srt').read_bytes() == SRT


@pytest.mark.asyncio
@pytest.mark.parametrize('veto', ['incomplete', 'qbit_vector', 'hardlink_config',
                                  'hardlink_uid', 'identity', 'descriptor', 'reservation',
                                  'placement', 'physical_size', 'scope', 'audio', 'symlink'])
async def test_no_post_before_all_physical_and_identity_gates(setup, veto):
    worker, ctx, store, helper = importer(setup)
    permit = worker.permits.get_for_reservation('reservation')
    if veto == 'incomplete':
        ctx.complete = False
    elif veto == 'qbit_vector':
        ctx.qbit_changed = True
    elif veto == 'hardlink_config':
        ctx.hardlink_config = False
    elif veto == 'hardlink_uid':
        helper.hardlink = False
    elif veto == 'identity':
        ctx.wrong_identity = True
    elif veto == 'descriptor':
        worker.torrent_store.get = lambda _p: None
    elif veto == 'reservation':
        worker.permits.probe_reservation_active = lambda _p: False
    elif veto == 'placement':
        worker.permits.placement_valid = lambda _p, **_kw: False
    elif veto == 'physical_size':
        helper._source_path(permit, 'Show/extra.mkv').write_bytes(b'changed')
    elif veto == 'scope':
        permit.scope_key = 'S01E01'
    elif veto == 'audio':
        from homeserver_control.domain import media_probe
        worker_probe = media_probe.probe_media
        def worker_no_audio(path):
            return MediaProbe(1280, 720, (), (), worker_probe(path).raw, 1200)
        media_probe.probe_media = worker_no_audio
        try:
            await worker.run_once()
        finally:
            media_probe.probe_media = worker_probe
        assert ctx.posts == []
        return
    else:
        original = helper.torrent_root / 'Show/Show.S01E01E02.mkv'
        actual = original.with_name('actual.mkv')
        original.rename(actual)
        original.symlink_to(actual.name)
        helper._source_path = lambda _permit, path: (helper.torrent_root / path).resolve()
    await worker.run_once()
    assert ctx.posts == []
    assert len(store.pending_files(now=1e20)) == 2


@pytest.mark.asyncio
async def test_lost_response_never_reposts_and_recovers_inode_before_subtitles(setup):
    worker, ctx, store, _helper = importer(setup)
    ctx.lose_response = True
    ctx.import_on_post = False
    await worker.run_once()
    assert len(ctx.posts) == 2
    assert {row['state'] for row in store.pending_files(now=1e20)} == {'import_unknown'}
    # Force due retries without changing the durable unknown intent.
    for row in store.pending_files(now=1e20):
        store.defer(row['file_id'], 'fixture', retry_after=0)
    await worker.run_once()
    assert len(ctx.posts) == 2
    assert ctx.subdl_calls == []


@pytest.mark.asyncio
async def test_lost_response_with_completed_hardlink_recovers_without_second_post(setup):
    worker, ctx, store, _helper = importer(setup)
    ctx.lose_response = True
    await worker.run_once()
    assert len(ctx.posts) == 2
    for row in store.pending_files(now=1e20):
        store.defer(row['file_id'], 'fixture', retry_after=0)
    await worker.run_once()
    assert len(ctx.posts) == 2
    assert {row['state'] for row in store.pending_files(now=1e20)} == {'subtitle_pending'}


@pytest.mark.asyncio
async def test_copy_readback_is_not_published_or_used_for_subtitles(setup):
    worker, ctx, store, helper = importer(setup)
    helper.jellyfin_url = 'http://jellyfin/jellyfin'
    helper.jellyfin_api_key = 'fixture-jellyfin'
    ctx.copy_import = True
    await worker.run_once()
    assert len(ctx.posts) == 2
    assert {row['state'] for row in store.pending_files(now=1e20)} == {'import_requested'}
    assert ctx.subdl_calls == []
    assert ctx.notifications == []


@pytest.mark.asyncio
async def test_verified_import_notifies_jellyfin_once_before_subtitle_retries(setup):
    worker, ctx, store, helper = importer(setup)
    helper.jellyfin_url = 'http://jellyfin/jellyfin'
    helper.jellyfin_api_key = 'fixture-jellyfin'
    await worker.run_once()
    assert set(ctx.notifications) == set(ctx.native_files.values())
    assert len(ctx.notifications) == 2
    rows = store.pending_files(now=1e20)
    assert {row['state'] for row in rows} == {'subtitle_pending'}
    for row in rows:
        store.defer(row['file_id'], 'fixture', retry_after=0)
    await worker.run_once()
    assert len(ctx.notifications) == 2
    assert len(ctx.posts) == 2


@pytest.mark.asyncio
async def test_jellyfin_failure_retries_verified_import_without_reposting_native_command(setup):
    worker, ctx, store, helper = importer(setup)
    helper.jellyfin_url = 'http://jellyfin/jellyfin'
    helper.jellyfin_api_key = 'fixture-jellyfin'
    ctx.notification_status = 401
    await worker.run_once()
    rows = store.pending_files(now=1e20)
    assert {row['state'] for row in rows} == {'import_requested'}
    assert all(row['imported_path'] is None for row in rows)
    assert len(ctx.posts) == len(ctx.notifications) == 2
    ctx.notification_status = 204
    for row in rows:
        store.defer(row['file_id'], 'fixture', retry_after=0)
    await worker.run_once()
    assert len(ctx.posts) == 2
    assert len(ctx.notifications) == 4
    rows = store.pending_files(now=1e20)
    assert {row['state'] for row in rows} == {'subtitle_pending'}
    assert all(row['imported_path'] for row in rows)


@pytest.mark.asyncio
async def test_missing_subdl_keeps_imported_files_with_clear_pending_state(setup):
    worker, ctx, store, _helper = importer(setup)
    await worker.run_once()
    rows = store.pending_files(now=1e20)
    assert len(ctx.posts) == 2
    assert {row['state'] for row in rows} == {'subtitle_pending'}
    assert all(row['imported_path'] for row in rows)
    for row in rows:
        store.defer(row['file_id'], 'fixture', retry_after=0)
    await worker.run_once()
    assert len(ctx.posts) == 2


@pytest.mark.asyncio
async def test_existing_sidecar_matches_literal_video_name_with_glob_characters(setup):
    worker, ctx, store, _helper = importer(setup)
    await worker.run_once()
    for old in set(ctx.native_files.values()):
        renamed = old.with_stem(old.stem + ' [1]')
        old.rename(renamed)
        renamed.with_name(renamed.stem + '.pt-BR.srt').write_bytes(SRT)
        for episode, path in list(ctx.native_files.items()):
            if path == old:
                ctx.native_files[episode] = renamed
    for row in store.pending_files(now=1e20):
        store.defer(row['file_id'], 'fixture', retry_after=0)
    await worker.run_once()
    assert not store.pending_files(now=1e20)
    assert len(ctx.posts) == 2


@pytest.mark.asyncio
async def test_invalid_manifest_loader_never_touches_any_native_app(setup):
    worker, ctx, _store, _helper = importer(setup)
    worker.manifest_loader = lambda: (_ for _ in ()).throw(ValueError('changed manifest'))
    with pytest.raises(ValueError):
        await worker.run_once()
    assert ctx.posts == []


@pytest.mark.asyncio
async def test_cycle_finalizes_at_most_configured_file_count(setup):
    worker, ctx, _store, _helper = importer(setup)
    worker.max_files_per_cycle = 1
    await worker.run_once()
    assert len(ctx.posts) == 1


def test_subtitle_is_created_on_admitted_branch_before_union_readback(tmp_path):
    union = tmp_path / 'union'
    branch = tmp_path / 'branch'
    union.mkdir()
    branch.mkdir()
    video = union / 'episode.mkv'
    actual = branch / video.name
    actual.write_bytes(b'video')
    os.link(actual, video)
    helper = SimpleNamespace(media_root=union, import_uid=os.getuid(), import_gid=os.getgid(),
        _physical=lambda path, _permit: branch / path.name)
    api().AuditedManifestImporter._install_subtitle(helper, None, video, SRT, 'BR_PT')
    assert (branch / 'episode.pt-BR.srt').read_bytes() == SRT
    assert not (union / 'episode.pt-BR.srt').exists()
