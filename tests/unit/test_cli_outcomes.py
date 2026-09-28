import pytest

from homeserver_common import cli


@pytest.mark.parametrize(
    "value", [[], {}, [{"service": "qbit", "status": "unknown", "changes": []}]]
)
def test_invalid_operator_outcome_is_failure(value):
    with pytest.raises(RuntimeError):
        cli._validate_outcomes(value)


def test_secret_changes_are_redacted():
    outcomes = [
        {
            "service": "qbittorrent",
            "status": "verified",
            "changes": [
                {
                    "service": "qbittorrent",
                    "key": "password",
                    "secret": True,
                    "restart_required": False,
                    "before": "private-before",
                    "after": "private-after",
                }
            ],
        }
    ]
    outcomes += [
        {"service": name, "status": "verified", "changes": []}
        for name in ("sonarr", "radarr", "prowlarr", "bazarr", "jellyfin", "seerr")
    ]
    result = cli._validate_outcomes(outcomes)
    assert "private" not in repr(result)


def test_verified_subset_cannot_claim_global_success():
    with pytest.raises(RuntimeError):
        cli._validate_outcomes([{"service": "qbittorrent", "status": "verified", "changes": []}])


def test_development_apply_adopts_discovered_credentials_and_reapplies_once(tmp_path, monkeypatch):
    from homeserver_common.env import load_settings, parse_env
    from homeserver_control.configuration import reconcile
    from homeserver_control.configuration.native_config import ServiceOutcome

    path = tmp_path / ".env"
    path.write_text(f"HOMESERVER_RUN_ROOT={tmp_path / 'run'}\n")
    observed = []

    async def apply(settings):
        observed.append(settings.jellyfin_api_key)
        return [
            ServiceOutcome(name, "verified")
            for name in (
                "qbittorrent",
                "sonarr",
                "radarr",
                "prowlarr",
                "bazarr",
                "jellyfin",
                "seerr",
            )
        ]

    async def discover(settings):
        return (
            {}
            if settings.jellyfin_api_key == "discovered-fixture"
            else {"HOMESERVER_JELLYFIN_API_KEY": "discovered-fixture"}
        )

    monkeypatch.setattr(reconcile, "apply_native_settings", apply)
    monkeypatch.setattr(reconcile, "discover_native_credentials", discover)
    outcomes = cli.native_configuration(load_settings(path), "apply", env_file=path)
    assert len(outcomes) == 7
    assert observed == ["", "discovered-fixture"]
    assert parse_env(path.read_text())["HOMESERVER_JELLYFIN_API_KEY"] == "discovered-fixture"
    assert not (tmp_path / "run/native-credentials.json").exists()


def test_apply_drift_is_not_success(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("")
    monkeypatch.setattr(
        cli,
        "native_configuration",
        lambda *args, **kwargs: [{"service": "qbit", "status": "drift", "changes": []}],
    )
    assert cli.main(["config", "apply", "--mode", "dev", "--env-file", str(path)]) == 4


@pytest.mark.parametrize("status", ["verified", "drift"])
def test_host_apply_keeps_maintenance_across_native_and_refresh(tmp_path, monkeypatch, status):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from homeserver_common import backup, release

    events = []
    marker = tmp_path / "maintenance"

    @contextmanager
    def lock(settings):
        events.append("lock")
        yield
        events.append("unlock")

    @contextmanager
    def maintenance(settings, reason, *, keep_on_error, stop_stack):
        assert keep_on_error and not stop_stack
        marker.write_text(reason)
        try:
            yield
        except RuntimeError:
            marker.write_text(reason + "_failed")
            raise
        else:
            marker.unlink()

    def native(*args, **kwargs):
        assert marker.exists()
        events.append("native")
        return [{"service": "qbit", "status": status, "changes": []}]

    def refresh(*args, **kwargs):
        assert marker.exists()
        assert kwargs == {"_already_locked": True, "_already_maintenance": True}
        events.append("refresh")

    monkeypatch.setattr(backup, "operation_lock", lock)
    monkeypatch.setattr(backup, "maintenance", maintenance)
    monkeypatch.setattr(cli, "native_configuration", native)
    monkeypatch.setattr(cli, "load_settings", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(release, "refresh_runtime_configuration", refresh)
    assert cli.apply_host_configuration(SimpleNamespace(), tmp_path / ".env")[0]["status"] == status
    assert events == ["lock", "native"] + (["refresh"] if status == "verified" else []) + ["unlock"]
    assert marker.exists() == (status != "verified")
