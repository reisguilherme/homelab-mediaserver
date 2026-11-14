"""ASC credentials stay private and do not use the public challenge proxy."""

import json
from copy import deepcopy

import httpx
import pytest

from homeserver_common.env import load_settings
from homeserver_control.configuration.prowlarr import reconcile_prowlarr


def test_private_asc_can_be_opted_in_without_changing_public_defaults(tmp_path):
    path = tmp_path / ".env"
    path.write_text("")
    assert load_settings(path).release_indexer_priority == ("uindex", "1337x")
    path.write_text('HOMESERVER_RELEASE_INDEXER_PRIORITY="uindex,1337x,amigosshare"\n')
    assert load_settings(path).release_indexer_priority == ("uindex", "1337x", "amigosshare")


@pytest.mark.asyncio
@pytest.mark.parametrize("restrict_sources", [False, True])
async def test_asc_cookie_reconcile_removes_only_managed_proxy_tag(restrict_sources):
    cookie = "amigos-share-club-session=fixture; XSRF-TOKEN=fixture"
    schema = {
        "name": "Amigos Share Club", "definitionName": "amigosshare",
        "implementation": "Cardigann", "configContract": "CardigannSettings",
        "privacy": "private", "protocol": "torrent", "priority": 25,
        "tags": [], "fields": [
            {"name": "definitionFile", "value": "amigosshare"},
            {"name": "baseUrl", "value": "https://amigos-share.club/"},
            {"name": "cookie", "value": "", "type": "textbox"},
            {"name": "freeleech", "value": False, "type": "checkbox"},
        ],
    }
    existing = deepcopy(schema) | {"id": 54, "enable": True, "appProfileId": 7,
                                   "tags": [9, 17]}
    existing["fields"][2]["value"] = cookie
    state = {
        "tag": [{"id": 9, "label": "homeserver-byparr"}, {"id": 17, "label": "personal"}],
        "indexerProxy": [],
        "appprofile": [{"id": 7, "name": "HomeServer", "enableRss": False,
                        "enableAutomaticSearch": False, "enableInteractiveSearch": True}],
        "indexer": [existing],
    }

    def handler(request):
        endpoint = request.url.path.split("/")[3]
        if request.url.path.endswith("/schema"):
            if endpoint == "indexer":
                return httpx.Response(200, json=[schema])
            assert endpoint == "indexerProxy"
            return httpx.Response(200, json=[{
                "implementation": "FlareSolverr", "configContract": "FlareSolverrSettings",
                "fields": [{"name": "host", "value": ""}], "tags": [],
            }])
        if request.method == "POST":
            state[endpoint].append(json.loads(request.content) | {"id": 55})
        elif request.method == "PUT":
            row = json.loads(request.content)
            current = next(item for item in state[endpoint] if item["id"] == row["id"])
            current.update(row)
        else:
            assert request.method == "GET"
        return httpx.Response(200, json=deepcopy(state[endpoint]))

    env = {
        "HOMESERVER_BYPARR_ENABLED": "true",
        "HOMESERVER_PROWLARR_INDEXERS": json.dumps([{
            "name": "Amigos Share Club", "definitionName": "amigosshare",
            "implementation": "Cardigann", "fields": [
                {"name": "baseUrl", "value": "https://amigos-share.club/"},
                {"name": "cookie", "value": cookie},
            ],
        }]),
    }
    if restrict_sources:
        env["HOMESERVER_RELEASE_INDEXER_PRIORITY"] = "uindex,1337x,amigosshare"
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_prowlarr(env, client, "apply")
        second = await reconcile_prowlarr(env, client, "apply")
    assert first.status == second.status == "verified"
    assert not second.changes
    assert state["indexer"][0]["id"] == 54
    assert state["indexer"][0]["enable"] is True
    assert state["indexer"][0]["tags"] == [17]
    assert state["indexerProxy"][0]["tags"] == [9]
    assert state["indexer"][0]["fields"][2]["value"] == cookie
    assert state["appprofile"][0]["enableAutomaticSearch"] is False
    assert state["appprofile"][0]["enableRss"] is False
    assert cookie not in repr(first)
