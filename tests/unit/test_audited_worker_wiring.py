from pathlib import Path
from types import SimpleNamespace

import pytest

from homeserver_common.settings import FIELDS
from homeserver_control.worker import __main__ as cli


def test_audited_manifest_is_explicitly_disabled_by_default():
    assert FIELDS['audited_import_manifest'].default == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('hybrid', [False, True])
async def test_import_helpers_receive_current_jellyfin_connection(monkeypatch, tmp_path, hybrid):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv('HOMESERVER_JELLYFIN_URL', 'http://jellyfin:8096/jellyfin')
    monkeypatch.setenv('HOMESERVER_JELLYFIN_API_KEY', 'fixture-jellyfin')
    monkeypatch.setenv('HOMESERVER_RADARR_URL', 'http://radarr')
    monkeypatch.setenv('HOMESERVER_RADARR_API_KEY', 'fixture-radarr')
    if hybrid:
        monkeypatch.setenv('HOMESERVER_AUDITED_IMPORT_ALLOW_SEERR', 'true')
        monkeypatch.setenv('HOMESERVER_SEERR_URL', 'http://seerr')
        monkeypatch.setenv('HOMESERVER_SEERR_API_KEY', 'fixture-seerr')
    monkeypatch.setattr(cli, 'load_storage_registry', lambda: SimpleNamespace(
        pools={'ssd': SimpleNamespace(filesystem_id='fixture')},
        primary_pool=SimpleNamespace(filesystem_id='fixture'),
        primary_pool_id='ssd',
        inspect=lambda _pool: (_ for _ in ()).throw(ValueError('fixture unavailable')),
    ))
    monkeypatch.setattr(cli, 'load_audited_manifest', lambda path, database: {'jobs': []})
    cycle = cli._build_cycle(tmp_path / 'control.sqlite')
    try:
        helpers = list(cycle.audited_imports.helpers.values())
        if hybrid:
            helpers += [cycle.finalizer, cycle.series_finalizer]
        assert len(helpers) == (4 if hybrid else 2)
        for helper in helpers:
            assert helper.jellyfin_url == 'http://jellyfin:8096/jellyfin'
            assert helper.jellyfin_api_key == 'fixture-jellyfin'
    finally:
        await cli._run_forever(cycle, should_run=lambda: False, interval=0)


def configure(monkeypatch, tmp_path):
    for key in list(cli.os.environ):
        if key.startswith('HOMESERVER_'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('HOMESERVER_AUDITED_IMPORT_MANIFEST', str(tmp_path / 'audited-imports.json'))
    monkeypatch.setenv('HOMESERVER_RECOVERY_MODE', str(tmp_path / 'RECOVERY_MODE'))
    monkeypatch.setenv('HOMESERVER_ARR_TOKEN', 'fixture')
    monkeypatch.setenv('HOMESERVER_SONARR_URL', 'http://sonarr')
    monkeypatch.setenv('HOMESERVER_SONARR_API_KEY', 'fixture')
    monkeypatch.setenv('HOMESERVER_CAPACITY_SNAPSHOT', str(tmp_path / 'capacity.json'))
    def inspect(_pool):
        raise ValueError('unused storage bootstrap probe')

    monkeypatch.setattr(cli, 'load_storage_registry', lambda: SimpleNamespace(inspect=inspect))


def test_audited_mode_builds_without_seerr_or_any_acquisition(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    # Loading is tested independently against symlinks/private mode and sealing.
    monkeypatch.setattr(cli, 'load_audited_manifest', lambda path, database: {'jobs': []})
    cycle = cli._build_cycle(tmp_path / 'control.sqlite')
    assert cycle is not None and cycle.audited_imports is not None
    assert cycle.acquirer is None and cycle.series_acquirer is None
    assert cycle.cancellation is None and cycle.source_reconciler is None
    assert cycle.deletion_coordinator is None
    assert cycle.audited_imports.helpers['sonarr'].subtitle_policy.providers == ('subdl',)


def test_invalid_audited_path_cannot_fall_through_to_normal_cycle(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv('HOMESERVER_SEERR_URL', 'http://seerr')
    monkeypatch.setenv('HOMESERVER_SEERR_API_KEY', 'fixture')

    def refused(_path: Path, _database: Path):
        raise ValueError('private manifest refused')

    monkeypatch.setattr(cli, 'load_audited_manifest', refused)
    with pytest.raises(ValueError):
        cli._build_cycle(tmp_path / 'control.sqlite')

@pytest.mark.asyncio
async def test_opt_in_hybrid_shares_registry_and_capacity_and_closes_audited_helpers(
    monkeypatch, tmp_path,
):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv('HOMESERVER_AUDITED_IMPORT_ALLOW_SEERR', 'true')
    monkeypatch.setenv('HOMESERVER_SEERR_URL', 'http://seerr')
    monkeypatch.setenv('HOMESERVER_SEERR_API_KEY', 'fixture')
    monkeypatch.setattr(cli, 'load_storage_registry', lambda: SimpleNamespace(
        pools={'ssd': SimpleNamespace(filesystem_id='fixture')},
        primary_pool=SimpleNamespace(filesystem_id='fixture'),
        primary_pool_id='ssd',
        inspect=lambda _pool: (_ for _ in ()).throw(ValueError('fixture unavailable')),
    ))
    monkeypatch.setattr(cli, 'load_audited_manifest', lambda path, database: {'jobs': []})
    cycle = cli._build_cycle(tmp_path / 'control.sqlite')
    try:
        assert cycle is not None and cycle.audited_allow_seerr
        assert cycle.audited_imports is not None and cycle.series_acquirer is not None
        assert cycle.audited_imports.permits is cycle.series_acquirer.permits
        assert cycle.audited_imports.capacity_provider is cycle.series_acquirer.capacity_provider
        assert cycle.audited_imports.helpers['sonarr'].permits is cycle.series_acquirer.permits
        assert cycle.audited_imports.helpers['sonarr'].subtitle_policy.providers == ('subdl',)
        assert cycle.cancellation is not None
        assert cycle.source_reconciler is None and cycle.discarded_sources is None
        assert cycle.manual_source_replacements is None and cycle.movie_prioritizer is None
        assert cycle.deletion_coordinator is None
    finally:
        await cli._run_forever(cycle, should_run=lambda: False, interval=0)
    assert cycle.source.client.is_closed
    assert cycle.audited_imports.helpers['sonarr'].client.is_closed


@pytest.mark.parametrize('flag', ['', 'false'])
def test_audited_default_ignores_seerr_credentials(monkeypatch, tmp_path, flag):
    configure(monkeypatch, tmp_path)
    monkeypatch.setenv('HOMESERVER_AUDITED_IMPORT_ALLOW_SEERR', flag)
    monkeypatch.setenv('HOMESERVER_SEERR_URL', 'http://seerr')
    monkeypatch.setenv('HOMESERVER_SEERR_API_KEY', 'fixture')
    monkeypatch.setattr(cli, 'load_audited_manifest', lambda path, database: {'jobs': []})
    cycle = cli._build_cycle(tmp_path / 'control.sqlite')
    assert isinstance(cycle.source, cli._AuditedOnly)
    assert cycle.acquirer is None and cycle.series_acquirer is None
