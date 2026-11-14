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
            "media": {
                "tmdbId": 97546, "tvdbId": 383203, "externalServiceId": 124,
                "mediaType": "tv",
            },
            "seasons": [{"seasonNumber": 3, "status": 2},
                        {"seasonNumber": 4, "status": 2}],
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await SeerrAdapter(
            base_url="http://seerr", api_key="secret", client=client
        ).list_approved(page=1)
    assert page == [
        {"source_id": "3:3", "media_key": "season:tmdb:97546:3", "kind": "season",
         "tvdb_id": 383203, "external_service_id": 124},
        {"source_id": "3:4", "media_key": "season:tmdb:97546:4", "kind": "season",
         "tvdb_id": 383203, "external_service_id": 124},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["tmdbId", "tvdbId", "externalServiceId"])
@pytest.mark.parametrize("invalid", [True, False, 0, -1, "6", 6.0])
async def test_seerr_tv_rejects_malformed_positive_identity(field, invalid):
    media = {"tmdbId": 97546, "tvdbId": 383203, "externalServiceId": 6, "mediaType": "tv"}
    media[field] = invalid

    def handler(request):
        return httpx.Response(200, json={"results": [{
            "id": 3, "status": 2, "media": media,
            "seasons": [{"seasonNumber": 1, "status": 2}],
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        with pytest.raises(ContractError):
            await adapter.list_approved(1)


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [7, "7", "007"])
@pytest.mark.parametrize("media_type", ["movie", "tv"])
async def test_seerr_approved_request_accepts_positive_ascii_decimal_identity(
    request_id, media_type,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/api/v1/request"
        return httpx.Response(200, json={"results": [{
            "id": request_id, "status": 2,
            "media": {"tmdbId": 123, "mediaType": media_type},
            "seasons": [{"seasonNumber": 3, "status": 2}],
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        page = await SeerrAdapter(
            base_url="http://seerr", api_key="fixture", client=client,
        ).list_approved(1)
    if media_type == "movie":
        assert page == [{"source_id": str(request_id), "media_key": "movie:tmdb:123",
                         "kind": "movie"}]
    else:
        assert page == [{"source_id": f"{request_id}:3", "media_key": "season:tmdb:123:3",
                         "kind": "season"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(0, id="zero"),
    pytest.param(-7, id="negative"),
    pytest.param(7.0, id="float"),
    pytest.param(None, id="null"),
    pytest.param("", id="empty"),
    pytest.param("0", id="zero-string"),
    pytest.param("000", id="zero-padded"),
    pytest.param("-7", id="negative-string"),
    pytest.param("+7", id="signed"),
    pytest.param(" 7", id="leading-space"),
    pytest.param("7 ", id="trailing-space"),
    pytest.param("7:3", id="season-suffix"),
    pytest.param("7\n", id="newline"),
    pytest.param("\u0667", id="arabic-decimal"),
    pytest.param("\uff17", id="full-width-decimal"),
    pytest.param("https://fixture.invalid/?token=upstream-body-private", id="private-url"),
    pytest.param("upstream-body-private", id="private-token"),
])
async def test_seerr_approved_request_rejects_invalid_identity_without_response_details(
    request_id,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET" and request.url.path == "/api/v1/request"
        return httpx.Response(200, json={"results": [{
            "id": request_id, "status": 2,
            "media": {"tmdbId": 123, "mediaType": "movie"},
            "upstream_detail": "upstream-body-private",
        }]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        with pytest.raises(ContractError) as failure:
            await adapter.list_approved(1)
    assert "upstream-body-private" not in str(failure.value)


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
