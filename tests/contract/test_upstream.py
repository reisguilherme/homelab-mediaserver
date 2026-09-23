import httpx
import pytest

from homeserver_control.adapters.arr import ArrAdapterClient, CredentialError, EffectUncertain
from homeserver_control.adapters.contracts import MediaRef
from homeserver_control.adapters.http import ContractError
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
async def test_seerr_tv_request_maps_each_approved_season_with_tmdb_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/request"
        return httpx.Response(200, json={"results": [{
            "id": 3, "status": 2,
            "media": {"tmdbId": 97546, "tvdbId": 383203, "mediaType": "tv"},
            "seasons": [{"seasonNumber": 3, "status": 2},
                        {"seasonNumber": 4, "status": 2}],
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await SeerrAdapter(
            base_url="http://seerr", api_key="secret", client=client
        ).list_approved(page=1)
    assert page == [
        {"source_id": "3:3", "media_key": "season:tmdb:97546:3", "kind": "season"},
        {"source_id": "3:4", "media_key": "season:tmdb:97546:4", "kind": "season"},
    ]


@pytest.mark.asyncio
async def test_seerr_request_lookup_distinguishes_removed_from_approved() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/request/7":
            return httpx.Response(404, json={"message": "Request not found"})
        if request.url.path == "/api/v1/request/8":
            return httpx.Response(200, json={"id": 8, "status": 2})
        raise AssertionError("unexpected request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SeerrAdapter(base_url="http://seerr", api_key="secret", client=client)
        assert await adapter.get_request_status("7") is None
        assert await adapter.get_request_status("8") == 2


@pytest.mark.asyncio
async def test_seerr_season_lookup_detects_withdrawn_season() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/request/3"
        return httpx.Response(200, json={
            "id": 3, "status": 2,
            "seasons": [{"seasonNumber": 4, "status": 2}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SeerrAdapter(base_url="http://seerr", api_key="secret", client=client)
        assert await adapter.get_request_status("3:4") == 2
        assert await adapter.get_request_status("3:3") is None


@pytest.mark.asyncio
async def test_seerr_season_lookup_does_not_treat_missing_status_as_withdrawal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": 3, "status": 2, "seasons": [{"seasonNumber": 4}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SeerrAdapter(base_url="http://seerr", api_key="secret", client=client)
        with pytest.raises(ContractError, match="status"):
            await adapter.get_request_status("3:4")


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
