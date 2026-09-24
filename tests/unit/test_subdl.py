from __future__ import annotations

import httpx
import pytest

from homeserver_control.worker.subdl import SubDLSource

_SRT = b"1\n00:00:01,000 --> 00:00:02,000\nOla!\n"
_SRT_CP1252 = "1\r\n00:00:01,000 --> 00:00:02,000\r\nAção!\r\n".encode("cp1252")
_MOVIE_SRT = (
    b"1\n00:00:01,000 --> 00:00:02,000\nHello!\n\n"
    b"2\n00:12:00,000 --> 00:12:03,000\nTwo\n\n"
    b"3\n00:24:00,000 --> 00:24:03,000\nThree\n\n"
    b"4\n00:36:00,000 --> 00:36:03,000\nFour\n\n"
    b"5\n00:48:00,000 --> 00:48:03,000\nFive\n\n"
    b"6\n01:00:00,000 --> 01:00:03,000\nSix\n\n"
    b"7\n01:12:00,000 --> 01:12:03,000\nSeven\n\n"
    b"8\n01:24:00,000 --> 01:24:03,000\nEight\n\n"
    b"9\n01:36:00,000 --> 01:36:03,000\nNine\n\n"
    b"10\n01:56:00,000 --> 01:56:03,000\nGoodbye!\n"
)


def _movie_payload(files, *, language="BR_PT", tmdb_id=152532, media_type="movie"):
    return {
        "status": True,
        "results": [{"tmdb_id": tmdb_id, "type": media_type}],
        "subtitles": [{"language": language, "unpack_files": files}],
    }


def _movie_file(*, name="Other.Release.2013.1080p.WEB", path="other", **extra):
    file = {
        "language": "BR_PT", "format": "srt", "size": len(_MOVIE_SRT),
        "release_name": name, "url": f"/subtitle/123/{path}",
    }
    file.update(extra)
    return file


@pytest.mark.asyncio
async def test_subdl_fetch_movie_exact_release_uses_verified_movie_identity():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.host == "api.subdl.com":
            assert request.url.params["tmdb_id"] == "152532"
            assert request.url.params["type"] == "movie"
            assert request.url.params["languages"] == "BR_PT"
            assert "season_number" not in request.url.params
            return httpx.Response(200, json={
                "status": True,
                "results": [{"tmdb_id": 152532, "type": "movie"}],
                "subtitles": [{"language": "BR_PT", "unpack_files": [
                    {"language": "BR_PT", "format": "srt", "size": len(_MOVIE_SRT),
                     "release_name": "Dallas.Buyers.Club.2013.EXTENDED.1080p.WEB.pt-BR",
                     "url": "/subtitle/123/different"},
                    {"language": "BR_PT", "format": "srt", "size": len(_MOVIE_SRT),
                     "release_name": "Dallas.Buyers.Club.2013.1080p.WEB.pt-BR",
                     "url": "/subtitle/123/exact?token=strip"},
                ]}],
            })
        assert request.url.host == "dl.subdl.com"
        assert request.url.path == "/subtitle/123/exact"
        assert not request.url.query
        return httpx.Response(200, content=_MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532,
            release_titles=["Dallas Buyers Club 2013 1080p WEB"],
        )

    assert result == _MOVIE_SRT
    assert requested == ["/api/v1/subtitles", "/subtitle/123/exact"]


@pytest.mark.asyncio
async def test_subdl_movie_exact_rejects_generic_stem_when_torrent_marks_extended():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "base cut must not download"
        return httpx.Response(200, json=_movie_payload([
            _movie_file(name="Dallas.Buyers.Club.2013.WEB")
        ]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532,
            release_titles=["Dallas.Buyers.Club.2013.WEB", "Dallas.Buyers.Club.2013.EXTENDED.WEB"],
        )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["en-US", "en_US"])
async def test_subdl_movie_exact_accepts_us_english_release_suffix(suffix):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            assert request.url.params["languages"] == "EN"
            return httpx.Response(200, json=_movie_payload([
                _movie_file(
                    name=f"Dallas.Buyers.Club.2013.WEB.{suffix}",
                    language="EN",
                )
            ], language="EN"))
        return httpx.Response(200, content=_MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            language="EN", match_mode="exact",
        )
    assert result == _MOVIE_SRT


@pytest.mark.asyncio
async def test_subdl_movie_exact_prefers_us_english_marker_to_generic_english():
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([
                _movie_file(name="Dallas.Buyers.Club.2013.WEB.en", language="EN", path="en"),
                _movie_file(
                    name="Dallas.Buyers.Club.2013.WEB.en-US", language="EN", path="en-us"
                ),
            ], language="EN"))
        fetched.append(request.url.path)
        return httpx.Response(200, content=_MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            language="EN", match_mode="exact",
        )
    assert result == _MOVIE_SRT
    assert fetched == ["/subtitle/123/en-us"]


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_accepts_other_release_from_verified_movie():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([_movie_file()]))
        assert request.url.path == "/subtitle/123/other"
        return httpx.Response(200, content=_MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch_movie(
            tmdb_id=152532,
            release_titles=["Dallas.Buyers.Club.2013.1080p.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result == _MOVIE_SRT


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_accepts_missing_release_name():
    file = _movie_file()
    del file["release_name"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([file]))
        return httpx.Response(200, content=_MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.1080p.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result == _MOVIE_SRT


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [None, 0, -1, float("nan"), float("inf")])
async def test_subdl_movie_same_duration_requires_valid_runtime(duration):
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid runtime must not initiate SubDL search")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.1080p.WEB"],
            match_mode="same_duration", movie_duration_seconds=duration,
        )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target, candidate", [
    ("Dallas.Buyers.Club.2013.1080p.WEB", "Dallas.Buyers.Club.2013.EXTENDED.WEB"),
    ("Dallas.Buyers.Club.2013.EXTENDED.WEB", "Dallas.Buyers.Club.2013.WEB"),
    ("Dallas.Buyers.Club.2013.DIRECTORS.CUT.WEB", "Dallas.Buyers.Club.2013.THEATRICAL.WEB"),
    ("Dallas.Buyers.Club.2013.UNRATED.WEB", "Dallas.Buyers.Club.2013.UNCUT.WEB"),
])
async def test_subdl_movie_same_duration_rejects_different_editions(target, candidate):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "incompatible cut must not download"
        return httpx.Response(200, json=_movie_payload([_movie_file(name=candidate)]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=[target],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result is None


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_accepts_same_special_edition():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([
                _movie_file(name="Dallas.Buyers.Club.2013.EXTENDED.1080p.BluRay")
            ]))
        return httpx.Response(200, content=_MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.EXTENDED.1080p.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result == _MOVIE_SRT


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_treats_theatrical_as_base_cut():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([
                _movie_file(name="Dallas.Buyers.Club.2013.THEATRICAL.BluRay")
            ]))
        return httpx.Response(200, content=_MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result == _MOVIE_SRT


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_uses_edition_from_any_release_title():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "base cut must not download"
        return httpx.Response(200, json=_movie_payload([
            _movie_file(name="Dallas.Buyers.Club.2013.1080p.WEB")
        ]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532,
            release_titles=["Dallas Buyers Club 2013", "Dallas.Buyers.Club.2013.EXTENDED.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    b"1\n01:56:00,000 --> 01:56:03,000\nOnly one cue\n",
    _MOVIE_SRT.replace(b"00:12:00,000", b"00:00:00,000"),
    _MOVIE_SRT.replace(b"01:56:", b"01:50:"),
    _MOVIE_SRT.replace(b"01:56:", b"02:02:"),
])
async def test_subdl_movie_same_duration_rejects_implausible_full_timeline(content):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([_movie_file(size=len(content))]))
        return httpx.Response(200, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result is None


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_rejects_sparse_two_cue_subtitle():
    sparse = (
        b"1\n00:00:01,000 --> 00:00:02,000\nFirst\n\n"
        b"2\n01:56:00,000 --> 01:56:03,000\nLast\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([_movie_file(size=len(sparse))]))
        return httpx.Response(200, content=sparse)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result is None


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_requires_cues_across_runtime():
    late_only = _MOVIE_SRT
    for old, new in (
        (b"00:00:", b"01:40:"), (b"00:12:", b"01:41:"),
        (b"00:24:", b"01:42:"), (b"00:36:", b"01:43:"),
        (b"00:48:", b"01:44:"), (b"01:00:", b"01:45:"),
        (b"01:12:", b"01:46:"), (b"01:24:", b"01:47:"),
        (b"01:36:", b"01:48:"),
    ):
        late_only = late_only.replace(old, new)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([_movie_file(size=len(late_only))]))
        return httpx.Response(200, content=late_only)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result is None


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_retries_after_bad_timeline():
    bad = _MOVIE_SRT.replace(b"01:56:", b"01:48:")
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([
                _movie_file(path="bad", size=len(bad)),
                _movie_file(name="Another.Release.2013.WEB", path="good"),
            ]))
        fetched.append(request.url.path)
        return httpx.Response(200, content=bad if request.url.path.endswith("bad") else _MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result == _MOVIE_SRT
    assert fetched == ["/subtitle/123/bad", "/subtitle/123/good"]


@pytest.mark.asyncio
async def test_subdl_movie_same_duration_prefers_source_and_fps_similarity():
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json=_movie_payload([
                _movie_file(name="Dallas.Buyers.Club.2013.BluRay", path="bluray", fps=23.976),
                _movie_file(name="Dallas.Buyers.Club.2013.WEBRip", path="webrip", fps=23.976),
                _movie_file(name="Dallas.Buyers.Club.2013.WEB.H264", path="wrong-fps", fps=30),
                _movie_file(name="Dallas.Buyers.Club.2013.WEB", path="web", fps=23.976),
            ]))
        fetched.append(request.url.path)
        return httpx.Response(200, content=_MOVIE_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB-DL.ETHEL"],
            match_mode="same_duration", movie_duration_seconds=7200, movie_fps=23.976,
        )
    assert result == _MOVIE_SRT
    assert fetched == ["/subtitle/123/web"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload_change, file_change", [
    ({"tmdb_id": 999}, {}),
    ({"media_type": "tv"}, {}),
    ({}, {"language": "EN"}),
    ({}, {"format": "zip"}),
    ({}, {"size": 1_000_001}),
    ({}, {"url": "https://attacker.example/subtitle/123/other"}),
    ({}, {"url": "http://[invalid"}),
])
async def test_subdl_movie_same_duration_keeps_identity_and_file_guards(
    payload_change, file_change
):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "untrusted file must not download"
        return httpx.Response(200, json=_movie_payload([
            _movie_file(**file_change)
        ], **payload_change))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("language, suffix", [("BR_PT", "en"), ("EN", "pt-BR")])
async def test_subdl_movie_same_duration_rejects_conflicting_language_suffix(language, suffix):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "wrong-language suffix must not download"
        return httpx.Response(200, json=_movie_payload([
            _movie_file(
                name=f"Dallas.Buyers.Club.2013.WEB.{suffix}", language=language,
            )
        ], language=language))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await SubDLSource(api_key="test-key", client=client).fetch_movie(
            tmdb_id=152532, release_titles=["Dallas.Buyers.Club.2013.WEB"],
            language=language, match_mode="same_duration", movie_duration_seconds=7200,
        )
    assert result is None


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
@pytest.mark.parametrize("language, suffix", [("EN", "en"), ("BR_PT", "pt-BR")])
async def test_subdl_movie_accepts_exact_release_with_language_suffix(language, suffix):
    release = "Dallas.Buyers.Club.2013.REPACK.1080p.BluRay.DD.5.1.X265-Ralphy"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.subdl.com":
            assert request.url.params["languages"] == language
            return httpx.Response(200, json={
                "status": True,
                "results": [{"tmdb_id": 152532, "type": "movie"}],
                "subtitles": [{"language": language, "unpack_files": [{
                    "language": language, "format": "srt", "size": len(_SRT),
                    "release_name": f"{release}.{suffix}",
                    "url": "/subtitle/123/abc",
                }]}],
            })
        assert request.url.host == "dl.subdl.com"
        return httpx.Response(200, content=_SRT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch(
            tmdb_id=152532,
            release_title="Dallas Buyers Club 2013 REPACK 1080p BluRay DD  5 1 X265-Ralphy",
            language=language,
        )
    assert result == _SRT


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate", [
    "Dallas.Buyers.Club.2013.REPACK.1080p.BluRay.DD.5.1.X264-Ralphy.en",
    "Dallas.Buyers.Club.2013.REPACK.1080p.BluRay.DD.5.1.X265-Other.en",
    "Dallas.Buyers.Club.2013.REPACK.1080p.BluRay.DD.5.1.X265-Ralphy.pt-BR",
])
async def test_subdl_movie_suffix_does_not_accept_different_release_or_language(candidate):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.subdl.com", "mismatched release must not download"
        return httpx.Response(200, json={
            "status": True,
            "results": [{"tmdb_id": 152532, "type": "movie"}],
            "subtitles": [{"language": "EN", "unpack_files": [{
                "language": "EN", "format": "srt", "size": len(_SRT),
                "release_name": candidate,
                "url": "/subtitle/123/abc",
            }]}],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="test-key", client=client)
        result = await source.fetch(
            tmdb_id=152532,
            release_title="Dallas Buyers Club 2013 REPACK 1080p BluRay DD  5 1 X265-Ralphy",
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
