import copy
import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_audited_import_store import manifest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.audited_imports import AuditedImportStore
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.audited_imports import AuditedManifestImporter
from homeserver_control.worker.cancellation import CancellationReconciler
from homeserver_control.worker.runtime import CycleReport, WorkerCycle
from homeserver_control.worker.scheduler import AdmissionScheduler, FilesystemSnapshot


def mixed_manifest():
    data = manifest()
    job = data['jobs'][0]
    # Scopes deliberately span multiple seasons/files and include a partial season.
    job['files'][0]['scopes'] = ['S01E01', 'S02E01']
    job['files'][1]['scopes'] = ['S03E07']
    movie = copy.deepcopy(job)
    movie.update(job_id='movie-job', permit_id='movie-permit', reservation_id='movie-reserve',
                 category='radarr', tmdb_id=456)
    movie['files'] = [{'path': 'Movie.mkv', 'size_bytes': 100, 'kind': 'movie',
                       'native_movie_id': 9}]
    movie['payload_files'] = [{'path': 'Movie.mkv', 'size_bytes': 100}]
    movie.update(excluded_files=[], padding_bytes=0, expected_payload_bytes=100)
    data['jobs'].append(movie)
    return data


class Source:
    def __init__(self, rows):
        self.rows = rows
        self.pages = []
        self.statuses = []

    async def list_approved(self, page):
        self.pages.append(page)
        return self.rows if page == 1 else []

    async def get_request_status(self, source_id):
        self.statuses.append(source_id)
        return None


class Native:
    def __init__(self):
        self.acquired = []
        self.finalized = []

    async def acquire(self, media_key, reservation_id):
        self.acquired.append(media_key)
        return 'waiting_for_release'

    async def finalize(self, media_key, reservation_id):
        self.finalized.append(media_key)
        return 'downloading'


class Profiles:
    def __init__(self):
        self.requests = []
        self.preserved = None

    async def reconcile(self, rows, *, preserve_native_series=frozenset()):
        self.requests = rows
        self.preserved = preserve_native_series
        return set()


class Importer(AuditedManifestImporter):
    async def _finalize(self, row):
        self.finished.append(row['file_id'])


def cycle_fixture(tmp_path, rows, *, data=None):
    database = tmp_path / 'control.sqlite'
    repo = ReservationRepository(database)
    repo.initialize()
    permits = PermitRegistry(database)
    store = AuditedImportStore(database)
    importer = Importer(store=store, manifest_loader=lambda: data or mixed_manifest(),
                        permits=permits, torrent_store=TorrentArtifactStore(database), helpers={},
                        gateway_url='http://gateway', arr_token='fixture')
    importer.finished = []
    source, native, profiles = Source(rows), Native(), Profiles()
    scheduler = AdmissionScheduler(repository=repo, clock=lambda: 1000,
                                   snapshot_provider=lambda: FilesystemSnapshot(
                                       'fixture', 500_000_000_000, 600_000_000_000, 1000))
    cycle = WorkerCycle(source=source, scheduler=scheduler, acquirer=native, finalizer=native,
                        series_acquirer=native, series_finalizer=native,
                        cancellation=CancellationReconciler(source=source, repository=repo),
                        request_profiles=profiles, audited_imports=importer,
                        audited_allow_seerr=True)
    return cycle, repo, source, native, profiles, importer


def request(source_id, media_key):
    return {'source_id': source_id, 'media_key': media_key,
            'kind': 'movie' if media_key.startswith('movie:') else 'season'}


@pytest.mark.asyncio
async def test_hybrid_filters_all_audited_targets_before_reserving_and_profile_changes(tmp_path):
    rows = [request('1', 'movie:tmdb:456'), request('2:1', 'season:tmdb:123:1'),
            request('3:2', 'season:tmdb:123:2'), request('4:3', 'season:tmdb:123:3'),
            request('5:4', 'season:tmdb:123:4'), request('6', 'movie:tmdb:999')]
    cycle, repo, source, native, profiles, importer = cycle_fixture(tmp_path, rows)
    report = await cycle.run_once()
    assert report == CycleReport(processed=6, accepted=2, deferred=4)
    assert native.acquired == ['season:tmdb:123:4', 'movie:tmdb:999']
    assert native.finalized == native.acquired
    assert [row['media_key'] for row in profiles.requests] == native.acquired
    assert profiles.preserved == frozenset({123})
    with sqlite3.connect(repo.path) as connection:
        reserved = connection.execute(
            'SELECT media_key FROM requests ORDER BY media_key',
        ).fetchall()
        assert reserved == [
            ('movie:tmdb:999',), ('season:tmdb:123:4',)]
    assert len(importer.finished) == 3
    assert source.statuses == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'sealed'])
async def test_hybrid_invalid_manifest_after_valid_cycle_closes_all_seerr_mutations(
    tmp_path, failure, caplog,
):
    cycle, repo, source, native, profiles, importer = cycle_fixture(
        tmp_path, [request('1', 'movie:tmdb:999')])
    await cycle.run_once()
    native.acquired.clear()
    native.finalized.clear()
    source.pages.clear()
    profiles.requests.clear()

    def broken():
        if failure == 'missing':
            raise FileNotFoundError('private credential')
        if failure == 'corrupt':
            return {'jobs': []}
        changed = mixed_manifest()
        changed['jobs'][0]['release_title'] = 'Changed identity'
        return changed

    importer.manifest_loader = broken
    assert await cycle.run_once() == CycleReport()
    assert not native.acquired and not native.finalized and not source.pages
    assert not profiles.requests and not source.statuses
    assert 'private credential' not in caplog.text


@pytest.mark.asyncio
async def test_hybrid_pending_import_and_global_callbacks_cannot_touch_audited_jobs(tmp_path):
    cycle, repo, source, native, profiles, importer = cycle_fixture(tmp_path, [])
    cycle.pending_movie_imports = lambda: [
        {'source_id': 'operator', 'reservation_id': 'movie-reserve', 'media_key': 'movie:tmdb:456'},
        {'source_id': 'operator', 'reservation_id': 'reserve-series',
         'media_key': 'movie:tmdb:999'},
    ]

    class Forbidden:
        def __init__(self):
            self.calls = []

        async def run_once(self):
            self.calls.append("run_once")
            raise AssertionError('global mutation cannot run in hybrid mode')

        async def reconcile(self):
            self.calls.append("reconcile")
            raise AssertionError('global mutation cannot run in hybrid mode')

        async def prioritize(self):
            self.calls.append("prioritize")
            raise AssertionError('global mutation cannot run in hybrid mode')

    guard = Forbidden()
    cycle.deletion_coordinator = guard
    cycle.manual_source_replacements = guard
    cycle.source_reconciler = guard
    cycle.discarded_sources = guard
    cycle.movie_prioritizer = guard
    assert await cycle.run_once() == CycleReport()
    assert native.finalized == []
    assert source.statuses == []
    assert guard.calls == []


@pytest.mark.asyncio
async def test_hybrid_keeps_operator_and_filtered_approval_and_caps_imports_at_four(tmp_path):
    data = mixed_manifest()
    for number in (5, 6, 7):
        movie = copy.deepcopy(data['jobs'][1])
        movie.update(job_id=f'job-{number}', permit_id=f'permit-{number}',
                     reservation_id=f'reserve-{number}', tmdb_id=number)
        movie['files'][0]['native_movie_id'] = number
        data['jobs'].append(movie)
    rows = [request('1', 'movie:tmdb:456'), request('2', 'movie:tmdb:999')]
    cycle, repo, source, native, profiles, importer = cycle_fixture(tmp_path, rows, data=data)
    reserved = []
    for identity, source_id, media_key in [
        ('operator:1', 'operator', 'movie:tmdb:777'),
        ('seerr:withdrawn', 'withdrawn', 'movie:tmdb:888'),
        ('seerr:1', '1', 'movie:tmdb:456'),
    ]:
        reserved.append(repo.reserve(request_id=identity, source_id=source_id, media_key=media_key,
                                     filesystem_id='fixture', budget_bytes=10,
                                     free_bytes=500_000_000_000, total_bytes=600_000_000_000))
    operator_permit = importer.permits.issue(
        infohash='a' * 40, metadata_sha256='b' * 64, destination='/data/torrents',
        category='radarr', reservation_id=reserved[0].reservation_id, budget_bytes=10,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    report = await cycle.run_once()
    assert importer.permits.get(operator_permit.token).state == 'authorized'
    assert len(importer.finished) == 4
    assert native.acquired == ['movie:tmdb:999']
    assert report.cancelled == 1
    assert source.statuses == ['withdrawn']
    assert repo.active_reservation(reserved[0].reservation_id) is not None
    assert repo.active_reservation(reserved[1].reservation_id) is None
    assert repo.active_reservation(reserved[2].reservation_id) is not None


@pytest.mark.asyncio
async def test_request_outcomes_are_safe_and_only_repeat_when_state_changes(tmp_path, caplog):
    cycle, _, _, native, _, _ = cycle_fixture(tmp_path, [request('1', 'movie:tmdb:999')])
    with caplog.at_level(logging.INFO):
        await cycle.run_once()
        await cycle.run_once()
    events = [record for record in caplog.records if record.message.startswith('{')]
    outcomes = [json.loads(record.message) for record in events]
    assert outcomes == [
        {'event': 'request_outcome', 'request_id': 'seerr:1', 'media_key': 'movie:tmdb:999',
         'phase': 'acquisition', 'outcome': 'waiting_for_release'},
        {'event': 'request_outcome', 'request_id': 'seerr:1', 'media_key': 'movie:tmdb:999',
         'phase': 'finalization', 'outcome': 'downloading'},
    ]

@pytest.mark.asyncio
async def test_hybrid_profile_conflict_defers_new_season_without_normal_mutations(tmp_path, caplog):
    cycle, _, _, native, _, _ = cycle_fixture(tmp_path, [request('1:4', 'season:tmdb:123:4')])

    class Conflict:
        blocked_reasons = {'season:tmdb:123:4': 'audit_native_profile_conflict'}

        async def reconcile(self, rows, *, preserve_native_series):
            return {'season:tmdb:123:4'}

    cycle.request_profiles = Conflict()
    with caplog.at_level(logging.INFO):
        report = await cycle.run_once()
    assert report.deferred == 1
    assert native.acquired == [] and native.finalized == []
    assert 'audit_native_profile_conflict' in caplog.text


@pytest.mark.asyncio
async def test_completed_audited_targets_remain_excluded(tmp_path):
    cycle, repo, _, native, _, importer = cycle_fixture(
        tmp_path, [request('1', 'movie:tmdb:456'), request('2:2', 'season:tmdb:123:2')])
    importer.prepare_manifest()
    for row in importer.store.pending_files():
        importer.store.mark_imported(row['file_id'], str(tmp_path / 'imported.mkv'))
        importer.store.complete(row['file_id'])
    assert importer.store.pending_files() == []
    assert (await cycle.run_once()).deferred == 2
    assert native.acquired == [] and native.finalized == []
    assert repo.count_reservations() == 0

@pytest.mark.asyncio
async def test_admission_wait_reason_is_logged_again_after_request_recovers(tmp_path, caplog):
    cycle, _, _, _, _, _ = cycle_fixture(tmp_path, [request('1', 'movie:tmdb:999')])
    available = False
    cycle.scheduler.snapshot_provider = lambda: FilesystemSnapshot(
        'fixture' if available else None, 500_000_000_000, 600_000_000_000, 1000)
    with caplog.at_level(logging.INFO):
        await cycle.run_once()
        available = True
        await cycle.run_once()
        available = False
        await cycle.run_once()
    reasons = ['filesystem_snapshot_unavailable' in record.message for record in caplog.records]
    assert sum(reasons) == 2

@pytest.mark.asyncio
@pytest.mark.parametrize('source_kind', ['adapter', 'source', 'media'])
async def test_request_outcomes_never_log_untrusted_upstream_identity(
    tmp_path, caplog, source_kind,
):
    import httpx

    from homeserver_control.adapters.http import ContractError
    from homeserver_control.adapters.seerr import SeerrAdapter

    private = 'https://personal.example/private?api_key=fixture-secret'
    raw = request(private, 'movie:tmdb:123')
    cycle, _, _, _, _, _ = cycle_fixture(tmp_path, [raw])
    client = None
    if source_kind == 'adapter':
        def handler(request):
            results = ([{'id': private, 'status': 2,
                         'media': {'tmdbId': 123, 'mediaType': 'movie'}}]
                       if request.url.params.get('skip') == '0' else [])
            return httpx.Response(200, json={'results': results})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        cycle.source = SeerrAdapter(base_url='http://seerr', api_key='fixture', client=client)
        cycle.cancellation = None
    elif source_kind == 'media':
        raw['source_id'] = '123'
        raw['media_key'] = 'movie:tmdb:' + private
    try:
        with caplog.at_level(logging.INFO):
            try:
                await cycle.run_once()
            except ContractError:
                pass  # Rejection at the adapter boundary also keeps identities out of logs.
    finally:
        if client is not None:
            await client.aclose()
    assert 'fixture-secret' not in caplog.text
    assert 'personal.example' not in caplog.text
    assert 'api_key=' not in caplog.text
