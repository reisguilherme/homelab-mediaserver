import json
from copy import deepcopy

import httpx
import pytest

from homeserver_control.configuration.native_config import NativeConfigurationError
from homeserver_control.configuration.prowlarr import reconcile_prowlarr


class NativeProwlarr:
    def __init__(self, *, managed_tag=True, tag_status=200, tag_payload=None, drop_tags=False):
        self.writes = []
        self.tag_status = tag_status
        self.tag_payload = tag_payload
        self.drop_tags = drop_tags
        self.state = {
            "tag": [{"id": 3, "label": "manual"}],
            "indexerProxy": [
                {
                    "id": 40,
                    "name": "HomeServer Byparr",
                    "implementation": "FlareSolverr",
                    "configContract": "FlareSolverrSettings",
                    "tags": [3],
                    "fields": [{"name": "host", "value": "http://byparr:8191"}],
                    "manual": "preserve-proxy",
                }
            ],
            "appprofile": [
                {
                    "id": 7,
                    "name": "HomeServer",
                    "enableRss": False,
                    "enableAutomaticSearch": False,
                    "enableInteractiveSearch": True,
                }
            ],
            "indexer": [
                {
                    "id": 54,
                    "name": "UIndex",
                    "definitionName": "uindex",
                    "implementation": "Cardigann",
                    "fields": [{"name": "baseUrl", "value": "https://uindex.org/"}],
                    "enable": True,
                    "appProfileId": 7,
                    "tags": [11],
                    "manual": "preserve-indexer",
                },
                {
                    "id": 88,
                    "name": "Unmanaged",
                    "implementation": "Cardigann",
                    "tags": [3],
                    "fields": [],
                },
            ],
        }
        if managed_tag:
            self.state["tag"].append({"id": 9, "label": "homeserver-byparr"})

    def handler(self, request):
        endpoint = request.url.path.split("/")[3]
        if endpoint == "tag" and self.tag_status != 200:
            return httpx.Response(self.tag_status)
        if endpoint == "tag" and self.tag_payload is not None:
            return httpx.Response(200, json=self.tag_payload)
        if request.url.path.endswith("/schema"):
            if endpoint == "indexerProxy":
                row = deepcopy(self.state[endpoint][0])
            else:
                row = deepcopy(self.state["indexer"][0])
            for key in ("id", "manual"):
                row.pop(key, None)
            return httpx.Response(200, json=[row])
        if request.method in {"POST", "PUT", "DELETE"}:
            payload = json.loads(request.content)
            self.writes.append((request.method, request.url.path, deepcopy(payload)))
            if request.method == "POST":
                payload["id"] = 99
                self.state[endpoint].append(payload)
            else:
                current = next(row for row in self.state[endpoint] if row["id"] == payload["id"])
                if self.drop_tags:
                    payload["tags"] = current.get("tags", [])
                current.update(payload)
        return httpx.Response(200, json=deepcopy(self.state[endpoint]))

    @property
    def env(self):
        return {
            "HOMESERVER_BYPARR_ENABLED": "true",
            "HOMESERVER_BYPARR_URL": "http://byparr:8191",
            "HOMESERVER_PROWLARR_INDEXERS": json.dumps(
                [
                    {
                        "id": 54,
                        "name": "UIndex",
                        "definitionName": "uindex",
                        "implementation": "Cardigann",
                        "tags": [12],
                        "fields": [{"name": "baseUrl", "value": "https://uindex.org/"}],
                    }
                ]
            ),
        }


@pytest.mark.asyncio
async def test_byparr_tag_binds_managed_proxy_and_indexers_without_losing_tags():
    native = NativeProwlarr()
    unmanaged = deepcopy(native.state["indexer"][1])
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(native.handler)
    ) as client:
        first = await reconcile_prowlarr(native.env, client, "apply")
        write_count = len(native.writes)
        second = await reconcile_prowlarr(native.env, client, "apply")
    assert first.status == second.status == "verified"
    assert native.state["indexerProxy"][0]["tags"] == [3, 9]
    assert native.state["indexer"][0]["tags"] == [9, 11, 12]
    assert native.state["indexerProxy"][0]["manual"] == "preserve-proxy"
    assert native.state["indexer"][0]["manual"] == "preserve-indexer"
    assert native.state["indexer"][1] == unmanaged
    assert not second.changes
    assert len(native.writes) == write_count
    assert {path for _, path, _ in native.writes} == {
        "/api/v1/indexerProxy/40",
        "/api/v1/indexer/54",
    }


@pytest.mark.asyncio
async def test_byparr_creates_one_stable_native_tag_and_reuses_its_returned_id():
    native = NativeProwlarr(managed_tag=False)
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(native.handler)
    ) as client:
        first = await reconcile_prowlarr(native.env, client, "apply")
        second = await reconcile_prowlarr(native.env, client, "apply")
    assert first.status == second.status == "verified"
    assert native.state["tag"] == [
        {"id": 3, "label": "manual"},
        {"id": 99, "label": "homeserver-byparr"},
    ]
    assert native.state["indexerProxy"][0]["tags"] == [3, 99]
    assert native.state["indexer"][0]["tags"] == [11, 12, 99]
    assert not second.changes
    assert len([write for write in native.writes if write[1] == "/api/v1/tag"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, status", [("plan", "planned"), ("verify", "drift")])
async def test_missing_byparr_tag_plans_without_writes_or_guessing_an_id(mode, status):
    native = NativeProwlarr(managed_tag=False)
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(native.handler)
    ) as client:
        result = await reconcile_prowlarr(native.env, client, mode)
    assert result.status == status
    assert native.writes == []
    assert any(
        change.key == "label" and change.after == "homeserver-byparr"
        for change in result.changes
    )
    assert any(change.key == "byparr.tagId" for change in result.changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, status", [("plan", "planned"), ("verify", "drift")])
async def test_existing_byparr_tag_detects_proxy_and_indexer_drift_without_writes(mode, status):
    native = NativeProwlarr()
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(native.handler)
    ) as client:
        result = await reconcile_prowlarr(native.env, client, mode)
    assert result.status == status
    assert native.writes == []
    assert len([change for change in result.changes if change.key == "tags"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("tag_status", [404, 405, 501])
async def test_missing_native_tag_api_is_explicitly_unsupported(tag_status):
    native = NativeProwlarr(tag_status=tag_status)
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(native.handler)
    ) as client:
        result = await reconcile_prowlarr(native.env, client, "apply")
    assert result.status == "unsupported"
    assert "tag" in result.message.lower()
    assert native.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{}, [{"id": 0, "label": "homeserver-byparr"}], [{"id": True, "label": "manual"}]],
)
async def test_unknown_native_tag_schema_fails_before_writing(payload):
    native = NativeProwlarr(tag_payload=payload)
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(native.handler)
    ) as client:
        result = await reconcile_prowlarr(native.env, client, "apply")
    assert result.status == "unsupported"
    assert native.writes == []


@pytest.mark.asyncio
async def test_byparr_association_requires_verified_native_readback():
    native = NativeProwlarr(drop_tags=True)
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(native.handler)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="read-back drift"):
            await reconcile_prowlarr(native.env, client, "apply")


@pytest.mark.asyncio
async def test_disabled_byparr_does_not_require_or_change_proxy_tags():
    native = NativeProwlarr(tag_status=404)
    proxy = deepcopy(native.state["indexerProxy"])
    env = native.env | {"HOMESERVER_BYPARR_ENABLED": "false"}
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(native.handler)
    ) as client:
        result = await reconcile_prowlarr(env, client, "apply")
    assert result.status == "verified"
    assert native.state["indexerProxy"] == proxy
    assert not any("tag" in path or "Proxy" in path for _, path, _ in native.writes)
