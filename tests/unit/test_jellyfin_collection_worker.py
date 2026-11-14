from types import SimpleNamespace

import pytest

from homeserver_control.worker import __main__ as cli
from homeserver_control.worker.__main__ import _build_cycle
from homeserver_control.worker.runtime import WorkerCycle


@pytest.mark.asyncio
async def test_artwork_failure_does_not_stop_request_processing(caplog):
    calls = []

    class Covers:
        async def reconcile(self):
            calls.append('artwork')
            raise RuntimeError('native failure contains private query credentials')

    class Source:
        async def list_approved(self, page):
            calls.append('requests')
            return []

    cycle = WorkerCycle(source=Source(), scheduler=object(), jellyfin_collections=Covers())
    result = await cycle.run_once()
    assert calls == ['artwork', 'requests']
    assert result.processed == 0
    assert 'private query credentials' not in caplog.text


@pytest.mark.parametrize('audited', [False, True])
def test_collection_worker_uses_runtime_preferences_and_can_be_disabled(
    tmp_path, monkeypatch, audited,
):
    monkeypatch.delenv('HOMESERVER_AUDITED_IMPORT_MANIFEST', raising=False)
    monkeypatch.setenv('HOMESERVER_CAPACITY_SNAPSHOT', str(tmp_path / 'capacity.json'))
    monkeypatch.setenv('HOMESERVER_RECOVERY_MODE', str(tmp_path / 'RECOVERY_MODE'))
    monkeypatch.setenv('HOMESERVER_SEERR_URL', 'http://seerr:5055')
    monkeypatch.setenv('HOMESERVER_SEERR_API_KEY', 'fixture-seerr')
    monkeypatch.setenv('HOMESERVER_JELLYFIN_URL', 'http://jellyfin:8096/jellyfin')
    monkeypatch.setenv('HOMESERVER_JELLYFIN_API_KEY', 'fixture-jellyfin')
    monkeypatch.setenv('HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED', 'true')
    monkeypatch.setenv('HOMESERVER_JELLYFIN_COLLECTION_COVERS_INTERVAL_SECONDS', '600')
    monkeypatch.setenv('HOMESERVER_ADMIN_USERNAME', 'fixture-owner')
    monkeypatch.setenv('HOMESERVER_HTTP_TIMEOUT_SECONDS', '7')
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
    cycle = _build_cycle(tmp_path / 'control.sqlite')
    assert cycle.jellyfin_collections is not None
    assert cycle.jellyfin_collections.base_url == 'http://jellyfin:8096/jellyfin'
    assert cycle.jellyfin_collections.interval_seconds == 600
    assert cycle.jellyfin_collections.admin_username == 'fixture-owner'
    assert cycle.jellyfin_collections.client.timeout.read == 7
    monkeypatch.setenv('HOMESERVER_JELLYFIN_COLLECTION_COVERS_ENABLED', 'false')
    assert _build_cycle(tmp_path / 'control.sqlite').jellyfin_collections is None
