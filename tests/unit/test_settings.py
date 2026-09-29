import pytest

from homeserver_common.env import load_settings, parse_env, serialize_env


def test_literal_roundtrip():
    values = {"HOMESERVER_QBIT_PASSWORD": "a $HOME # ' \" spaces"}
    assert parse_env(serialize_env(values)) == values


def test_empty_bazarr_providers_explicitly_disables_network_providers(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_BAZARR_PROVIDERS=\n")
    settings = load_settings(path)
    assert settings.bazarr_providers == ()
    assert settings.as_environment()["HOMESERVER_BAZARR_PROVIDERS"] == ""


def test_optional_jellyfin_deletion_preserves_unset_and_rejects_non_boolean(tmp_path):
    path = tmp_path / ".env"
    path.write_text("")
    assert load_settings(path).jellyfin_enable_media_deletion == ""
    path.write_text("HOMESERVER_JELLYFIN_ENABLE_MEDIA_DELETION=true\n")
    assert load_settings(path).jellyfin_enable_media_deletion == "true"
    path.write_text("HOMESERVER_JELLYFIN_ENABLE_MEDIA_DELETION=maybe\n")
    with pytest.raises(ValueError):
        load_settings(path)


def test_invalid_and_unknown_keys(tmp_path):
    path = tmp_path / ".env"
    for key, value in [("DOWNLOAD_MAX_ACTIVE", "zero"), ("TYPO", "secret")]:
        path.write_text(f"HOMESERVER_{key}={value}\n")
        with pytest.raises(ValueError, match=f"HOMESERVER_{key}") as exc:
            load_settings(path, mode="dev")
        assert value not in str(exc.value)


def test_effective_environment_and_redaction(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_UPLOAD_LIMIT_MBIT=20\nHOMESERVER_QBIT_PASSWORD=private\n")
    monkeypatch.setenv("HOMESERVER_UPLOAD_LIMIT_MBIT", "999")
    settings = load_settings(path, mode="dev")
    assert settings.upload_limit_bytes == 2500000
    assert settings.lan_bind_ip == "127.0.0.1"
    assert settings.redacted_dict()["qbit_password"] == "[redacted]"
    assert settings.as_environment()["HOMESERVER_UPLOAD_LIMIT_MBIT"] == "20"


def test_credentials_are_required_in_production(tmp_path):
    path = tmp_path / ".env"
    path.write_text("")
    with pytest.raises(ValueError, match="HOMESERVER_ARR_TOKEN"):
        load_settings(path, mode="prod")


def test_credential_exact_value_and_repr(tmp_path):
    secret = " $HOME # literal ' \" "
    path = tmp_path / ".env"
    path.write_text(serialize_env({"HOMESERVER_QBIT_PASSWORD": secret}))
    settings = load_settings(path, mode="dev")
    assert settings.qbit_password == secret
    assert secret not in repr(settings)


@pytest.mark.parametrize(
    "line,key",
    [
        ("HOMESERVER_CONTROL_PORT=8096", "PORT"),
        ("HOMESERVER_METRICS_MAX_AGE_SECONDS=1", "METRICS_MAX_AGE_SECONDS"),
        ("HOMESERVER_SOURCE_MIN_TIME_GAIN_PERCENT=100", "SOURCE_MIN_TIME_GAIN_PERCENT"),
        ("HOMESERVER_LAN_BIND_IP=0.0.0.0", "LAN_BIND_IP"),
        ("HOMESERVER_SUBTITLE_LANGUAGES=pt-PT", "SUBTITLE_LANGUAGES"),
        ("HOMESERVER_DOWNLOAD_LIMIT_MBIT=NaN", "DOWNLOAD_LIMIT_MBIT"),
        ("HOMESERVER_AUTOMATIC_UPGRADES=maybe", "AUTOMATIC_UPGRADES"),
    ],
)
def test_cross_field_validation(tmp_path, line, key):
    path = tmp_path / ".env"
    path.write_text(line)
    with pytest.raises(ValueError, match=key):
        load_settings(path, mode="dev")


def test_explicit_legacy_aliases_and_generated_runtime_paths(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "HOMESERVER_ARR_UID=1234\nHOMESERVER_DB_PATH=/private/old.sqlite\n"
    )
    settings = load_settings(path, mode="dev")
    assert settings.service_uid == 1234
    assert settings.as_environment()["HOMESERVER_DB_PATH"] == "/var/lib/homeserver/control.sqlite"


def test_catalog_matches_generated_example():
    from homeserver_common.catalog import catalog, example_env
    from homeserver_common.settings import FIELDS

    rows = catalog()
    assert len(rows) == sum(field.editable for field in FIELDS.values())
    assert set(parse_env(example_env())) == {row["key"] for row in rows}
    assert all(row["consumer"] and row["application"] for row in rows)


def test_exact_adopted_bandwidth(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_UPLOAD_LIMIT_BYTES=2499999\n")
    assert load_settings(path, mode="dev").upload_limit_bytes == 2499999


def test_search_timeout_has_a_positive_canonical_default(tmp_path):
    path = tmp_path / ".env"
    path.write_text("")
    assert load_settings(path).search_timeout_seconds == 90
    path.write_text("HOMESERVER_SEARCH_TIMEOUT_SECONDS=0\n")
    with pytest.raises(ValueError, match="HOMESERVER_SEARCH_TIMEOUT_SECONDS"):
        load_settings(path)


def test_download_preferences_default_to_ten_downloads_and_unlimited_seeding(tmp_path):
    path = tmp_path / ".env"
    path.write_text("")
    settings = load_settings(path)
    assert settings.download_max_active == settings.series_download_window == 10
    assert settings.seed_max_active == settings.torrent_max_active == -1
    assert settings.queue_ignore_slow_torrents is False
    assert settings.source_slow_replacement_enabled is False
    assert settings.release_indexer_priority == ("uindex", "1337x")
    assert settings.indexer_fallback_min_seeders == 5
    assert settings.movie_resolutions == ("2160", "1080")
    assert settings.series_resolutions == ("1080",)


@pytest.mark.parametrize("key", ["SEED_MAX_ACTIVE", "TORRENT_MAX_ACTIVE"])
@pytest.mark.parametrize("value,accepted", [("-1", True), ("12", True), ("0", False),
                                           ("-2", False)])
def test_active_seed_and_total_limits_accept_unlimited_or_positive(tmp_path, key, value, accepted):
    path = tmp_path / ".env"
    path.write_text(f"HOMESERVER_{key}={value}\n")
    if accepted:
        assert getattr(load_settings(path), key.lower()) == int(value)
    else:
        with pytest.raises(ValueError, match=key):
            load_settings(path)


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_worker_cycle_timeout_is_finite_and_positive(tmp_path, value):
    path = tmp_path / ".env"
    path.write_text("")
    assert load_settings(path).worker_cycle_timeout_seconds == 900
    path.write_text(f"HOMESERVER_WORKER_CYCLE_TIMEOUT_SECONDS={value}\n")
    with pytest.raises(ValueError, match="HOMESERVER_WORKER_CYCLE_TIMEOUT_SECONDS"):
        load_settings(path)
