import httpx
import pytest

from homeserver_control.worker.movie_priority import MoviePrioritizer


@pytest.mark.asyncio
async def test_movie_priority_calls_internal_gateway_once_per_interval() -> None:
    now = [100.0]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/internal/prioritize-movies"
        assert request.headers["X-Arr-Token"] == "worker-secret"
        assert request.content == b""
        return httpx.Response(200, json={"state": "reordered", "count": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        prioritizer = MoviePrioritizer(
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            client=client, clock=lambda: now[0],
        )
        assert await prioritizer.prioritize() == "reordered"
        now[0] += 59
        assert await prioritizer.prioritize() == "deferred"
        now[0] += 1
        assert await prioritizer.prioritize() == "reordered"

    assert len(requests) == 2


@pytest.mark.asyncio
async def test_movie_priority_rejects_invalid_gateway_reply() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"state": "unexpected"})
        )
    ) as client:
        prioritizer = MoviePrioritizer(
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            client=client,
        )
        with pytest.raises(ValueError, match="invalid priority response"):
            await prioritizer.prioritize()
