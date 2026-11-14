import json
from xml.etree import ElementTree

import httpx
import pytest

from homeserver_common.env import load_settings
from homeserver_common.host import _native_seeds
from homeserver_common.settings import Settings


def test_default_native_addresses_and_fresh_seeds_use_service_paths(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            f"HOMESERVER_{name}_API_KEY=fixture"
            for name in ("SONARR", "RADARR", "PROWLARR", "BAZARR")
        )
    )
    settings = Settings(
        dict(load_settings(path, mode="dev").values) | {"appdata_root": str(tmp_path)}
    )
    for service in ("sonarr", "radarr", "prowlarr", "bazarr", "jellyfin"):
        assert getattr(settings, service + "_url").endswith("/" + service)
    seeds = _native_seeds(settings)
    for service in ("sonarr", "radarr", "prowlarr"):
        config = ElementTree.fromstring(seeds[tmp_path / service / "config.xml"])
        assert config.findtext("UrlBase") == "/" + service
    assert (
        json.loads(seeds[tmp_path / "bazarr/config/config.yaml"])["general"]["base_url"]
        == "/bazarr"
    )
    network = ElementTree.fromstring(seeds[tmp_path / "jellyfin/config/network.xml"])
    assert network.findtext("BaseUrl") == "/jellyfin"


@pytest.mark.asyncio
async def test_native_path_update_preserves_unrelated_fields_and_marks_restart():
    from homeserver_control.configuration.service_paths import reconcile_service_path

    current = {"id": 1, "urlBase": "", "port": 7878, "username": "owner"}
    writes = []

    def handler(request):
        assert request.url.path == "/api/v3/config/host"
        if request.method == "PUT":
            writes.append(json.loads(request.content))
            current.update(writes[-1])
        return httpx.Response(200, json=current)

    async with httpx.AsyncClient(
        base_url="http://radarr:7878", transport=httpx.MockTransport(handler)
    ) as client:
        env = {"HOMESERVER_RADARR_URL": "http://radarr:7878/radarr"}
        planned = await reconcile_service_path("radarr", env, client, "plan")
        assert planned and planned[0].restart_required
        assert not writes
        changed = await reconcile_service_path("radarr", env, client, "apply")
        assert changed and writes == [
            {"id": 1, "urlBase": "/radarr", "port": 7878, "username": "owner"}
        ]
        assert not await reconcile_service_path("radarr", env, client, "verify")


@pytest.mark.asyncio
async def test_base_path_probe_falls_back_only_on_missing_endpoint():
    from homeserver_control.configuration.service_paths import select_native_base_url

    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.startswith("/radarr/"):
            return httpx.Response(404)
        return httpx.Response(200, json={"appName": "Radarr"})

    async with httpx.AsyncClient(
        base_url="http://radarr:7878/radarr", transport=httpx.MockTransport(handler)
    ) as client:
        await select_native_base_url("radarr", client)
        assert str(client.base_url) == "http://radarr:7878/"
    assert paths == ["/radarr/api/v3/system/status", "/api/v3/system/status"]


@pytest.mark.asyncio
async def test_uindex_categories_include_misclassified_movies_without_enabling_other_indexers():
    from homeserver_control.configuration.arr import reconcile_indexer_filters

    rows = [
        {
            "id": 6,
            "name": "UIndex (Prowlarr)",
            "enableInteractiveSearch": True,
            "enableRss": False,
            "enableAutomaticSearch": False,
            "fields": [
                {"name": "categories", "value": [2000]},
                {"name": "minimumSeeders", "value": 5},
            ],
        }
    ]

    def handler(request):
        if request.method == "PUT":
            rows[0] = json.loads(request.content)
            return httpx.Response(200, json=rows[0])
        return httpx.Response(200, json=rows)

    env = {"HOMESERVER_RELEASE_INDEXER_PRIORITY": "uindex,1337x"}
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(handler)
    ) as client:
        await reconcile_indexer_filters("radarr", env, client, "apply")
    fields = {x["name"]: x["value"] for x in rows[0]["fields"]}
    assert fields["categories"] == [2000, 5070, 8000]
    assert fields["minimumSeeders"] == 5
    assert not rows[0]["enableAutomaticSearch"]


@pytest.mark.asyncio
async def test_migration_saves_native_path_before_testing_cross_service_connections():
    from homeserver_control.configuration.reconcile import reconcile_native_settings

    current = {"id": 1, "urlBase": "", "port": 7878}
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/radarr/api/v3/system/status":
            return httpx.Response(404)
        if request.url.path == "/api/v3/system/status":
            return httpx.Response(200, json={"appName": "Radarr"})
        assert request.url.path == "/api/v3/config/host"
        if request.method == "PUT":
            current.update(json.loads(request.content))
        return httpx.Response(200, json=current)

    result = await reconcile_native_settings(
        {"HOMESERVER_RADARR_URL": "http://radarr:7878/radarr"}, "apply",
        transport=httpx.MockTransport(handler),
    )
    from dataclasses import asdict

    from homeserver_common.cli import _validate_outcomes

    _validate_outcomes([asdict(x) for x in result])
    radarr = next(x for x in result if x.service == "radarr")
    assert radarr.changes[0].restart_required
    assert "Restart" in radarr.message
    assert all(x.status == "planned" for x in result)
    assert current["urlBase"] == "/radarr"
    assert not any("notification" in path or "indexer" in path for path in paths)


@pytest.mark.asyncio
async def test_path_discovery_does_not_retry_authentication_on_another_path():
    from homeserver_control.configuration.service_paths import select_native_base_url

    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(401)

    async with httpx.AsyncClient(
        base_url="http://radarr:7878/radarr", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await select_native_base_url("radarr", client)
    assert paths == ["/radarr/api/v3/system/status"]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["login", "spa"])
async def test_path_discovery_recognizes_native_unknown_route_fallback(missing):
    from homeserver_control.configuration.service_paths import select_native_base_url

    def handler(request):
        if request.url.path.startswith("/radarr/"):
            if missing == "login":
                return httpx.Response(302, headers={"Location": "http://radarr:7878/login"})
            return httpx.Response(200, text="<html>app</html>",
                                  headers={"Content-Type": "text/html"})
        return httpx.Response(200, json={"appName": "Radarr"})

    async with httpx.AsyncClient(
        base_url="http://radarr:7878/radarr", transport=httpx.MockTransport(handler)
    ) as client:
        await select_native_base_url("radarr", client)
        assert client.base_url.path == "/"


@pytest.mark.asyncio
async def test_jellyfin_save_can_require_restart_before_readback():
    from homeserver_control.configuration.service_paths import reconcile_service_path

    saved = []

    def handler(request):
        if request.method == "POST":
            saved.append(json.loads(request.content))
            return httpx.Response(204)
        if saved:
            return httpx.Response(302, headers={"Location": "../../jellyfin/web/"})
        return httpx.Response(200, json={"BaseUrl": "", "EnableIPv6": False})

    async with httpx.AsyncClient(
        base_url="http://jellyfin:8096", transport=httpx.MockTransport(handler)
    ) as client:
        changes = await reconcile_service_path(
            "jellyfin", {"HOMESERVER_JELLYFIN_URL": "http://jellyfin:8096/jellyfin"},
            client, "apply",
        )
    assert saved == [{"BaseUrl": "/jellyfin", "EnableIPv6": False}]
    assert changes[0].restart_required
