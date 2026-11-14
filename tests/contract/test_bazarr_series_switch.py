"""Series-only provider isolation without changing movie subtitle configuration."""

import copy
from urllib.parse import parse_qs

import httpx
import pytest

from homeserver_common.env import load_settings
from homeserver_control.configuration.bazarr import reconcile_bazarr
from homeserver_control.configuration.native_config import NativeConfigurationError


@pytest.fixture
def native_bazarr():
    profiles = [
        {
            "profileId": 88,
            "name": "HomeServer",
            "cutoff": 1,
            "items": [
                {
                    "id": index,
                    "language": lang,
                    "forced": "False",
                    "hi": "False",
                    "audio_exclude": "False",
                    "audio_only_include": "False",
                }
                for index, lang in enumerate(("pb", "en"), 1)
            ],
            "mustContain": [],
            "mustNotContain": [],
            "originalFormat": None,
        },
        {"profileId": 44, "name": "Manual", "items": [], "cutoff": None},
    ]
    state = {
        "general": {
            "enabled_providers": ["subdl", "opensubtitlescom"],
            "use_sonarr": True,
            "use_radarr": True,
            "serie_default_enabled": True,
            "movie_default_enabled": True,
            "serie_default_profile": 88,
            "movie_default_profile": 88,
            "wanted_search_frequency": 6,
            "wanted_search_frequency_movie": 6,
            "upgrade_subs": True,
            "upgrade_frequency": 12,
            "adaptive_searching": True,
            "adaptive_searching_delay": "3w",
            "adaptive_searching_delta": "1w",
        },
        "sonarr": {
            "ip": "sonarr",
            "port": 8989,
            "ssl": False,
            "base_url": "/sonarr",
            "apikey": "fixture-sonarr",
        },
        "radarr": {
            "ip": "radarr",
            "port": 7878,
            "ssl": False,
            "base_url": "/radarr",
            "apikey": "fixture-radarr",
        },
        "subdl": {"api_key": "fixture-subdl"},
        "opensubtitlescom": {"username": "fixture-opensub-user", "password": "fixture-pass"},
    }
    env = {
        "HOMESERVER_BAZARR_PROVIDERS": "subdl,opensubtitlescom",
        "HOMESERVER_SONARR_URL": "http://sonarr:8989/sonarr",
        "HOMESERVER_SONARR_API_KEY": "fixture-sonarr",
        "HOMESERVER_RADARR_URL": "http://radarr:7878/radarr",
        "HOMESERVER_RADARR_API_KEY": "fixture-radarr",
        "HOMESERVER_SUBTITLE_LANGUAGES": "pt-BR,en-US",
        "HOMESERVER_SUBDL_API_KEY": "fixture-subdl",
        "HOMESERVER_OPENSUBTITLES_USERNAME": "fixture-opensub-user",
        "HOMESERVER_OPENSUBTITLES_PASSWORD": "fixture-pass",
    }
    writes = []

    def handler(request):
        assert request.url.path in {
            "/api/system/settings",
            "/api/system/languages/profiles",
            "/api/system/languages",
        }, "No subtitle provider/search/download route is allowed"
        if request.url.path.endswith("/profiles"):
            assert request.method == "GET"
            return httpx.Response(200, json=profiles)
        if request.url.path.endswith("/languages"):
            assert request.method == "GET"
            return httpx.Response(
                200, json=[{"code2": "pb", "enabled": True}, {"code2": "en", "enabled": True}]
            )
        if request.method == "POST":
            body = parse_qs(request.content.decode(), keep_blank_values=True)
            writes.append(body)
            for key, values in body.items():
                section, field = key.removeprefix("settings-").split("-", 1)
                assert isinstance(state[section][field], bool)
                state[section][field] = values[0] == "true"
            return httpx.Response(204)
        assert request.method == "GET"
        return httpx.Response(200, json=state)

    return state, profiles, env, writes, handler


@pytest.mark.asyncio
async def test_series_disabled_despite_sonarr_credentials_preserves_movies(native_bazarr):
    state, profiles, env, writes, handler = native_bazarr
    before, original_profiles = copy.deepcopy(state), copy.deepcopy(profiles)
    env["HOMESERVER_BAZARR_USE_SONARR"] = "false"
    async with httpx.AsyncClient(
        base_url="http://bazarr", transport=httpx.MockTransport(handler)
    ) as client:
        assert (await reconcile_bazarr(env, client, "plan")).status == "planned"
        assert writes == []
        assert state == before
        assert (await reconcile_bazarr(env, client, "verify")).status == "drift"
        assert writes == []
        assert (await reconcile_bazarr(env, client, "apply")).status == "verified"
        assert (await reconcile_bazarr(env, client, "verify")).status == "verified"
        second = await reconcile_bazarr(env, client, "apply")
    assert not second.changes
    assert writes == [
        {
            "settings-general-use_sonarr": ["false"],
            "settings-general-serie_default_enabled": ["false"],
        }
    ]
    before["general"].update(use_sonarr=False, serie_default_enabled=False)
    assert state == before
    assert profiles == original_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "true"])
async def test_legacy_default_and_explicit_enable_keep_both_integrations(native_bazarr, flag):
    state, _, env, writes, handler = native_bazarr
    before = copy.deepcopy(state)
    if flag is not None:
        env["HOMESERVER_BAZARR_USE_SONARR"] = flag
    async with httpx.AsyncClient(
        base_url="http://bazarr", transport=httpx.MockTransport(handler)
    ) as client:
        result = await reconcile_bazarr(env, client, "apply")
    assert result.status == "verified"
    assert not result.changes
    assert writes == []
    assert state == before


@pytest.mark.asyncio
async def test_explicit_disable_works_without_sonarr_key_or_language_management(native_bazarr):
    state, _, env, writes, handler = native_bazarr
    env.pop("HOMESERVER_SONARR_API_KEY")
    env.pop("HOMESERVER_SUBTITLE_LANGUAGES")
    env["HOMESERVER_BAZARR_USE_SONARR"] = "false"
    before = copy.deepcopy(state)
    async with httpx.AsyncClient(
        base_url="http://bazarr", transport=httpx.MockTransport(handler)
    ) as client:
        result = await reconcile_bazarr(env, client, "apply")
    assert result.status == "verified"
    assert state["general"]["use_sonarr"] is False
    assert state["general"]["serie_default_enabled"] is False
    assert state["sonarr"] == before["sonarr"]
    assert state["radarr"] == before["radarr"]
    assert len(writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["use_sonarr", "serie_default_enabled"])
async def test_unsupported_disable_capability_is_not_partially_applied(native_bazarr, missing):
    state, _, env, writes, handler = native_bazarr
    state["general"].pop(missing)
    env["HOMESERVER_BAZARR_USE_SONARR"] = "false"
    async with httpx.AsyncClient(
        base_url="http://bazarr", transport=httpx.MockTransport(handler)
    ) as client:
        result = await reconcile_bazarr(env, client, "apply")
    assert result.status == "unsupported"
    assert writes == []


@pytest.mark.asyncio
async def test_disable_requires_successful_native_readback(native_bazarr):
    state, _, env, _, handler = native_bazarr
    env["HOMESERVER_BAZARR_USE_SONARR"] = "false"

    def ignores_write(request):
        if request.method == "POST":
            return httpx.Response(204)
        return handler(request)

    async with httpx.AsyncClient(
        base_url="http://bazarr", transport=httpx.MockTransport(ignores_write)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="Bazarr read-back drift"):
            await reconcile_bazarr(env, client, "apply")
    assert state["general"]["use_sonarr"] is True


@pytest.mark.parametrize("configured, expected", [("", True), ("true", True), ("false", False)])
def test_managed_switch_reaches_native_environment(tmp_path, configured, expected):
    path = tmp_path / ".env"
    path.write_text("" if not configured else f"HOMESERVER_BAZARR_USE_SONARR={configured}\n")
    settings = load_settings(path)
    assert settings.bazarr_use_sonarr is expected
    assert settings.as_environment()["HOMESERVER_BAZARR_USE_SONARR"] == str(expected).lower()


def test_invalid_managed_switch_is_rejected_without_value_leak(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_BAZARR_USE_SONARR=invalid-private-value\n")
    with pytest.raises(ValueError, match="HOMESERVER_BAZARR_USE_SONARR") as error:
        load_settings(path)
    assert "invalid-private-value" not in str(error.value)
