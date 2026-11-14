"""Deferred descriptions must leave room for useful work in later discovery cycles."""

import asyncio
from time import monotonic
from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.worker import dubbed_discovery
from homeserver_control.worker.dubbed_discovery import DubbedDiscovery

MEDIA = {
    "id": 401,
    "tmdbId": 990001,
    "title": "Fixture Feature",
    "originalTitle": "Fixture Feature",
    "year": 2025,
}
QUALITY = {
    "quality": {"name": "WEBDL-1080p", "source": "webdl", "resolution": 1080}
}


def _release(number):
    return {
        "title": f"Fixture.Feature.2025.1080p.WEB-DL.Release{number}",
        "guid": f"native-{number}",
        "indexer": "UIndex (Prowlarr)",
        "indexerId": 16,
        "movieId": 401,
        "infoHash": f"{number:040x}",
        "size": 2_000_000_000,
        "seeders": 20,
        "protocol": "torrent",
        "downloadUrl": f"http://prowlarr:9696/6/download?link=native-{number}",
        "infoUrl": f"https://uindex.org/details.php?id={number}",
        "publishDate": "2025-01-01T00:00:00Z",
        "quality": QUALITY,
        "rejected": False,
        "rejections": [],
    }


def _api_response(request, native):
    assert request.method == "GET"
    path = request.url.path
    if request.url.host == "prowlarr":
        if path == "/api/v1/indexer":
            return httpx.Response(
                200,
                json=[
                    {"id": 6, "name": "UIndex", "enable": True},
                    {"id": 5, "name": "1337x", "enable": True},
                ],
            )
        if path in {"/api/v1/indexerstatus", "/api/v1/search"}:
            return httpx.Response(200, json=[])
    if request.url.host == "radarr" and path == "/api/v3/parse":
        assert request.url.params["title"] in {row["title"] for row in native}
        return httpx.Response(
            200,
            json={
                "movie": MEDIA,
                "languages": [{"name": "English"}],
                "parsedMovieInfo": {
                    "year": 2025,
                    "languages": [{"name": "English"}],
                    "quality": QUALITY,
                },
            },
        )
    raise AssertionError(str(request.url))


@pytest.mark.asyncio
async def test_cooldown_descriptions_allow_later_native_candidate_in_next_cycle():
    native = [_release(number) for number in range(1, 10)]
    descriptions = []

    def handler(request):
        if request.url.host == "uindex.org":
            assert "X-Api-Key" not in request.headers
            descriptions.append(str(request.url))
            if request.url.params["id"] != "9":
                return httpx.Response(503)
            return httpx.Response(200, text="Audio: English + Portuguese (Brazil)")
        return _api_response(request, native)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="fixture-prowlarr-key",
            interval_seconds=0,
            max_descriptions=8,
        )
        try:
            first = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={}
            )
            assert not first.complete
            assert descriptions == [row["infoUrl"] for row in native[:8]]
            assert all(
                item["source"] != "description"
                for row in first.releases
                for item in row["audioEvidence"]
            )

            second = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={}
            )
            assert not second.complete
            healthy = next(row for row in second.releases if row["guid"] == "native-9")
            assert {"source": "description", "languages": ["en", "pt-br"]} in healthy[
                "audioEvidence"
            ]
            assert descriptions == [row["infoUrl"] for row in native]
            assert healthy["indexerId"] == 16
            assert healthy["movieId"] == 401
            assert healthy["rejected"] is False
            assert healthy["rejections"] == []
            assert healthy.get("requiresNativeDecision", False) is False
        finally:
            await discovery.aclose()


@pytest.mark.asyncio
async def test_description_progress_survives_expired_cooldowns(monkeypatch):
    native = [_release(number) for number in range(1, 10)]
    descriptions = []
    clock = SimpleNamespace(offset=0)
    # Shift only discovery's clock; asyncio keeps its own real deadlines.
    monkeypatch.setattr(
        dubbed_discovery, "time", SimpleNamespace(monotonic=lambda: monotonic() + clock.offset)
    )

    def handler(request):
        if request.url.host == "uindex.org":
            descriptions.append(str(request.url))
            if request.url.params["id"] != "9":
                return httpx.Response(503)
            return httpx.Response(200, text="Audio: English + Portuguese (Brazil)")
        return _api_response(request, native)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="fixture-prowlarr-key",
            interval_seconds=0,
            max_descriptions=8,
        )
        try:
            first = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={}
            )
            assert not first.complete
            assert descriptions == [row["infoUrl"] for row in native[:8]]

            clock.offset += 301
            second = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={}
            )
            assert not second.complete
            healthy = next(row for row in second.releases if row["guid"] == "native-9")
            assert {"source": "description", "languages": ["en", "pt-br"]} in healthy[
                "audioEvidence"
            ]
            assert native[8]["infoUrl"] in descriptions[8:]
            assert any(descriptions.count(row["infoUrl"]) > 1 for row in native[:8])
            assert {row["guid"] for row in second.releases} == {
                f"native-{number}" for number in range(1, 10)
            }
        finally:
            await discovery.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://uindex.org/unsupported?id={number}",
    "https://user:pass@uindex.org/details.php?id={number}",
    "http://localhost/details.php?id={number}",
    "https://uindex.org:invalid/details.php?id={number}",
    "https://uindex.org:444/details.php?id={number}",
])
async def test_rejected_description_urls_finish_over_budget_without_external_requests(url):
    native = [_release(number) for number in range(1, 10)]
    for number, row in enumerate(native, start=1):
        row["infoUrl"] = url.format(number=number)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host in {"prowlarr", "radarr"}, "Rejected URL reached HTTP client"
        return _api_response(request, native)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696", prowlarr_api_key="fixture",
            interval_seconds=0, max_descriptions=8,
        )
        try:
            first = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={},
            )
            assert not first.complete
            second = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={},
            )
            assert second.complete
            assert len(second.releases) == 9
            assert all(
                item["source"] != "description"
                for row in second.releases
                for item in row["audioEvidence"]
            )
        finally:
            await discovery.aclose()
    assert {request.url.host for request in requests} == {"prowlarr", "radarr"}


@pytest.mark.asyncio
async def test_joining_background_description_preserves_budget_for_later_candidate():
    native = [_release(1), _release(2)]
    finish_background = asyncio.Event()
    background_finished = asyncio.Event()
    descriptions = []

    async def handler(request):
        if request.url.host == "uindex.org":
            descriptions.append(str(request.url))
            if request.url.params["id"] == "1":
                await finish_background.wait()
                background_finished.set()
                return httpx.Response(200, text="Audio: English")
            return httpx.Response(200, text="Audio: English + Portuguese (Brazil)")
        if (
            request.url.host == "radarr"
            and request.url.path == "/api/v3/parse"
            and request.url.params["title"] == native[1]["title"]
        ):
            finish_background.set()
            await asyncio.wait_for(background_finished.wait(), timeout=0.3)
            await asyncio.sleep(0)
        return _api_response(request, native)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="fixture-prowlarr-key",
            interval_seconds=0,
            max_descriptions=1,
            total_timeout_seconds=1,
            description_timeout_seconds=0.01,
            description_runtime_seconds=1,
        )
        try:
            first = await discovery.enrich(
                MEDIA, native[:1], arr_url="http://radarr:7878", arr_headers={}
            )
            assert not first.complete
            assert descriptions == [native[0]["infoUrl"]]

            second = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={}
            )
            assert not second.complete
            healthy = next(row for row in second.releases if row["guid"] == "native-2")
            assert {"source": "description", "languages": ["en", "pt-br"]} in healthy[
                "audioEvidence"
            ]
            assert descriptions == [row["infoUrl"] for row in native]
        finally:
            finish_background.set()
            await discovery.aclose()
