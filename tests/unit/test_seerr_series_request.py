"""Subtitle retries use a fresh, authenticated request for the exact TV season."""

from copy import deepcopy

import httpx
import pytest

from homeserver_control.adapters.http import ContractError, CredentialError, UpstreamError
from homeserver_control.adapters.seerr import SeerrAdapter


def approved_request():
    return {
        "id": 127, "status": 2,
        "media": {"tmdbId": 74606, "tvdbId": 293012, "externalServiceId": 124,
                  "mediaType": "tv"},
        "seasons": [{"seasonNumber": 1, "status": 2}, {"seasonNumber": 2, "status": 5}],
    }


def source(client):
    return SeerrAdapter(
        base_url="http://seerr", api_key="fixture", client=client, include_series_titles=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("request_status,season_status", [(2, 2), (2, 5), (5, 2), (5, 5)])
async def test_current_approved_or_available_season_returns_exact_authenticated_binding(
    request_status, season_status,
):
    payload = approved_request()
    payload["status"] = request_status
    payload["seasons"][0]["status"] = season_status

    def handler(request):
        assert request.headers["X-Api-Key"] == "fixture"
        if request.url.path == "/api/v1/request/127":
            return httpx.Response(200, json=payload)
        assert request.url.path == "/api/v1/tv/74606"
        assert request.url.params["language"] == "pt-BR"
        return httpx.Response(200, json={"id": 74606, "name": "Irmão do Jorel"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await source(client).get_series_request("127:1") == {
            "source_id": "127:1", "media_key": "season:tmdb:74606:1", "kind": "season",
            "tvdb_id": 293012, "external_service_id": 124, "title": "Irmão do Jorel",
        }


@pytest.mark.asyncio
async def test_request_status_is_refreshed_even_when_localized_title_is_cached():
    payload = approved_request()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/v1/request/127":
            return httpx.Response(200, json=deepcopy(payload))
        assert request.url.path == "/api/v1/tv/74606"
        return httpx.Response(200, json={"id": 74606, "name": "Irmão do Jorel"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = source(client)
        assert await adapter.get_series_request("127:1") is not None
        assert await adapter.get_series_request("127:1") is not None
        payload["status"] = 3
        assert await adapter.get_series_request("127:1") is None
    assert paths == [
        "/api/v1/request/127", "/api/v1/tv/74606",
        "/api/v1/request/127", "/api/v1/request/127",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["request_pending", "request_declined", "season_pending",
                                        "season_declined", "season_removed", "not_found"])
async def test_withdrawn_or_absent_request_does_not_fetch_or_return_an_alias(fault):
    payload = approved_request()
    status = 200
    if fault.startswith("request_"):
        payload["status"] = 1 if fault.endswith("pending") else 3
    elif fault.startswith("season_") and fault != "season_removed":
        payload["seasons"][0]["status"] = 1 if fault.endswith("pending") else 3
    elif fault == "season_removed":
        payload["seasons"].pop(0)
    else:
        status = 404

    def handler(request):
        assert request.url.path == "/api/v1/request/127"
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await source(client).get_series_request("127:1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("id", 128), ("id", "127"), ("id", 127.0), ("id", True),
    ("status", "2"), ("status", True), ("status", None),
    ("media", None), ("seasons", None),
])
async def test_malformed_or_foreign_request_cannot_supply_retry_proof(field, value):
    payload = approved_request()
    payload[field] = value

    def handler(request):
        assert request.url.path == "/api/v1/request/127"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ContractError):
            await source(client).get_series_request("127:1")


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("tmdbId", None), ("tmdbId", True), ("tmdbId", 0), ("tmdbId", "74606"),
    ("tvdbId", None), ("tvdbId", False), ("tvdbId", 0), ("tvdbId", 293012.0),
    ("externalServiceId", None), ("externalServiceId", True),
    ("externalServiceId", -1), ("externalServiceId", "124"), ("mediaType", "movie"),
])
async def test_retry_binding_requires_positive_strict_tv_identities(field, value):
    payload = approved_request()
    payload["media"][field] = value

    def handler(request):
        assert request.url.path == "/api/v1/request/127"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ContractError):
            await source(client).get_series_request("127:1")


@pytest.mark.asyncio
@pytest.mark.parametrize("seasons", [
    [{"seasonNumber": True, "status": 2}], [{"seasonNumber": 1.0, "status": 2}],
    [{"seasonNumber": "1", "status": 2}], [{"seasonNumber": 1, "status": True}],
    [{"seasonNumber": 1, "status": "5"}],
    [{"seasonNumber": 1, "status": 2}, {"seasonNumber": 1, "status": 5}],
    [None],
])
async def test_malformed_or_ambiguous_season_cannot_supply_retry_proof(seasons):
    payload = approved_request()
    payload["seasons"] = seasons

    def handler(request):
        assert request.url.path == "/api/v1/request/127"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ContractError):
            await source(client).get_series_request("127:1")


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["127", "127:1:2", "0:1", "-1:1", "127:-1",
                                     "127:True", "１２７:1", True, None])
async def test_invalid_source_identity_is_rejected_before_network(source_id):
    def handler(_request):
        raise AssertionError("invalid source identity reached the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await source(client).get_series_request(source_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,error", [(401, CredentialError), (403, CredentialError),
                                        (429, UpstreamError), (503, UpstreamError)])
async def test_request_http_failures_never_authorize_cached_retry_proof(status, error):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _request: httpx.Response(status, json=approved_request()),
    )) as client:
        with pytest.raises(error):
            await source(client).get_series_request("127:1")


@pytest.mark.asyncio
async def test_unverified_optional_localized_metadata_does_not_override_valid_request_ids():
    def handler(request):
        if request.url.path == "/api/v1/request/127":
            return httpx.Response(200, json=approved_request())
        assert request.url.path == "/api/v1/tv/74606"
        return httpx.Response(200, json={"id": 999, "name": "Foreign title"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await source(client).get_series_request("127:1") == {
            "source_id": "127:1", "media_key": "season:tmdb:74606:1", "kind": "season",
            "tvdb_id": 293012, "external_service_id": 124,
        }
