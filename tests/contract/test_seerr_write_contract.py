import json
from copy import deepcopy

import httpx
import pytest

from homeserver_control.configuration.arr import collection
from homeserver_control.configuration.media_servers import reconcile_media_server


@pytest.mark.asyncio
async def test_existing_seerr_jellyfin_connection_changes_without_read_only_fields():
    state = {
        "name": "Existing server",
        "ip": "192.168.1.20",
        "port": 8096,
        "urlBase": "/previous",
        "useSsl": False,
        "apiKey": "existing-jellyfin-token",
        "serverId": "existing-server-id",
        "externalHostname": "http://100.74.194.46:8096",
        "libraries": [
            {"id": "existing-movies", "name": "Movies", "enabled": True},
            {"id": "existing-tv", "name": "TV", "enabled": True},
            {"id": "manual-library", "name": "Manual", "enabled": False},
        ],
    }
    original = deepcopy(state)
    writes = []

    def handler(request):
        if request.url.path == "/api/v1/settings/public":
            return httpx.Response(200, json={"initialized": True})
        assert request.url.path == "/api/v1/settings/jellyfin"
        if request.method == "POST":
            payload = json.loads(request.content)
            if {"name", "libraries"} & payload.keys():
                return httpx.Response(400, json={
                    "message": "request.body.name and request.body.libraries are read-only",
                })
            writes.append(payload)
            # Jellyseerr updates settings with Object.assign; omitted fields survive.
            state.update(payload)
        return httpx.Response(200, json=state)

    async with httpx.AsyncClient(
        base_url="http://seerr", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_media_server("seerr", {}, client, "apply")
        assert first.status == "verified"
        assert (await reconcile_media_server("seerr", {}, client, "verify")).status == "verified"
        assert not (await reconcile_media_server("seerr", {}, client, "apply")).changes
    desired = {"ip": "jellyfin", "port": 8096, "urlBase": "", "useSsl": False}
    assert writes == [desired]
    assert state == original | desired
    assert "existing-jellyfin-token" not in repr(first)


@pytest.mark.asyncio
@pytest.mark.parametrize("service,port", [("radarr", 7878), ("sonarr", 8989)])
async def test_existing_seerr_arr_update_keeps_id_in_route_and_omits_it_from_body(service, port):
    endpoint = f"/api/v1/settings/{service}"
    state = [{
        "id": 73,
        "name": f"HomeServer {service.title()}",
        "hostname": service,
        "port": port,
        "apiKey": "existing-arr-key",
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": 2,
        "activeProfileName": "HomeServer",
        "activeDirectory": "/data/media/movies" if service == "radarr" else "/data/media/tv",
        "is4k": False,
        "isDefault": True,
        "minimumAvailability": "released",
        "enableSeasonFolders": True,
        "syncEnabled": True,
        "preventSearch": False,
        "tags": [17],
        "manual": "keep",
    }]
    original = deepcopy(state[0])
    writes = []

    def handler(request):
        if request.method == "PUT":
            assert request.url.path == endpoint + "/73"
            payload = json.loads(request.content)
            if "id" in payload:
                return httpx.Response(400, json={"message": "request.body.id is read-only"})
            writes.append(payload)
            # The native PUT handler takes identity from the route.
            state[0] = payload | {"id": 73}
        else:
            assert request.url.path == endpoint and request.method == "GET"
        return httpx.Response(200, json=state)

    desired = {
        "name": f"HomeServer {service.title()}",
        "activeProfileId": 93,
        "preventSearch": True,
    }
    async with httpx.AsyncClient(
        base_url="http://seerr", transport=httpx.MockTransport(handler)
    ) as client:
        assert await collection("seerr", client, endpoint, desired, "name", "apply")
        assert not await collection("seerr", client, endpoint, desired, "name", "verify")
        assert not await collection("seerr", client, endpoint, desired, "name", "apply")
    assert len(writes) == 1
    assert state == [original | desired]
    assert state[0]["id"] == 73
