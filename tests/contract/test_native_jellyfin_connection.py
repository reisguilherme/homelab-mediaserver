import json
from copy import deepcopy

import httpx
import pytest

from homeserver_control.configuration.arr import reconcile_arr
from homeserver_control.configuration.native_config import NativeConfigurationError


class NativeMovies:
    def __init__(self):
        self.schema = [{
            "implementation": "MediaBrowser", "configContract": "MediaBrowserSettings",
            "supportsOnDownload": True, "supportsOnUpgrade": True, "supportsOnRename": True,
            "onGrab": True, "onDownload": False, "onUpgrade": False, "onRename": False,
            "onHealthIssue": True, "supportsOnImportComplete": True,
            "onImportComplete": True, "tags": [],
            "fields": [{"name": name, "value": None} for name in (
                "host", "port", "useSsl", "urlBase", "apiKey", "notify",
                "updateLibrary", "mapFrom", "mapTo",
            )],
        }]
        self.rows = []
        self.writes = []
        self.ignore_write = False

    def handle(self, request):
        path = request.url.path
        if path == "/api/v3/notification/schema":
            return httpx.Response(200, json=self.schema)
        assert path.startswith("/api/v3/notification")
        if request.method in {"POST", "PUT"}:
            payload = json.loads(request.content)
            self.writes.append(payload)
            if not self.ignore_write:
                self.rows = [payload | {"id": 42}]
        return httpx.Response(200, json=self.rows)


def settings():
    return {
        "HOMESERVER_MOVIE_ROOT": "", "HOMESERVER_SERIES_ROOT": "",
        "HOMESERVER_JELLYFIN_URL": "https://jellyfin:8920/base",
        "HOMESERVER_JELLYFIN_API_KEY": "fixture-private-key",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_import_connection_created_once_with_only_library_events(service):
    native = NativeMovies()
    async with httpx.AsyncClient(
        base_url=f"http://{service}", transport=httpx.MockTransport(native.handle)
    ) as client:
        first = await reconcile_arr(service, settings(), client, "apply")
        second = await reconcile_arr(service, settings(), client, "apply")
        verified = await reconcile_arr(service, settings(), client, "verify")
    assert first.changes
    assert not second.changes and not verified.changes
    assert len(native.writes) == 1
    row = native.rows[0]
    assert row["implementation"] == "MediaBrowser"
    assert row["onDownload"] and row["onUpgrade"] and row["onRename"]
    assert not row["onGrab"] and not row["onHealthIssue"]
    assert not row["onImportComplete"]
    assert row["tags"] == []
    assert {f["name"]: f["value"] for f in row["fields"]} == {
        "host": "service-proxy", "port": 80, "useSsl": False, "urlBase": "/jellyfin",
        "apiKey": "fixture-private-key", "notify": False, "updateLibrary": True,
        "mapFrom": "", "mapTo": "",
    }
    assert "fixture-private-key" not in repr(first.changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_connection_uses_private_proxy_even_when_controller_has_direct_native_url(service):
    native = NativeMovies()
    env = settings() | {"HOMESERVER_JELLYFIN_URL": "http://jellyfin:8096/jellyfin"}
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        await reconcile_arr(service, env, client, "apply")
    fields = {field["name"]: field["value"] for field in native.rows[0]["fields"]}
    assert {key: fields[key] for key in ("host", "port", "useSsl", "urlBase")} == {
        "host": "service-proxy", "port": 80, "useSsl": False, "urlBase": "/jellyfin",
    }
    assert env["HOMESERVER_JELLYFIN_URL"] == "http://jellyfin:8096/jellyfin"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["plan", "verify"])
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_connection_plan_and_verify_do_not_write(mode, service):
    native = NativeMovies()
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        result = await reconcile_arr(service, settings(), client, mode)
    assert result.status == ("planned" if mode == "plan" else "drift")
    assert result.changes and not native.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_existing_managed_connection_is_repaired_without_duplicate(service):
    native = NativeMovies()
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        await reconcile_arr(service, settings(), client, "apply")
        assert native.rows
        native.rows[0]["onDownload"] = False
        native.rows[0]["fields"].append({"name": "futureOption", "value": "preserved"})
        await reconcile_arr(service, settings(), client, "apply")
    assert len(native.rows) == 1 and native.rows[0]["id"] == 42
    assert native.rows[0]["onDownload"]
    assert {"name": "futureOption", "value": "preserved"} in native.rows[0]["fields"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["implementation", "updateLibrary", "supportsOnDownload"])
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_incompatible_notification_schema_is_reported_without_writes(missing, service):
    native = NativeMovies()
    if missing == "updateLibrary":
        native.schema[0]["fields"] = [f for f in native.schema[0]["fields"] if f["name"] != missing]
    else:
        native.schema[0].pop(missing)
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        result = await reconcile_arr(service, settings(), client, "apply")
    assert result.status == "unsupported"
    assert not native.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_ignored_connection_write_fails_readback(service):
    native = NativeMovies()
    native.ignore_write = True
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="read-back"):
            await reconcile_arr(service, settings(), client, "apply")


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_no_connector_without_jellyfin_credentials(service):
    def unexpected(request):
        raise AssertionError("unexpected native request")
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(unexpected)
    ) as client:
        env = deepcopy(settings())
        env.pop("HOMESERVER_JELLYFIN_API_KEY")
        assert not (await reconcile_arr(service, env, client, "apply")).changes
