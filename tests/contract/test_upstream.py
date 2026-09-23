import httpx
import pytest

from homeserver_control.adapters.arr import ArrAdapterClient, CredentialError, EffectUncertain
from homeserver_control.adapters.contracts import MediaRef
from homeserver_control.adapters.seerr import SeerrAdapter


@pytest.mark.asyncio
async def test_seerr_pagination_maps_only_approved_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/request"
        assert request.url.params["take"] == "20"
        assert request.url.params["skip"] == "20"
        assert request.url.params["filter"] == "approved"
        return httpx.Response(
            200,
            json={
                "pageInfo": {"pages": 2, "page": 2},
                "results": [
                    {"id": 7, "media": {"tmdbId": 123, "mediaType": "movie"}, "status": 2},
                    {"id": 8, "media": {"tmdbId": 456, "mediaType": "movie"}, "status": 1},
                    {"id": 9, "media": {"tmdbId": 789, "mediaType": "movie"}, "status": 3},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://seerr") as client:
        adapter = SeerrAdapter(base_url="http://seerr", api_key="secret", client=client)
        page = await adapter.list_approved(page=2)
    assert page == [{"source_id": "7", "media_key": "movie:tmdb:123", "kind": "movie"}]


@pytest.mark.asyncio
async def test_arr_credential_error_is_not_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"message": "bad key"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://arr"
    ) as client:
        adapter = ArrAdapterClient(base_url="http://arr", api_key="secret", client=client)
        with pytest.raises(CredentialError):
            await adapter.search(MediaRef(media_key="movie:tmdb:1", kind="movie", source_id=1))
    assert calls == 1


@pytest.mark.asyncio
async def test_arr_timeout_after_grab_is_explicitly_uncertain() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://arr"
    ) as client:
        adapter = ArrAdapterClient(base_url="http://arr", api_key="secret", client=client)
        with pytest.raises(EffectUncertain):
            await adapter.grab(
                {
                    "guid": "release-1",
                    "indexer_id": 1,
                    "app": "radarr",
                    "title": "Fixture",
                    "reported_bytes": 10,
                    "torrent_url": "https://indexer.invalid/fixture.torrent",
                }
            )
