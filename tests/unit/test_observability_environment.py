"""Compose-only observability settings never become application credentials."""

import pytest

from homeserver_common.catalog import catalog
from homeserver_common.env import load_settings, serialize_env
from homeserver_common.runtime import apply_runtime_configuration
from homeserver_common.service_environment import ROLE_KEYS
from homeserver_common.settings import PRODUCTION_REQUIRED

OBSERVABILITY = {
    "HOMESERVER_GRAFANA_ADMIN_USER": "fixture-observability-user",
    "HOMESERVER_GRAFANA_ADMIN_PASSWORD": "fixture-observability-password",
    "HOMESERVER_OBSERVABILITY_DIR": "/fixture-observability-directory",
}


def write_preferences(tmp_path, **extra):
    preferences = tmp_path / ".env"
    values = {
        "HOMESERVER_" + name.upper(): "fixture-required-value"
        for name in PRODUCTION_REQUIRED
    }
    values.update(OBSERVABILITY)
    values.update(extra)
    preferences.write_text(serialize_env(values), encoding="utf-8")
    return preferences


@pytest.mark.parametrize("mode", ["dev", "prod"])
def test_observability_values_load_without_entering_settings_or_generated_environment(
    tmp_path, mode,
):
    preferences = write_preferences(tmp_path)

    settings = load_settings(preferences, mode=mode)

    assert settings.admin_password == "fixture-required-value"
    assert not set(OBSERVABILITY) & settings.as_environment().keys()
    assert not {key.removeprefix("HOMESERVER_").lower() for key in OBSERVABILITY} & (
        settings.values.keys()
    )
    for value in OBSERVABILITY.values():
        assert value not in str(settings.redacted_dict())
        assert value not in str(settings.as_environment())
    assert not set(OBSERVABILITY) & {entry["key"] for entry in catalog()}


@pytest.mark.parametrize("role", sorted(ROLE_KEYS))
def test_observability_credentials_are_removed_from_every_service_environment(tmp_path, role):
    preferences = write_preferences(tmp_path)

    environment = apply_runtime_configuration(preferences, "prod", {
        **OBSERVABILITY,
        "HOMESERVER_SERVICE_ROLE": role,
        "PATH": "/fixture/bin",
    })

    assert environment["PATH"] == "/fixture/bin"
    assert not set(OBSERVABILITY) & environment.keys()
    assert all(value not in environment.values() for value in OBSERVABILITY.values())


@pytest.mark.parametrize("unknown", [
    "HOMESERVER_GRAFANA_ADMIN_PASSWORD_FILE",
    "HOMESERVER_GRAFANA_UNKNOWN_SETTING",
    "HOMESERVER_OBSERVABILITY_OTHER_DIR",
    "HOMESERVER_UNREVIEWED_KEY",
])
def test_compose_only_allowlist_does_not_accept_other_managed_names(tmp_path, unknown):
    preferences = write_preferences(tmp_path, **{unknown: "fixture-secret-must-not-appear"})

    with pytest.raises(ValueError, match=f"{unknown}: unknown managed key") as error:
        load_settings(preferences, mode="prod")

    assert "fixture-secret-must-not-appear" not in str(error.value)
