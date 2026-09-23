from __future__ import annotations

import httpx
import pytest

from homeserver_control.worker.subdl import SubDLSource

_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nOla!\n"
_SRT_CP1252 = "1\r\n00:00:01,000 --> 00:00:02,000\r\nAção!\r\n".encode("cp1252")


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
async def test_subdl_normalizes_exact_release_cp1252_subtitle_to_utf8():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json={
                "status": True,
                "results": [{"tmdb_id": 97546, "type": "tv"}],
                "subtitles": [{
                    "language": "BR_PT", "season": 4, "episode": 1,
                    "unpack_files": [{
                        "language": "BR_PT", "season": 4, "episode": 1,
                        "release_name": "Ted.Lasso.S04E01.1080p.WEB.H264-CAKES",
                        "format": "srt", "size": len(_SRT_CP1252),
                        "url": "/subtitle/123/abc",
                    }],
                }],
            })
        return httpx.Response(200, content=_SRT_CP1252)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch(
            tmdb_id=97546, release_title="Ted Lasso S04E01 1080p WEB H264 CAKES",
            season=4, episode=1,
        )

    assert result == "1\r\n00:00:01,000 --> 00:00:02,000\r\nAção!\r\n".encode()


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


@pytest.mark.asyncio
async def test_subdl_reuses_one_title_search_when_checking_multiple_releases():
    searches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal searches
        if request.url.host == "api.subdl.com":
            searches += 1
            return httpx.Response(200, json={
                "status": True, "results": [{"tmdb_id": 97546, "type": "tv"}],
                "subtitles": [{
                    "language": "BR_PT", "season": 4, "episode": 1,
                    "unpack_files": [{
                        "language": "BR_PT", "season": 4, "episode": 1,
                        "release_name": "Ted.Lasso.S04E01.1080p.WEB.H264-CAKES",
                        "format": "srt", "size": len(_SRT),
                        "url": "/subtitle/123/abc",
                    }],
                }],
            })
        return httpx.Response(200, content=_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        assert await source.fetch(
            tmdb_id=97546, release_title="Ted Lasso S04E01 1080p WEB H264 ETHEL",
            season=4, episode=1,
        ) == _SRT
        assert await source.fetch(
            tmdb_id=97546, release_title="Ted Lasso S04E01 1080p WEB H264 CAKES",
            season=4, episode=1,
        ) == _SRT
    assert searches == 1


@pytest.mark.asyncio
async def test_subdl_fetches_exact_english_fallback_without_reusing_portuguese_search():
    searches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            language = request.url.params["languages"]
            searches.append(language)
            return httpx.Response(200, json={
                "status": True,
                "results": [{"tmdb_id": 97546, "type": "tv"}],
                "subtitles": [{
                    "language": language, "season": 1, "episode": 1,
                    "unpack_files": [{
                        "language": language, "season": 1, "episode": 1,
                        "release_name": "Slow.Horses.S01E01.1080p.WEB.H264",
                        "format": "srt", "size": len(_SRT),
                        "url": "/subtitle/123/abc",
                    }],
                }],
            })
        return httpx.Response(200, content=_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        portuguese = await source.fetch(
            tmdb_id=97546, release_title="Slow Horses S01E01 1080p WEB H264",
            season=1, episode=1,
        )
        english = await source.fetch(
            tmdb_id=97546, release_title="Slow Horses S01E01 1080p WEB H264",
            season=1, episode=1, language="EN",
        )

    assert portuguese == _SRT
    assert english == _SRT
    assert searches == ["BR_PT", "EN"]


@pytest.mark.asyncio
async def test_subdl_rejects_wrong_language_file_in_english_search():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "wrong-language file must not download"
        assert request.url.params["languages"] == "EN"
        return httpx.Response(200, json={
            "status": True,
            "results": [{"tmdb_id": 152532, "type": "movie"}],
            "subtitles": [{"language": "EN", "unpack_files": [{
                "language": "BR_PT", "format": "srt", "size": len(_SRT),
                "release_name": "Dallas.Buyers.Club.2013.1080p.BluRay.x264-SPARKS",
                "url": "/subtitle/123/abc",
            }]}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch(
            tmdb_id=152532,
            release_title="Dallas Buyers Club 2013 1080p BluRay x264 SPARKS",
            language="EN",
        )
    assert result is None


@pytest.mark.asyncio
async def test_subdl_rejects_unsupported_language():
    source = SubDLSource(api_key="test-key")
    with pytest.raises(ValueError, match="language"):
        await source.fetch(tmdb_id=152532, release_title="Film", language="PT")
    await source.client.aclose()


@pytest.mark.asyncio
async def test_subdl_episode_uses_same_episode_and_web_source_when_exact_release_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json={
                "status": True,
                "results": [{"tmdb_id": 97546, "type": "tv"}],
                "subtitles": [{"language": "BR_PT", "season": 4, "episode": 6,
                               "unpack_files": [{
                                   "language": "BR_PT", "season": 4, "episode": 6,
                                   "release_name": "Slow.Horses.S04E06.1080p.WEB.CAKES",
                                   "format": "srt", "size": len(_SRT),
                                   "url": "/subtitle/123/abc",
                               }]}],
            })
        assert request.url.path == "/subtitle/123/abc"
        return httpx.Response(200, content=_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch(
            tmdb_id=97546,
            release_title="Slow.Horses.S04E06.2160p.ATVP.WEB-DL.Kitsune",
            season=4, episode=6,
        )
    assert result == _SRT


@pytest.mark.asyncio
async def test_subdl_prefers_exact_episode_release_over_same_source_fallback():
    exact = b"1\n00:00:01,000 --> 00:00:02,000\nExact!\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json={
                "status": True,
                "results": [{"tmdb_id": 97546, "type": "tv"}],
                "subtitles": [{"language": "BR_PT", "season": 4, "episode": 6,
                               "unpack_files": [{
                                   "language": "BR_PT", "season": 4, "episode": 6,
                                   "release_name": "Slow.Horses.S04E06.1080p.WEB.CAKES",
                                   "format": "srt", "size": len(_SRT),
                                   "url": "/subtitle/123/fallback",
                               }, {
                                   "language": "BR_PT", "season": 4, "episode": 6,
                                   "release_name": "Slow.Horses.S04E06.2160p.ATVP.WEB-DL.Kitsune",
                                   "format": "srt", "size": len(exact),
                                   "url": "/subtitle/123/exact",
                               }]}],
            })
        assert request.url.path == "/subtitle/123/exact"
        return httpx.Response(200, content=exact)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch(
            tmdb_id=97546,
            release_title="Slow.Horses.S04E06.2160p.ATVP.WEB-DL.Kitsune",
            season=4, episode=6,
        )
    assert result == exact


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [
    "Slow.Horses.S04E07.1080p.WEB.CAKES",
    "Slow.Horses.S04E06E07.1080p.WEB.CAKES",
    "Slow.Horses.S04E06.1080p.BluRay.CAKES",
    "Another.Show.S04E06.1080p.WEB.CAKES",
])
async def test_subdl_episode_fallback_rejects_different_episode_source_or_show(candidate):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "unsafe subtitle must not download"
        return httpx.Response(200, json={
            "status": True,
            "results": [{"tmdb_id": 97546, "type": "tv"}],
            "subtitles": [{"language": "BR_PT", "season": 4, "episode": 6,
                           "unpack_files": [{
                               "language": "BR_PT", "season": 4, "episode": 6,
                               "release_name": candidate,
                               "format": "srt", "size": len(_SRT),
                               "url": "/subtitle/123/abc",
                           }]}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch(
            tmdb_id=97546,
            release_title="Slow.Horses.S04E06.2160p.ATVP.WEB-DL.Kitsune",
            season=4, episode=6,
        )
    assert result is None
