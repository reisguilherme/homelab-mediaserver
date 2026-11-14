from __future__ import annotations

from email.utils import formatdate
from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.worker import subdl
from homeserver_control.worker.subdl import SubDLSource

_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello!\n"
_RELEASE = "Fixture.Movie.2026.1080p.WEB"


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(monotonic=1000.0, wall=2_000_000_000.0)
    monkeypatch.setattr(subdl, "time", SimpleNamespace(
        monotonic=lambda: now.monotonic, time=lambda: now.wall,
    ))
    return now


def _payload(*, paths=("first",)):
    return {
        "status": True,
        "results": [{"tmdb_id": 42, "type": "movie"}],
        "subtitles": [{"language": "BR_PT", "unpack_files": [
            {"language": "BR_PT", "format": "srt", "size": len(_SRT),
             "release_name": _RELEASE, "url": f"/subtitle/123/{path}"}
            for path in paths
        ]}],
    }


async def _fetch(source, method, *, language="BR_PT"):
    if method == "fetch_movie":
        return await source.fetch_movie(
            tmdb_id=42, release_titles=[_RELEASE], language=language,
        )
    return await source.fetch(tmdb_id=42, release_title=_RELEASE, language=language)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fetch", "fetch_movie"])
@pytest.mark.parametrize("retry_after", ["seconds", "date", "missing", "invalid"])
async def test_quota_cooldown_blocks_other_languages_and_media_until_retry_after(
    method, retry_after, clock, caplog,
):
    requests = []
    delay = 120 if retry_after in {"seconds", "date"} else 900
    headers = {
        "seconds": {"Retry-After": "120"},
        "date": {"Retry-After": formatdate(clock.wall + 120, usegmt=True)},
        "missing": {},
        "invalid": {"Retry-After": "not-a-date"},
    }[retry_after]

    def handler(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(429, headers=headers, json={
                "status": False, "statusCode": 429, "error": "daily_limit",
                "message": "Daily Limit private-response-marker",
            })
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_payload())
        return httpx.Response(200, content=_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="fixture-private-key", client=client)
        assert await _fetch(source, method) is None
        assert await _fetch(source, method, language="EN") is None
        assert await source.fetch(
            tmdb_id=99, release_title="Fixture.S01E01.WEB", season=1, episode=1,
        ) is None
        assert await _fetch(source, "fetch_movie") is None
        assert requests == ["/api/v1/subtitles"]

        clock.monotonic += delay - 1
        assert await _fetch(source, method) is None
        assert requests == ["/api/v1/subtitles"]
        clock.monotonic += 1
        assert await _fetch(source, method) == _SRT

    assert requests == ["/api/v1/subtitles", "/api/v1/subtitles", "/subtitle/123/first"]
    assert "daily_limit" in caplog.text
    assert "fixture-private-key" not in caplog.text
    assert "private-response-marker" not in caplog.text
    assert "api_key=" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fetch", "fetch_movie"])
@pytest.mark.parametrize("status", [200, 403])
async def test_daily_limit_payload_also_suppresses_next_search(method, status, clock):
    searches = []

    def handler(request):
        searches.append(request.url.path)
        return httpx.Response(status, json={"status": False, "error": "daily_limit"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="fixture-key", client=client)
        assert await _fetch(source, method) is None
        assert await _fetch(source, method, language="EN") is None

    assert searches == ["/api/v1/subtitles"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fetch", "fetch_movie"])
async def test_http_429_without_json_still_defers_search(method, clock):
    searches = []

    def handler(request):
        searches.append(request.url.path)
        return httpx.Response(429, text="private upstream error")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="fixture-key", client=client)
        assert await _fetch(source, method) is None
        assert await _fetch(source, method) is None

    assert searches == ["/api/v1/subtitles"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fetch", "fetch_movie"])
async def test_download_quota_stops_remaining_candidates_and_reuses_search_after_cooldown(
    method, clock,
):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_payload(paths=("first", "second")))
        if len(requests) == 2:
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, content=_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="fixture-key", client=client)
        assert await _fetch(source, method) is None
        assert await _fetch(source, method, language="EN") is None
        assert requests == ["/api/v1/subtitles", "/subtitle/123/first"]
        clock.monotonic += 120
        assert await _fetch(source, method) == _SRT

    assert requests == ["/api/v1/subtitles", "/subtitle/123/first", "/subtitle/123/first"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["fetch", "fetch_movie"])
async def test_non_quota_failure_does_not_block_later_success(method, clock):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if len(requests) == 1:
            return httpx.Response(503, json={"status": False, "error": "unavailable"})
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_payload())
        return httpx.Response(200, content=_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="fixture-key", client=client)
        assert await _fetch(source, method) is None
        assert await _fetch(source, method) == _SRT

    assert requests == ["/api/v1/subtitles", "/api/v1/subtitles", "/subtitle/123/first"]
