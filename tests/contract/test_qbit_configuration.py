import json
from urllib.parse import parse_qs

import httpx
import pytest

from homeserver_control.configuration.qbittorrent import (
    apply_qbit_settings,
    effective_qbit_preferences,
    reconcile_qbit_categories,
)


@pytest.mark.asyncio
async def test_qbit_authenticated_idempotent_apply_preserves_manual_preferences():
    state = {"manual": "preserved", "upnp": True, "web_ui_upnp": True}
    writes = []

    def handler(request):
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json={"radarr": {}, "sonarr": {}})
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=fixture; Path=/"})
        assert request.headers.get("cookie") == "SID=fixture"
        if request.url.path.endswith("/setPreferences"):
            change = json.loads(parse_qs(request.content.decode())["json"][0])
            state.update(change)
            writes.append(change)
            return httpx.Response(200)
        return httpx.Response(200, json=state)

    async with httpx.AsyncClient(
        base_url="http://qbittorrent:8080", transport=httpx.MockTransport(handler)
    ) as client:
        settings = {"HOMESERVER_QBIT_USERNAME": "admin", "HOMESERVER_QBIT_PASSWORD": "private"}
        assert await apply_qbit_settings(settings, client)
        assert not await apply_qbit_settings(settings, client)
    assert len(writes) == 1
    assert state["manual"] == "preserved"
    assert all(state[key] == value for key, value in effective_qbit_preferences({}).items())


@pytest.mark.asyncio
async def test_qbit_bad_auth_does_not_emit_credentials():
    async with httpx.AsyncClient(
        base_url="http://qbittorrent:8080",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="Fails.")),
    ) as client:
        with pytest.raises(RuntimeError, match="authentication rejected") as error:
            await apply_qbit_settings({"HOMESERVER_QBIT_PASSWORD": "private"}, client)
        assert "private" not in str(error.value)


@pytest.mark.asyncio
async def test_qbit_real_api_seeding_setters_require_enabled_flag_with_value():
    state = effective_qbit_preferences({"HOMESERVER_SEED_RATIO_LIMIT": "1"})
    writes = []

    def handler(request):
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json={"radarr": {}, "sonarr": {}})
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/setPreferences"):
            payload = json.loads(parse_qs(request.content.decode())["json"][0])
            writes.append(payload)
            if "max_ratio_enabled" in payload:
                state["max_ratio"] = payload["max_ratio"] if payload["max_ratio_enabled"] else -1
            state.update({key: value for key, value in payload.items() if key != "max_ratio"})
        return httpx.Response(200, json=state)

    async with httpx.AsyncClient(
        base_url="http://qbit", transport=httpx.MockTransport(handler)
    ) as client:
        env = {"HOMESERVER_SEED_RATIO_LIMIT": "2"}
        assert await apply_qbit_settings(env, client)
        assert not await apply_qbit_settings(env, client)
    assert writes == [{"max_ratio": 2.0, "max_ratio_enabled": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [True, False])
async def test_qbit_ambiguous_write_readback_detects_drift(lost_response):
    state = effective_qbit_preferences({}) | {"up_limit": 1}

    def handler(request):
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json={"radarr": {}, "sonarr": {}})
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, text="Ok.")
        if request.url.path.endswith("/setPreferences"):
            if lost_response:
                state.update(json.loads(parse_qs(request.content.decode())["json"][0]))
                raise httpx.ReadTimeout("sensitive transport text")
            return httpx.Response(200)
        return httpx.Response(200, json=state)

    async with httpx.AsyncClient(
        base_url="http://qbittorrent:8080", transport=httpx.MockTransport(handler)
    ) as client:
        if lost_response:
            assert await apply_qbit_settings({}, client)
            assert not await apply_qbit_settings({}, client)
        else:
            with pytest.raises(RuntimeError, match="read-back drift"):
                await apply_qbit_settings({}, client)


@pytest.mark.asyncio
async def test_qbit_categories_internal_bootstrap_readback_lost_response_preserves_paths():
    categories = {"radarr": {"name": "radarr", "savePath": "/manual/path"}}
    writes = []

    def handler(request):
        if request.url.path.endswith("/categories"):
            return httpx.Response(200, json=categories)
        assert request.url.path.endswith("/createCategory")
        form = parse_qs(request.content.decode(), keep_blank_values=True)
        name = form["category"][0]
        writes.append(name)
        categories[name] = {"name": name, "savePath": form["savePath"][0]}
        raise httpx.ReadError("response lost", request=request)

    async with httpx.AsyncClient(
        base_url="http://qbittorrent", transport=httpx.MockTransport(handler)
    ) as client:
        assert len(await reconcile_qbit_categories(client, "plan")) == 1
        assert not writes
        assert len(await reconcile_qbit_categories(client, "apply")) == 1
        assert not await reconcile_qbit_categories(client, "apply")
        assert not await reconcile_qbit_categories(client, "verify")
    assert writes == ["sonarr"]
    assert categories["radarr"]["savePath"] == "/manual/path"
