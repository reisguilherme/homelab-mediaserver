from types import SimpleNamespace

import pytest

from homeserver_control.worker import __main__ as cli
from homeserver_control.worker.runtime import CycleReport, WorkerCycle


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata_fails', [False, True])
async def test_brazilian_metadata_failure_is_isolated_from_artwork_and_requests(
    metadata_fails, caplog,
):
    calls = []

    class Covers:
        async def reconcile(self):
            calls.append('artwork')
            raise RuntimeError('private artwork credentials')

    class Metadata:
        async def reconcile(self):
            calls.append('metadata')
            if metadata_fails:
                raise RuntimeError('private metadata credentials')

    class Source:
        async def list_approved(self, _page):
            calls.append('requests')
            return []

    cycle = WorkerCycle(
        source=Source(), scheduler=object(), jellyfin_collections=Covers(),
        jellyfin_brazilian_metadata=Metadata(),
    )
    assert await cycle.run_once() == CycleReport()
    assert calls == ['artwork', 'metadata', 'requests']
    assert 'private artwork credentials' not in caplog.text
    assert 'private metadata credentials' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata_fails', [False, True])
@pytest.mark.parametrize('audited_fails', [False, True])
async def test_audited_metadata_preserves_normal_mutation_guards(
    metadata_fails, audited_fails, caplog,
):
    calls = []
    forbidden_calls = []

    async def capacity():
        calls.append('capacity')

    class Metadata:
        async def reconcile(self):
            calls.append('metadata')
            if metadata_fails:
                raise RuntimeError('private metadata credentials')

    class Audited:
        async def run_once(self):
            calls.append('audited')
            if audited_fails:
                raise RuntimeError('invalid audited manifest')

    class Forbidden:
        def __getattr__(self, name):
            forbidden_calls.append(name)
            raise AssertionError('normal request or download flow must remain disabled')

    forbidden = Forbidden()
    cycle = WorkerCycle(
        source=forbidden, scheduler=forbidden, acquirer=forbidden, finalizer=forbidden,
        series_acquirer=forbidden, series_finalizer=forbidden, cancellation=forbidden,
        deletion_coordinator=forbidden, source_reconciler=forbidden,
        discarded_sources=forbidden, manual_source_replacements=forbidden,
        request_profiles=forbidden, movie_prioritizer=forbidden,
        capacity_refresh=capacity, jellyfin_brazilian_metadata=Metadata(),
        audited_imports=Audited(),
    )
    assert await cycle.run_once() == CycleReport()
    assert calls == ['capacity', 'metadata', 'audited']
    assert forbidden_calls == []
    assert 'private metadata credentials' not in caplog.text


def _configure_cycle(tmp_path, monkeypatch, *, audited, jellyfin_key=True, interval='600'):
    monkeypatch.delenv('HOMESERVER_AUDITED_IMPORT_MANIFEST', raising=False)
    monkeypatch.delenv('HOMESERVER_JELLYFIN_API_KEY', raising=False)
    monkeypatch.setenv('HOMESERVER_CAPACITY_SNAPSHOT', str(tmp_path / 'capacity.json'))
    monkeypatch.setenv('HOMESERVER_RECOVERY_MODE', str(tmp_path / 'RECOVERY_MODE'))
    monkeypatch.setenv('HOMESERVER_SEERR_URL', 'http://seerr')
    monkeypatch.setenv('HOMESERVER_SEERR_API_KEY', 'fixture-seerr')
    monkeypatch.setenv('HOMESERVER_JELLYFIN_URL', 'http://jellyfin/jellyfin')
    monkeypatch.setenv('HOMESERVER_ADMIN_USERNAME', 'fixture-owner')
    monkeypatch.setenv('HOMESERVER_HTTP_TIMEOUT_SECONDS', '7')
    if interval is None:
        monkeypatch.delenv('HOMESERVER_JELLYFIN_COLLECTION_COVERS_INTERVAL_SECONDS', raising=False)
    else:
        monkeypatch.setenv('HOMESERVER_JELLYFIN_COLLECTION_COVERS_INTERVAL_SECONDS', interval)
    monkeypatch.setenv('HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED', 'false')
    if jellyfin_key:
        monkeypatch.setenv('HOMESERVER_JELLYFIN_API_KEY', 'fixture-jellyfin')
    if audited:
        def inspect(_pool):
            raise ValueError('unused storage bootstrap probe')

        monkeypatch.setenv(
            'HOMESERVER_AUDITED_IMPORT_MANIFEST', str(tmp_path / 'audited-imports.json'),
        )
        monkeypatch.setenv('HOMESERVER_ARR_TOKEN', 'fixture-token')
        monkeypatch.setenv('HOMESERVER_SONARR_URL', 'http://sonarr')
        monkeypatch.setenv('HOMESERVER_SONARR_API_KEY', 'fixture-sonarr')
        monkeypatch.setattr(cli, 'load_audited_manifest', lambda path, database: {'jobs': []})
        monkeypatch.setattr(cli, 'load_storage_registry', lambda: SimpleNamespace(inspect=inspect))
    return cli._build_cycle(tmp_path / 'control.sqlite')


@pytest.mark.asyncio
@pytest.mark.parametrize('audited', [False, True])
@pytest.mark.parametrize('interval', [None, '600'])
async def test_brazilian_metadata_uses_jellyfin_preferences_and_closes_on_shutdown(
    tmp_path, monkeypatch, audited, interval,
):
    cycle = _configure_cycle(tmp_path, monkeypatch, audited=audited, interval=interval)
    reconciler = getattr(cycle, 'jellyfin_brazilian_metadata', None)
    assert reconciler is not None
    assert cycle.jellyfin_collections is None
    assert reconciler.base_url == 'http://jellyfin/jellyfin'
    assert reconciler.admin_username == 'fixture-owner'
    assert reconciler.interval_seconds == (300 if interval is None else 600)
    assert reconciler.client.timeout.read == 7
    assert not reconciler.client.is_closed
    await cli._run_forever(cycle, should_run=lambda: False, interval=0)
    assert reconciler.client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('audited', [False, True])
async def test_missing_jellyfin_key_disables_only_native_metadata_reconciliation(
    tmp_path, monkeypatch, audited,
):
    cycle = _configure_cycle(tmp_path, monkeypatch, audited=audited, jellyfin_key=False)
    assert cycle.jellyfin_brazilian_metadata is None
    await cli._run_forever(cycle, should_run=lambda: False, interval=0)
