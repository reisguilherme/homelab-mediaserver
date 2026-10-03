import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeserver_common.storage import StorageUnavailable
from homeserver_control.api.deletion_capture import DeletionAdmission
from homeserver_control.storage_paths import physical_path, source_logical_path
from homeserver_control.worker.deletion_coordinator import (
    DeletionBlocked,
    DeletionCoordinator,
    DeletionRetryable,
)
from homeserver_control.worker.finalization import MovieFinalizer
from homeserver_control.worker.validation import ValidationError


class Registry:
    def __init__(self, root):
        self.pools = {
            name: SimpleNamespace(root=root / name, host_root=Path('/srv') / name,
                                  filesystem_id=name + '-uuid')
            for name in ('ssd', 'hdd')
        }
        self.offline = set()

    def inspect(self, name, *, writable=False):
        if name in self.offline:
            raise StorageUnavailable('offline')
        return SimpleNamespace(filesystem_id=self.pools[name].filesystem_id)

    def resolve(self, name, path, *, writable=False):
        self.inspect(name, writable=writable)
        return self.pools[name].root / Path(path).relative_to('/data')


@pytest.fixture
def registry(tmp_path):
    return Registry(tmp_path)


def test_source_uses_persisted_destination_and_rejects_escape():
    permit = SimpleNamespace(destination='/data/torrents/.placements/abc')
    assert source_logical_path(permit, 'Film/movie.mkv') == (
        '/data/torrents/.placements/abc/Film/movie.mkv')
    for name in ('../movie.mkv', '/other/movie.mkv', 'Film/../movie.mkv'):
        with pytest.raises(StorageUnavailable):
            source_logical_path(permit, name)


def test_physical_identity_uses_mergerfs_branch_and_rejects_duplicates(registry, monkeypatch):
    logical = Path('/data/media/tv/a.mkv')
    physical = registry.resolve('hdd', logical)
    physical.parent.mkdir(parents=True)
    physical.write_bytes(b'video')
    monkeypatch.setattr(os, 'getxattr', lambda p, key: b'/srv/hdd/media/tv/a.mkv')
    assert physical_path(registry, logical) == ('hdd', physical)
    monkeypatch.setattr(os, 'getxattr', lambda p, key: (
        b'/srv/hdd/media/tv/a.mkv' if key.endswith('fullpath') else
        b'/srv/hdd/media/tv/a.mkv\0/srv/ssd/media/tv/a.mkv'))
    with pytest.raises(StorageUnavailable):
        physical_path(registry, logical)


def test_missing_hdd_does_not_block_ssd_identity(registry, monkeypatch):
    registry.offline.add('hdd')
    monkeypatch.setattr(os, 'getxattr', lambda p, key: b'/srv/ssd/media/movies/a.mkv')
    assert physical_path(registry, '/data/media/movies/a.mkv')[0] == 'ssd'
    monkeypatch.setattr(os, 'getxattr', lambda p, key: b'/srv/hdd/media/movies/a.mkv')
    with pytest.raises(StorageUnavailable):
        physical_path(registry, '/data/media/movies/a.mkv')


@pytest.mark.parametrize('reported', [b'/unknown/media/a.mkv', b'/srv/hdd/media/../a.mkv',
                                     b'/srv/hdd/media/other.mkv'])
def test_branch_mapping_must_match_exact_logical_path(registry, monkeypatch, reported):
    monkeypatch.setattr(os, 'getxattr', lambda p, key: reported)
    with pytest.raises(StorageUnavailable):
        physical_path(registry, '/data/media/a.mkv')


def finalizer(tmp_path, registry=None):
    from homeserver_control.persistence.db import ReservationRepository

    repo = ReservationRepository(tmp_path / 'control.sqlite')
    repo.initialize()
    permits = SimpleNamespace(storage_registry=registry, placement_valid=lambda *a, **k: True)
    return MovieFinalizer(repository=repo, permits=permits,
                          torrent_root=tmp_path / 'union/torrents',
                          media_root=tmp_path / 'union/media/movies',
                          gateway_url='http://gateway', arr_token='fixture',
                          radarr_url='http://radarr', radarr_api_key='fixture')


def test_finalizer_uses_exclusive_destination_in_fixture(tmp_path):
    worker = finalizer(tmp_path)
    source = worker.torrent_root / '.placements/p/Film/a.mkv'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'video')
    permit = SimpleNamespace(destination='/data/torrents/.placements/p')
    assert worker._source_path(permit, 'Film/a.mkv') == source


@pytest.mark.asyncio
async def test_registered_finalizer_never_approves_copy_fallback(tmp_path, registry):
    worker = finalizer(tmp_path, registry)
    assert await worker._copy_fallback_fits([]) is False


def test_import_verifies_physical_inode_even_when_copy_allowed(tmp_path, registry, monkeypatch):
    worker = finalizer(tmp_path, registry)
    permit = SimpleNamespace(destination='/data/torrents/.placements/p', pool_id='hdd',
                             filesystem_id='hdd-uuid', selected_files=('Film/a.mkv',))
    source = worker.torrent_root / '.placements/p/Film/a.mkv'
    video = worker.media_root / 'Film/a.mkv'
    for path in (source, video):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'video')
    physical_source = registry.resolve('hdd', '/data/torrents/.placements/p/Film/a.mkv')
    physical_video = registry.resolve('hdd', '/data/media/movies/Film/a.mkv')
    for path in (physical_source, physical_video):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'video')
    monkeypatch.setattr(os, 'getxattr', lambda path, key: os.fsencode(
        '/srv/hdd/' + str(Path(path).relative_to(tmp_path / 'union'))))
    assert not worker._import_matches_source(video, permit, copy_allowed=True)
    physical_video.unlink()
    os.link(physical_source, physical_video)
    assert worker._import_matches_source(video, permit, copy_allowed=False)
    registry.offline.add('hdd')
    with pytest.raises(ValidationError):
        worker._import_matches_source(video, permit, copy_allowed=False)


def deletion_tools(tmp_path, registry):
    from homeserver_control.persistence.deletion_jobs import DeletionJobStore

    jobs = DeletionJobStore(tmp_path / 'deletions.sqlite')
    jobs.initialize()
    shared = dict(jobs=jobs, media_root=tmp_path / 'union/media',
                  snapshot_path=tmp_path / 'absent.json', filesystem_id='ssd-uuid',
                  radarr_url='http://radarr', radarr_api_key='fixture',
                  sonarr_url='http://sonarr', sonarr_api_key='fixture',
                  jellyfin_url='http://jellyfin', storage_registry=registry)
    return (DeletionAdmission(**shared), DeletionCoordinator(
        **shared, data_root=tmp_path / 'union', seerr_url='http://seerr',
        seerr_api_key='fixture', gateway_url='http://gateway', arr_token='fixture',
        jellyfin_api_key='fixture'))


def test_deletion_capture_records_physical_identity_and_disconnect_retries(
    tmp_path, registry, monkeypatch,
):
    admission, coordinator = deletion_tools(tmp_path, registry)
    logical = tmp_path / 'union/media/movies/a.mkv'
    physical = registry.resolve('hdd', '/data/media/movies/a.mkv')
    for path in (logical, physical):
        path.parent.mkdir(parents=True)
        path.write_bytes(b'video')
    monkeypatch.setattr(os, 'getxattr', lambda p, key: b'/srv/hdd/media/movies/a.mkv')
    _, identity = admission._file_identity(str(logical), 'movies')
    assert identity['pool_id'] == 'hdd'
    assert identity['filesystem_id'] == 'hdd-uuid'
    assert identity['inode'] == physical.stat().st_ino
    job = dict(item_type='Movie', payload=dict(media_key='movie:tmdb:1', tmdb_id=1,
               radarr_id=1, radarr_file_id=1, file_path=str(logical), file_identity=identity))
    assert coordinator._file(job, must_exist=True)[1].st_ino == physical.stat().st_ino
    logical.unlink()
    physical.unlink()
    registry.offline.add('hdd')
    with pytest.raises(DeletionRetryable):
        coordinator._file(job, must_exist=False)
    registry.offline.clear()
    assert coordinator._file(job, must_exist=False)[1] is None


def test_historical_deletion_without_pool_proof_blocks(tmp_path, registry):
    _, coordinator = deletion_tools(tmp_path, registry)
    video = tmp_path / 'union/media/movies/a.mkv'
    video.parent.mkdir(parents=True)
    video.write_bytes(b'video')
    status = video.stat()
    job = dict(item_type='Movie', payload=dict(media_key='movie:tmdb:1', tmdb_id=1,
               radarr_id=1, radarr_file_id=1, file_path=str(video), file_identity=dict(
                   device=status.st_dev, inode=status.st_ino, size=5, mtime_ns=status.st_mtime_ns)))
    with pytest.raises(DeletionBlocked, match='physical'):
        coordinator._file(job, must_exist=False)


def test_season_directory_captures_both_branches_and_waits_for_missing_pool(
    tmp_path, registry, monkeypatch,
):
    admission, coordinator = deletion_tools(tmp_path, registry)
    directory = tmp_path / 'union/media/tv/Series/Season 01'
    directory.mkdir(parents=True)
    for name in ('ssd', 'hdd'):
        registry.resolve(name, '/data/media/tv/Series/Season 01').mkdir(parents=True)
    monkeypatch.setattr(os, 'getxattr', lambda p, key: (
        b'/srv/ssd/media/tv/Series/Season 01' if key.endswith('fullpath') else
        b'/srv/ssd/media/tv/Series/Season 01\0/srv/hdd/media/tv/Series/Season 01'))
    _, identity = admission._season_directory(str(directory))
    assert {i['pool_id'] for i in identity['physical_directories']} == {'ssd', 'hdd'}
    payload = dict(file_path=str(directory), directory_identity=identity)
    monkeypatch.setattr(coordinator, '_season_payload', lambda job: payload)
    assert coordinator._season_folder({}, must_exist=True) == directory
    registry.offline.add('hdd')
    with pytest.raises(DeletionRetryable):
        coordinator._season_folder({}, must_exist=False)


def test_registered_sidecar_cleanup_only_removes_captured_files(tmp_path, registry, monkeypatch):
    admission, coordinator = deletion_tools(tmp_path, registry)
    directory = tmp_path / 'union/media/movies/Film'
    directory.mkdir(parents=True)
    physical_dir = registry.resolve('ssd', '/data/media/movies/Film')
    physical_dir.mkdir(parents=True)
    video = directory / 'a.mkv'
    for filename in ('a.mkv', 'a.pt-BR.srt'):
        physical = physical_dir / filename
        physical.write_bytes(b'fixture')
        os.link(physical, directory / filename)
    monkeypatch.setattr(os, 'getxattr', lambda p, key: os.fsencode(
        '/srv/ssd/media/movies/Film/' + Path(p).name))
    captured = admission._sidecars(video, 'movies')
    new_sidecar = directory / 'a.en.srt'
    new_sidecar.write_bytes(b'added after capture')
    coordinator._cleanup_sidecars(video, job={'payload': {'sidecar_files': captured}})
    assert not (directory / 'a.pt-BR.srt').exists()
    assert new_sidecar.exists()


def test_legacy_reservation_device_is_verified_against_physical_ssd(tmp_path, registry):
    _, coordinator = deletion_tools(tmp_path, registry)
    registry.pools['ssd'].root.mkdir()
    actual = registry.pools['ssd'].root.stat().st_dev
    assert coordinator._reservation_identity_valid(f'device:{actual}')
    assert not coordinator._reservation_identity_valid(f'device:{actual + 1}')
    registry.offline.add('ssd')
    with pytest.raises(DeletionRetryable):
        coordinator._reservation_identity_valid(f'device:{actual}')


def test_queue_snapshot_preserves_individual_pool_commitments(tmp_path):
    import json

    from homeserver_control.worker.__main__ import _publish_storage_queue
    from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence

    evidence = CapacityEvidence(10, {}, pools=(
        PoolCapacityEvidence('ssd', 'ssd-uuid', 10),
        PoolCapacityEvidence('hdd', 'hdd-uuid', 50),
    ))
    permits = SimpleNamespace(pending_bytes=lambda ev, pool_id: {'ssd': 15, 'hdd': 20}[pool_id])
    destination = tmp_path / 'runtime/storage-queue.json'
    _publish_storage_queue(destination, evidence, permits)
    payload = json.loads(destination.read_text())
    assert isinstance(payload['measured_at'], float)
    assert payload['pools'] == [
        dict(pool_id='ssd', filesystem_id='ssd-uuid', pending_bytes=15, available_bytes=0),
        dict(pool_id='hdd', filesystem_id='hdd-uuid', pending_bytes=20, available_bytes=30),
    ]
    assert list(destination.parent.iterdir()) == [destination]


def test_recovered_subtitle_is_created_beside_source_and_physically_linked(
    tmp_path, registry, monkeypatch,
):
    from homeserver_control.persistence.subtitle_artifacts import MOVIE_FINALIZER_SOURCE

    worker = finalizer(tmp_path, registry)
    # A single physical branch is its own union view in this Linux fixture.
    registry.pools['hdd'].root = tmp_path / 'union'
    permit = SimpleNamespace(destination='/data/torrents/.placements/p', pool_id='hdd',
                             filesystem_id='hdd-uuid', selected_files=('Film/a.mkv',),
                             reservation_id='reservation', infohash='a' * 40)
    source = worker.torrent_root / '.placements/p/Film/a.mkv'
    video = worker.media_root / 'Film/a.mkv'
    source.parent.mkdir(parents=True)
    video.parent.mkdir(parents=True)
    source.write_bytes(b'video')
    os.link(source, video)
    monkeypatch.setattr(os, 'getxattr', lambda path, key: os.fsencode(
        '/srv/hdd/' + str(Path(path).relative_to(tmp_path / 'union'))))
    content = b'1\n00:00:01,000 --> 00:00:03,000\nLegenda em portugues\n'
    reservation = worker.repository.reserve(
        request_id='request', source_id='1', media_key='movie:tmdb:1', filesystem_id='ssd-uuid',
        budget_bytes=0, free_bytes=100, total_bytes=100,
    )
    permit.reservation_id = reservation.reservation_id
    worker.subtitle_store.put(permit.reservation_id, None, 'a' * 40, content,
                              language='BR_PT', source=MOVIE_FINALIZER_SOURCE)
    worker._ensure_subtitle_for_video(video, permit, None)
    source_subtitle = source.with_suffix('.pt-BR.srt')
    library_subtitle = video.with_suffix('.pt-BR.srt')
    assert source_subtitle.read_bytes() == content
    assert os.path.samestat(source_subtitle.stat(), library_subtitle.stat())


@pytest.mark.asyncio
async def test_registered_series_pack_scans_only_selected_file_at_placement(
    tmp_path, registry, monkeypatch,
):
    import httpx

    from homeserver_control.domain.media_probe import MediaProbe
    from homeserver_control.worker.series_finalization import SeriesFinalizer
    from homeserver_control.worker.validation import ValidationResult

    base = finalizer(tmp_path, registry)
    registry.pools['hdd'].root = tmp_path / 'union'
    worker = SeriesFinalizer(repository=base.repository, permits=base.permits,
                             torrent_root=base.torrent_root, media_root=tmp_path / 'union/media/tv',
                             gateway_url='http://gateway', arr_token='fixture',
                             sonarr_url='http://sonarr', sonarr_api_key='fixture')
    permit = SimpleNamespace(destination='/data/torrents/.placements/pack', pool_id='hdd',
                             filesystem_id='hdd-uuid', selected_files=('Show/Show.S01E01.mkv',),
                             reservation_id='reservation', scope_key='S01E01',
                             infohash='a' * 40, season_pack_parent_id='pack', budget_bytes=5)
    source = worker.torrent_root / '.placements/pack/Show/Show.S01E01.mkv'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'video')
    monkeypatch.setattr(os, 'getxattr', lambda path, key: os.fsencode(
        '/srv/hdd/' + str(Path(path).relative_to(tmp_path / 'union'))))
    monkeypatch.setattr('homeserver_control.worker.series_finalization.validate_media',
                        lambda path, **kwargs: ValidationResult(
                            path, 5, MediaProbe(1920, 1080, ('eng',), (), {})))

    def handler(request):
        if request.url.path.endswith('/info'):
            return httpx.Response(200, json=[dict(hash=permit.infohash, progress=0.5,
                amount_left=5, content_path=permit.destination + '/Show', name='Show')])
        return httpx.Response(200, json=[dict(name=permit.selected_files[0], size=5, progress=1)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker.client = client
        result = await worker._validate_download(permit, season=1, number=1)
    assert result[0] == permit.destination + '/Show/Show.S01E01.mkv'


def test_hardlink_probe_requires_physical_verification_and_cleans_up(tmp_path):
    from homeserver_control.worker.imports import probe_hardlink_as

    source = tmp_path / 'source.mkv'
    source.write_bytes(b'video')
    destination = tmp_path / 'library'
    destination.mkdir()
    assert not probe_hardlink_as(source, destination, uid=os.getuid(), gid=os.getgid(),
                                 verify=lambda target: False)
    assert list(destination.iterdir()) == []
    assert probe_hardlink_as(source, destination, uid=os.getuid(), gid=os.getgid(),
                            verify=lambda target: os.path.samestat(source.stat(), target.stat()))
    assert list(destination.iterdir()) == []


def test_captured_generated_source_subtitle_is_cleaned_with_library_link(
    tmp_path, registry, monkeypatch,
):
    import json
    import sqlite3

    admission, coordinator = deletion_tools(tmp_path, registry)
    registry.pools['hdd'].root = tmp_path / 'union'
    source = tmp_path / 'union/torrents/.placements/p/Film/a.mkv'
    video = tmp_path / 'union/media/movies/Film/a.mkv'
    source.parent.mkdir(parents=True)
    video.parent.mkdir(parents=True)
    source.write_bytes(b'video')
    os.link(source, video)
    source_subtitle = source.with_suffix('.pt-BR.srt')
    library_subtitle = video.with_suffix('.pt-BR.srt')
    source_subtitle.write_bytes(b'subtitle')
    os.link(source_subtitle, library_subtitle)
    with sqlite3.connect(admission.jobs.path) as db:
        db.execute('CREATE TABLE gateway_permits (destination TEXT, selected_files_json TEXT, '
                   'pool_id TEXT, filesystem_id TEXT, state TEXT)')
        db.execute('INSERT INTO gateway_permits VALUES (?,?,?,?,?)', (
            '/data/torrents/.placements/p', json.dumps(['Film/a.mkv']),
            'hdd', 'hdd-uuid', 'confirmed'))
    monkeypatch.setattr(os, 'getxattr', lambda path, key: os.fsencode(
        '/srv/hdd/' + str(Path(path).relative_to(tmp_path / 'union'))))
    sidecars = admission._sidecars(video, 'movies')
    assert sidecars[0]['source_files'][0]['file_path'] == str(source_subtitle)
    coordinator._cleanup_sidecars(video, job={'payload': {'sidecar_files': sidecars}})
    assert not source_subtitle.exists()
    assert not library_subtitle.exists()


@pytest.mark.asyncio
async def test_series_import_completion_remains_ordered_across_physical_pools(
    tmp_path, registry, monkeypatch,
):
    import sqlite3

    import httpx

    from homeserver_control.domain.media_probe import MediaProbe
    from homeserver_control.worker.series_finalization import SeriesFinalizer
    from homeserver_control.worker.validation import ValidationResult

    base = finalizer(tmp_path, registry)
    reserved = base.repository.reserve(
        request_id='request', source_id='1:1', media_key='season:tmdb:1:1',
        filesystem_id='ssd-uuid', budget_bytes=0, free_bytes=100, total_bytes=100,
    )
    permits, paths = {}, {}
    for number, pool_id in ((1, 'ssd'), (2, 'hdd')):
        permit = SimpleNamespace(
            permit_id=f'p{number}', destination=f'/data/torrents/.placements/p{number}',
            pool_id=pool_id, filesystem_id=pool_id + '-uuid',
            selected_files=(f'Show.S01E0{number}.mkv',), state='confirmed',
            reservation_id=reserved.reservation_id, scope_key=f'S01E0{number}',
            infohash=str(number) * 40, budget_bytes=5,
        )
        permits[permit.scope_key] = permit
        source_raw = source_logical_path(permit, permit.selected_files[0])
        video_raw = '/data/media/tv/Show/' + permit.selected_files[0]
        source, video = (registry.resolve(pool_id, raw) for raw in (source_raw, video_raw))
        source.parent.mkdir(parents=True)
        video.parent.mkdir(parents=True)
        source.write_bytes(b'video')
        os.link(source, video)
        for raw, physical in ((source_raw, source), (video_raw, video)):
            local = tmp_path / 'union' / Path(raw).relative_to('/data')
            local.parent.mkdir(parents=True, exist_ok=True)
            os.link(physical, local)
            paths[str(local)] = '/srv/' + pool_id + '/' + str(Path(raw).relative_to('/data'))
        with sqlite3.connect(base.repository.path) as db:
            db.execute(
                "INSERT INTO episode_imports (permit_id,state,updated_at) VALUES (?,?,'now')",
                (permit.permit_id, 'accepted'),
            )
    base.permits.get_for_reservation = lambda reservation, scope_key: permits.get(scope_key)
    imported = {2}

    def handler(request):
        if request.url.path == '/api/v3/series':
            return httpx.Response(200, json=[dict(id=1, tmdbId=1, path='/data/media/tv/Show')])
        if request.url.path == '/api/v3/episode':
            return httpx.Response(200, json=[dict(id=n, seasonNumber=1, episodeNumber=n,
                hasFile=n in imported, episodeFileId=n, monitored=True,
                airDateUtc='2020-01-01T00:00:00Z') for n in (1, 2)])
        n = int(request.url.path.rsplit('/', 1)[-1])
        return httpx.Response(200, json=dict(path=f'/data/media/tv/Show/Show.S01E0{n}.mkv'))

    monkeypatch.setattr(os, 'getxattr', lambda path, key: os.fsencode(paths[str(path)]))
    probe = MediaProbe(1920, 1080, ('pt-BR',), (), {'streams': [{
        'codec_type': 'audio', 'tags': {'language': 'pt-BR'}, 'disposition': {'original': 1},
    }]})
    monkeypatch.setattr('homeserver_control.worker.finalization.validate_media',
                        lambda path, **kwargs: ValidationResult(path, 5, probe))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = SeriesFinalizer(repository=base.repository, permits=base.permits,
            torrent_root=base.torrent_root, media_root=tmp_path / 'union/media/tv',
            gateway_url='http://gateway', arr_token='fixture', sonarr_url='http://sonarr',
            sonarr_api_key='fixture', client=client)
        assert await worker.finalize('season:tmdb:1:1', reserved.reservation_id) == 'import_pending'
        assert base.repository.episode_import_state('p2') == 'accepted'
        imported.add(1)
        assert await worker.finalize('season:tmdb:1:1', reserved.reservation_id) == 'complete'
        assert base.repository.episode_import_state('p1') == 'complete'
        assert base.repository.episode_import_state('p2') == 'accepted'
        assert await worker.finalize('season:tmdb:1:1', reserved.reservation_id) == 'complete'
        assert base.repository.episode_import_state('p2') == 'complete'


@pytest.mark.asyncio
@pytest.mark.parametrize('sidecar_offline', [False, True])
async def test_hdd_video_deletion_respects_bazarr_sidecar_on_ssd(
    tmp_path, registry, monkeypatch, sidecar_offline,
):
    """Per-file capture governs cleanup, even with one shared logical folder."""
    import sqlite3
    from datetime import UTC, datetime, timedelta

    import httpx

    from homeserver_control.gateway.permits import PermitRegistry

    admission, coordinator = deletion_tools(tmp_path, registry)
    directory = tmp_path / 'union/media/movies/Film'
    directory.mkdir(parents=True)
    files = {}
    for filename, pool_id in (
        ('Film.mkv', 'hdd'), ('Film.pt-BR.srt', 'ssd'),
        ('Other.mkv', 'hdd'), ('Other.pt-BR.srt', 'ssd'),
    ):
        physical = registry.resolve(pool_id, '/data/media/movies/Film/' + filename)
        physical.parent.mkdir(parents=True, exist_ok=True)
        physical.write_bytes(filename.encode())
        local = directory / filename
        # Hardlink aliases model the logical view while retaining physical stat proof.
        os.link(physical, local)
        files[filename] = (local, physical, pool_id)
    video, physical_video, _ = files['Film.mkv']
    subtitle, physical_subtitle, _ = files['Film.pt-BR.srt']
    unrelated = {name: physical.stat() for name, (_, physical, _) in files.items()
                 if name.startswith('Other')}
    monkeypatch.setattr(os, 'getxattr', lambda path, key: os.fsencode(
        '/srv/' + files[Path(path).name][2] + '/media/movies/Film/' + Path(path).name))

    # An unrelated SSD torrent with the same video name is not the HDD video's source.
    permits = PermitRegistry(admission.jobs.path)
    permit = permits.issue(
        infohash='b' * 40, metadata_sha256='c' * 64, destination='/data/torrents',
        category='radarr', reservation_id=None, selected_files=('Film.mkv',), budget_bytes=8,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with sqlite3.connect(admission.jobs.path) as db:
        db.execute("UPDATE gateway_permits SET state='confirmed',pool_id='ssd',"
                   "filesystem_id='ssd-uuid' WHERE token=?", (permit.token,))
    other_source = registry.resolve('ssd', '/data/torrents/Film.mkv')
    other_source.parent.mkdir(parents=True)
    other_source.write_bytes(b'Film.mkv')
    calls, movie_present = [], True

    def handler(request):
        nonlocal movie_present
        calls.append((request.method, request.url.path))
        movie = dict(id=1, tmdbId=1, movieFile=dict(
            id=1, path=str(video), size=len(b'Film.mkv')))
        if request.method == 'GET' and request.url.path == '/api/v3/movie':
            return httpx.Response(200, json=[movie])
        if request.method == 'GET' and request.url.path == '/api/v3/movie/1':
            return httpx.Response(200, json=movie) if movie_present else httpx.Response(404)
        if request.method == 'DELETE' and request.url.path == '/api/v3/moviefile/1':
            video.unlink()
            physical_video.unlink()
            return httpx.Response(204)
        if request.method == 'DELETE' and request.url.path == '/api/v3/movie/1':
            assert request.url.params['deleteFiles'] == 'false'
            movie_present = False
            return httpx.Response(204)
        if request.method == 'GET' and request.url.path == '/Items':
            return httpx.Response(200, json={'Items': []})
        raise AssertionError(f'unexpected media operation: {request.method} {request.url.path}')

    await coordinator.client.aclose()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        admission.client = coordinator.client = client
        payload = await admission._capture_movie({'Path': str(video), 'ProviderIds': {'Tmdb': '1'}})
        assert payload['file_identity']['pool_id'] == 'hdd'
        assert payload['file_identity']['filesystem_id'] == 'hdd-uuid'
        assert payload['file_identity']['inode'] == physical_video.stat().st_ino
        captured = payload['sidecar_files']
        assert len(captured) == 1
        assert captured[0]['file_path'] == str(subtitle)
        assert captured[0]['file_identity']['pool_id'] == 'ssd'
        assert captured[0]['file_identity']['filesystem_id'] == 'ssd-uuid'
        assert captured[0]['file_identity']['inode'] == physical_subtitle.stat().st_ino
        assert captured[0]['source_files'] == []
        job = admission.jobs.enqueue('d' * 32, 'Movie', payload)
        admission.jobs.set_stage(job['item_id'], 'torrents_removed')
        newly_added = directory / 'Film.en.srt'
        newly_added.write_bytes(b'not captured')
        if sidecar_offline:
            registry.offline.add('ssd')
            subtitle.unlink()  # Its disappearance from the union is not deletion proof.
            assert await coordinator.run_once() == 'retry'
            pending = admission.jobs.get(job['item_id'])
            assert pending['stage'] == 'torrents_removed'
            assert physical_subtitle.read_bytes() == b'Film.pt-BR.srt'
            assert ('GET', '/Items') not in calls
            registry.offline.clear()
            os.link(physical_subtitle, subtitle)
        assert await coordinator.run_once() == 'complete'
        assert admission.jobs.get(job['item_id'])['stage'] == 'complete'
        assert not video.exists()
        assert not subtitle.exists()
        assert newly_added.read_bytes() == b'not captured'
        assert other_source.read_bytes() == b'Film.mkv'
        for name, expected in unrelated.items():
            local, physical, _ = files[name]
            assert local.read_bytes() == name.encode()
            assert os.path.samestat(physical.stat(), expected)
        assert [path for method, path in calls if method == 'DELETE'] == [
            '/api/v3/moviefile/1', '/api/v3/movie/1',
        ]
