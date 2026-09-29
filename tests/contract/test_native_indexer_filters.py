import json
from copy import deepcopy

import httpx
import pytest

from homeserver_control.configuration.arr import reconcile_indexer_filters


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["prowlarr", "sonarr", "radarr"])
@pytest.mark.parametrize("masked", [False, True])
async def test_only_preferred_indexers_are_enabled_without_deleting_or_losing_native_settings(
    service, masked
):
    suffix = "" if service == "prowlarr" else " (Prowlarr)"
    rows = [
        {
            "id": index,
            "name": name + suffix,
            "enable": True,
            "enableInteractiveSearch": True,
            "enableAutomaticSearch": True,
            "enableRss": True,
            "tags": [17],
            "fields": [{"name": "baseUrl", "value": "https://example.invalid"}],
        }
        for index, name in enumerate(("UIndex", "1337x", "LimeTorrents", "Nyaa.si", "YTS"), 1)
    ]
    original = deepcopy(rows)
    if masked:
        for row in rows:
            row["fields"].append({"name": "apiKey", "value": "********"})
        original = deepcopy(rows)
    writes = []

    def handler(request):
        if request.method == "PUT":
            payload = json.loads(request.content)
            writes.append(payload)
            current = next(row for row in rows if row["id"] == payload["id"])
            current.update(payload)
        else:
            assert request.method == "GET"
        return httpx.Response(200, json=deepcopy(rows))

    async with httpx.AsyncClient(
        base_url=f"http://{service}", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_indexer_filters(
            service, {"HOMESERVER_RELEASE_INDEXER_PRIORITY": "uindex,1337x"}, client, "apply"
        )
        count = len(writes)
        second = await reconcile_indexer_filters(
            service, {"HOMESERVER_RELEASE_INDEXER_PRIORITY": "uindex,1337x"}, client, "apply"
        )
    assert first and not second and len(writes) == count
    enabled_key = "enable" if service == "prowlarr" else "enableInteractiveSearch"
    assert [row["name"] for row in rows if row[enabled_key]] == [
        "UIndex" + suffix,
        "1337x" + suffix,
    ]
    assert len(rows) == len(original)
    for row, before in zip(rows, original, strict=True):
        assert (
            row["id"] == before["id"]
            and row["tags"] == before["tags"]
            and row["fields"] == before["fields"]
        )
        if service != "prowlarr":
            assert row["enableRss"] is False and row["enableAutomaticSearch"] is False
