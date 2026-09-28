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


def test_container_apply_adopts_credentials_directly_in_the_env(tmp_path, monkeypatch):
    from homeserver_common.env import load_settings, parse_env
    from homeserver_control.configuration import reconcile
    from homeserver_control.configuration.native_config import ServiceOutcome

    path = tmp_path / ".env"
    path.write_text("")
    observed = []

    async def apply(settings):
        assert settings.appdata_root == "/srv/appdata"
        assert settings.media_root == "/data"
        assert settings.run_root == "/run/homeserver"
        observed.append(settings.seerr_api_key)
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
        return {} if settings.seerr_api_key else {"HOMESERVER_SEERR_API_KEY": "discovered-fixture"}

    monkeypatch.setattr(reconcile, "apply_native_settings", apply)
    monkeypatch.setattr(reconcile, "discover_native_credentials", discover)
    result = cli.native_configuration(
        load_settings(path), "apply", in_container=True, env_file=path
    )
    assert all(row["status"] == "verified" for row in result)
    assert observed == ["", "discovered-fixture"]
    assert parse_env(path.read_text())["HOMESERVER_SEERR_API_KEY"] == "discovered-fixture"
    assert not (tmp_path / "native-credentials.json").exists()


def test_env_init_contains_only_editable_catalog_entries(tmp_path):
    from homeserver_common.env import parse_env

    path = tmp_path / ".env"
    cli.initialize_env(path, "dev")
    assert set(parse_env(path.read_text())) == set(parse_env(cli.example_env()))


def test_env_init_generates_native_hex_api_keys_and_leaves_discoverable_keys_empty(tmp_path):
    import re

    from homeserver_common.env import parse_env

    path = tmp_path / ".env"
    cli.initialize_env(path, "prod")
    values = parse_env(path.read_text())
    for name in ("SONARR", "RADARR", "PROWLARR", "BAZARR"):
        assert re.fullmatch(r"[0-9a-f]{32}", values[f"HOMESERVER_{name}_API_KEY"])
    assert values["HOMESERVER_JELLYFIN_API_KEY"] == ""
    assert values["HOMESERVER_SEERR_API_KEY"] == ""


def test_env_credential_update_can_write_a_docker_bind_mounted_file(tmp_path, monkeypatch):
    import errno

    path = tmp_path / ".env"
    path.write_text("old\n")
    path.chmod(0o600)

    def busy(*args):
        raise OSError(errno.EBUSY, "bind mount cannot be replaced")

    monkeypatch.setattr(cli.os, "replace", busy)
    cli._private_write(path, "new credential\n")
    assert path.read_text() == "new credential\n"
    assert path.stat().st_mode & 0o777 == 0o600


def test_env_credential_update_preserves_file_ownership_and_mode(tmp_path):
    path = tmp_path / ".env"
    path.write_text("old\n")
    path.chmod(0o640)
    original = path.stat()
    cli._private_write(path, "new\n")
    updated = path.stat()
    assert (updated.st_uid, updated.st_gid, updated.st_mode & 0o777) == (
        original.st_uid,
        original.st_gid,
        0o640,
    )
