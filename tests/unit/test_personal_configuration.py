import pytest

from homeserver_common.catalog import catalog, example_env
from homeserver_common.env import load_settings, parse_env, serialize_env


def test_personal_example_contains_behavior_and_credentials_without_host_wiring():
    values = parse_env(example_env())
    for key in (
        "DOWNLOAD_MAX_ACTIVE",
        "SERIES_DOWNLOAD_WINDOW",
        "SEED_MAX_ACTIVE",
        "MEDIA_RESOLUTIONS",
        "SUBTITLE_LANGUAGES",
        "SOURCE_SLOW_WINDOW_SECONDS",
        "ADMIN_PASSWORD",
        "SONARR_API_KEY",
        "SUBDL_API_KEY",
    ):
        assert "HOMESERVER_" + key in values
    for key in (
        "ENVIRONMENT",
        "MEDIA_ROOT",
        "APPDATA_ROOT",
        "MEDIA_UUID",
        "LAN_BIND_IP",
        "TAILSCALE_BIND_IP",
        "JELLYFIN_PORT",
        "SONARR_URL",
        "SERVICE_UID",
        "INTEL_RENDER_DEVICE",
        "BACKUP_ENABLED",
        "RELEASE_KEEP_COUNT",
    ):
        assert "HOMESERVER_" + key not in values
    assert {row["key"] for row in catalog()} == set(values)


def test_production_settings_need_no_machine_identity_or_first_use_library_keys(tmp_path):
    path = tmp_path / ".env"
    values = {
        "HOMESERVER_" + key: "private-value"
        for key in (
            "ARR_TOKEN",
            "ADMIN_TOKEN",
            "CSRF_TOKEN",
            "ADMIN_PASSWORD",
            "QBIT_PASSWORD",
            "SONARR_API_KEY",
            "RADARR_API_KEY",
            "PROWLARR_API_KEY",
            "BAZARR_API_KEY",
        )
    }
    path.write_text(serialize_env(values))
    settings = load_settings(path, mode="prod")
    assert settings.media_root == "/srv/data"
    assert settings.run_root == "/run/homeserver"
    assert settings.sonarr_url == "http://sonarr:8989"
    assert settings.jellyfin_api_key == settings.seerr_api_key == ""


def test_series_download_window_is_positive_and_editable(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_SERIES_DOWNLOAD_WINDOW=3\n")
    assert load_settings(path).series_download_window == 3
    path.write_text("HOMESERVER_SERIES_DOWNLOAD_WINDOW=0\n")
    with pytest.raises(ValueError, match="HOMESERVER_SERIES_DOWNLOAD_WINDOW"):
        load_settings(path)


def test_credentials_are_read_from_env_without_secret_file_support(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_QBIT_PASSWORD_FILE=password\n")
    (tmp_path / "password").write_text("a private password")
    with pytest.raises(ValueError, match="HOMESERVER_QBIT_PASSWORD_FILE"):
        load_settings(path)


@pytest.mark.parametrize(
    "key,value",
    [
        ("BACKUP_ENABLED", "true"),
        ("BACKUP_PASSWORD", "private-value"),
        ("RELEASE_KEEP_COUNT", "3"),
        ("INSTALL_ROOT", "/opt/homeserver"),
        ("SUPERVISOR_MAX_RESTARTS", "5"),
    ],
)
def test_removed_service_management_options_are_rejected(tmp_path, key, value):
    path = tmp_path / ".env"
    path.write_text(f"HOMESERVER_{key}={value}\n")
    with pytest.raises(ValueError, match="HOMESERVER_" + key + ": unknown managed key"):
        load_settings(path)
