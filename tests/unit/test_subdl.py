from __future__ import annotations

import httpx
import pytest

from homeserver_control.worker.subdl import SubDLSource

_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nOla!\n"


@pytest.mark.asyncio
async def test_subdl_fetches_only_exact_release_brazilian_episode():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append((request.url.host, request.url.path))
        if request.url.host == "api.subdl.com":
            assert request.url.params["tmdb_id"] == "97546"
            assert request.url.params["season_number"] == "4"
            assert request.url.params["episode_number"] == "1"
            assert request.url.params["languages"] == "BR_PT"
            return httpx.Response(200, json={
                "status": True,
                "results": [{"tmdb_id": 97546, "type": "tv"}],
                "subtitles": [{
                    "language": "BR_PT", "season": 4, "episode": 1,
                    "release_name": "Ted.Lasso.S04E01.1080p.WEB.H264-CAKES",
                    "unpack_files": [{
                        "language": "BR_PT", "season": 4, "episode": 1,
                        "release_name": "Ted.Lasso.S04E01.1080p.WEB.H264-CAKES",
                        "format": "srt", "size": len(_SRT),
                        "url": "/subtitle/123/abc?api_key=must-not-forward",
                    }],
                }],
            })
        assert request.url.host == "dl.subdl.com"
        assert request.url.path == "/subtitle/123/abc"
        assert not request.url.query
        return httpx.Response(200, content=_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch(
            tmdb_id=97546, release_title="Ted Lasso S04E01 1080p WEB H264 CAKES",
            season=4, episode=1,
        )
    assert result == _SRT
    assert requested == [
        ("api.subdl.com", "/api/v1/subtitles"),
        ("dl.subdl.com", "/subtitle/123/abc"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"language": "PT"},
        {"episode": 2},
        {"release_name": "Ted.Lasso.S04E01.1080p.WEBRip.H264-CAKES"},
        {"url": "https://attacker.example/subtitle/123/abc"},
        {"format": "zip"},
    ],
)
async def test_subdl_rejects_ambiguous_or_untrusted_results(override):
    subtitle = {
        "language": "BR_PT", "season": 4, "episode": 1,
        "release_name": "Ted.Lasso.S04E01.1080p.WEB.H264-CAKES",
        "format": "srt", "size": len(_SRT), "url": "/subtitle/123/abc",
    }
    subtitle.update(override)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "download must not occur"
        return httpx.Response(200, json={
            "status": True, "results": [{"tmdb_id": 97546, "type": "tv"}],
            "subtitles": [{
                "language": "BR_PT", "season": 4, "episode": 1,
                "unpack_files": [subtitle],
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        assert await source.fetch(
            tmdb_id=97546, release_title="Ted Lasso S04E01 1080p WEB H264 CAKES",
            season=4, episode=1,
        ) is None


@pytest.mark.asyncio
async def test_subdl_rejects_oversized_or_invalid_srt():
    body = b"x" * 1_000_001

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json={
                "status": True,
                "results": [{"tmdb_id": 152532, "type": "movie"}],
                "subtitles": [{"language": "BR_PT", "unpack_files": [{
                    "language": "BR_PT", "format": "srt", "size": 300,
                    "release_name": "Dallas.Buyers.Club.2013.1080p.BluRay.x264-SPARKS",
                    "url": "/subtitle/123/abc",
                }]}],
            })
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        assert await source.fetch(
            tmdb_id=152532,
            release_title="Dallas Buyers Club 2013 1080p BluRay x264 SPARKS",
        ) is None
