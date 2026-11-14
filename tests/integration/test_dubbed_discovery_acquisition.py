"""Supplemental sources are dispatched only through native Arr decisions and permits."""

import json

import httpx
import pytest
from test_movie_acquisition import _reserve, _torrent, _transport

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.dubbed_discovery import DiscoveryResult, DubbedDiscovery
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.series_acquisition import SeriesAcquirer


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True, "invalid"])
@pytest.mark.parametrize("audio_source", ["title", "description"])
async def test_supplemental_movie_preserves_native_rejection_and_capacity(
    tmp_path, rejected, audio_source,
):
    repository, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(subtitle=True, video_bytes=3_000_000_000)
    manifest = inspect_torrent(torrent)
    movie = {
        "id": 10,
        "tmdbId": 1101383,
        "title": "Fixture",
        "year": 2024,
        "qualityProfileId": 3,
        "hasFile": False,
        "runtime": 90,
        "originalLanguage": {"name": "English"},
    }
    pushed = []
    release_title = "Fixture.2024.1080p.BluRay." + (
        "DUAL.AUDIO.PTBR.ENG" if audio_source == "title" else "MULTi"
    )

    def handler(request):
        path = request.url.path
        if request.url.host == "uindex.org":
            assert request.method == "GET"
            assert "X-Api-Key" not in request.headers
            return httpx.Response(
                200,
                text='<div id="descrBody">Audio #1: English<br>'
                'Audio #2: Portuguese [Brazilian]<br>Text #1: Japanese</div>',
            )
        if path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if path == "/api/v3/movie":
            return httpx.Response(200, json=[movie])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 3, "name": "Dublado"}])
        if path == "/api/v3/release":
            assert request.method == "GET", "Raw Prowlarr results cannot use Arr cache dispatch"
            return httpx.Response(200, json=[])
        if path == "/api/v1/indexer":
            return httpx.Response(
                200,
                json=[
                    {"id": 6, "name": "UIndex", "enable": True},
                    {"id": 5, "name": "1337x", "enable": True},
                ],
            )
        if path == "/api/v1/indexerstatus":
            return httpx.Response(200, json=[])
        if path == "/api/v3/indexer":
            return httpx.Response(200, json=[{"id": 16, "name": "UIndex (Prowlarr)"}])
        if path == "/api/v1/search":
            return httpx.Response(
                200,
                json=[
                    {
                        "title": release_title,
                        "guid": "extra",
                        "indexer": "UIndex",
                        "indexerId": 6,
                        "seeders": 100,
                        "infoHash": manifest.infohash,
                        "size": manifest.total_bytes,
                        "protocol": "torrent",
                        "publishDate": "2026-01-01T00:00:00Z",
                        "downloadUrl": "http://prowlarr:9696/6/download",
                        **({"infoUrl": "https://uindex.org/details.php?id=10"}
                           if audio_source == "description" else {}),
                    }
                ]
                if request.url.params["indexerIds"] == "6"
                else [],
            )
        if path == "/api/v3/parse":
            return httpx.Response(
                200,
                json={
                    "movie": movie,
                    "parsedMovieInfo": {
                        "year": 2024,
                        "languages": [{"name": "Unknown"}],
                        "quality": {
                            "quality": {"resolution": 1080, "source": "bluray", "modifier": "none"}
                        },
                    },
                },
            )
        if path == "/6/download":
            return httpx.Response(200, content=torrent)
        if path == "/api/v3/release/push":
            permit = permits.get_for_reservation(reservation_id)
            assert permit is not None and permit.budget_bytes == manifest.total_bytes
            assert permit.infohash == manifest.infohash
            pushed.append(json.loads(request.content))
            if rejected == "invalid":
                return httpx.Response(400, json=[{"errorMessage": "Unable to parse"}])
            return httpx.Response(
                200,
                json=[{"rejected": rejected, "rejections": ["Blocklisted"] if rejected else []}],
            )
        raise AssertionError(str(request.url))

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        acquirer = MovieAcquirer(
            repository=repository,
            permits=permits,
            radarr_url="http://radarr:7878",
            radarr_api_key="private",
            prowlarr_url="http://prowlarr:9696",
            client=client,
            release_policy=ReleasePolicy.from_environment({}),
            dubbed_discovery=discovery,
        )
        outcome = await acquirer.acquire("movie:tmdb:1101383", reservation_id)
    assert outcome == ("no_eligible_release" if rejected else "grabbed")
    assert len(pushed) == 1
    assert "requiresNativeDecision" not in pushed[0]
    if rejected:
        assert permits.get_for_reservation(reservation_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pack", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_series_discovery_dispatches_episode_or_pack_with_exact_identity(
    tmp_path, pack, fallback,
):
    from test_series_pack_acquisition import SeriesFixture

    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.ongoing = not pack
    numbers = [1, 2] if pack else [1]
    candidate = fixture.release("brazil", numbers)
    candidate.update(
        title=candidate["title"] + ".DUAL.AUDIO.PTBR.ENG",
        protocol="torrent",
        publishDate="2026-01-01T00:00:00Z",
    )
    media = {
        "id": 1,
        "tmdbId": 101,
        "title": "Fixture",
        "year": 2024,
        "qualityProfileId": 3,
        "status": "ended" if pack else "continuing",
        "monitored": True,
        "runtime": 45,
        "originalLanguage": {"name": "English"},
    }

    def handler(request):
        path = request.url.path
        if path == "/api/v3/series":
            return httpx.Response(200, json=[media])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 3, "name": "Dublado"}])
        if path == "/api/v1/indexer":
            return httpx.Response(
                200,
                json=[
                    {"id": 6, "name": "UIndex", "enable": True},
                    {"id": 5, "name": "1337x", "enable": True},
                ],
            )
        if path == "/api/v1/indexerstatus":
            return httpx.Response(200, json=[])
        if path == "/api/v3/indexer":
            return httpx.Response(200, json=[{"id": 1, "name": "UIndex (Prowlarr)"}])
        if path == "/api/v1/search":
            return httpx.Response(
                200, json=[candidate] if request.url.params["indexerIds"] == "6" else []
            )
        if path == "/api/v3/parse":
            return httpx.Response(
                200,
                json={
                    "series": media,
                    "episodes": [
                        {"id": number, "episodeNumber": number, "seasonNumber": 1, "seriesId": 1}
                        for number in numbers
                    ],
                    "parsedEpisodeInfo": {
                        "seasonNumber": 1,
                        "fullSeason": pack,
                        "languages": [{"name": "Unknown"}],
                        "quality": candidate["quality"],
                    },
                },
            )
        if path == "/api/v3/release/push":
            scope = "S01PACK" if pack else "S01E01"
            permit = fixture.permits.get_for_reservation(fixture.reservation, scope_key=scope)
            assert permit is not None
            fixture.confirm(permit)
            fixture.posts.append(json.loads(request.content)["guid"])
            return httpx.Response(200, json=[{"rejected": False}])
        return fixture.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        acquirer = fixture.acquirer(client, prefer_season_pack=pack, dubbed_discovery=discovery)
        acquirer.release_policy = ReleasePolicy.from_environment(
            {"HOMESERVER_RELEASE_FALLBACK_INDEXERS": "1337x"} if fallback else {},
        )
        assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
    assert fixture.posts == ["brazil"]


@pytest.mark.asyncio
async def test_incomplete_search_uses_only_proven_dual_despite_custom_preference_order(tmp_path):
    repository, permits, _ = _reserve(tmp_path)
    quality = {"quality": {"source": "bluray", "resolution": 1080, "modifier": "none"}}
    rows = [
        {
            "title": "Fixture 1080p BluRay",
            "guid": "original",
            "languages": ["en"],
            "quality": quality,
            "seeders": 10,
            "size": 3_000_000_000,
        },
        {
            "title": "Fixture 1080p BluRay DUAL AUDIO PTBR ENG",
            "guid": "dual",
            "languages": ["pt-br", "en"],
            "quality": quality,
            "seeders": 10,
            "size": 3_000_000_000,
        },
        {
            "title": "Fixture 1080p BluRay PTBR",
            "guid": "brazil",
            "languages": ["pt-br"],
            "quality": quality,
            "seeders": 10,
            "size": 3_000_000_000,
        },
    ]

    class UnavailableDiscovery:
        async def enrich(self, *args, **kwargs):
            return DiscoveryResult(rows, False)

    async with httpx.AsyncClient() as client:
        acquirer = MovieAcquirer(
            repository=repository,
            permits=permits,
            radarr_url="http://radarr:7878",
            radarr_api_key="private",
            prowlarr_url="http://prowlarr:9696",
            client=client,
            release_policy=ReleasePolicy.from_environment(
                {
                    "HOMESERVER_DOWNLOAD_DEFAULT_PROFILE": "Dublado",
                    "HOMESERVER_DUBBED_AUDIO_PREFERENCES": "original,dual,pt-BR",
                }
            ),
            dubbed_discovery=UnavailableDiscovery(),
        )
        selected = await acquirer._discover_dubbed_releases(
            {"id": 1, "originalLanguage": "en"},
            [],
        )
    assert [row["guid"] for row in selected] == ["dual"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["movie", "episode", "pack"])
async def test_unreviewed_supplemental_source_cannot_bypass_arr_via_replacement(tmp_path, kind):
    repository, permits, _ = _reserve(tmp_path)
    row = {
        "title": "Fixture.S01E01.1080p.BluRay.DUAL.AUDIO.PTBR.ENG",
        "rejected": False,
        "requiresNativeDecision": True,
        "size": 3_000_000_000,
        "seeders": 30,
        "quality": {"quality": {"resolution": 1080, "source": "bluray", "modifier": "none"}},
        "languages": ["pt-br", "en"],
        "downloadUrl": "http://prowlarr:9696/1/download",
    }

    def handler(request):
        raise AssertionError("Supplemental replacement must not reach torrent fetch or gateway")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        common = dict(
            repository=repository,
            permits=permits,
            prowlarr_url="http://prowlarr:9696",
            client=client,
            release_policy=ReleasePolicy.from_environment(
                {
                    "HOMESERVER_DOWNLOAD_DEFAULT_PROFILE": "Dublado",
                }
            ),
        )
        if kind == "movie":
            acquirer = MovieAcquirer(radarr_url="http://radarr", radarr_api_key="private", **common)
            candidates = acquirer._eligible_movie_releases(
                [row],
                replacement_reason="stalled",
                excluded_infohashes=set(),
                runtime_minutes=90,
                rank=lambda item: acquirer._rank(item, original_language="en"),
            )
        else:
            acquirer = SeriesAcquirer(
                sonarr_url="http://sonarr", sonarr_api_key="private", **common
            )
            if kind == "episode":
                candidates = acquirer._inspect_episode_releases(
                    [row],
                    season=1,
                    episode=1,
                    replacement_reason="stalled",
                    excluded_infohashes=set(),
                    runtime_minutes=45,
                    rank=lambda item: acquirer._rank(item, original_language="en"),
                )
            else:
                acquirer._manual_permit_id = "old"
                candidates = acquirer._inspect_pack_releases(
                    [row],
                    season=1,
                    episode_runtimes={1: 45},
                    original_language="en",
                    rank=lambda item: acquirer._rank(item, original_language="en"),
                )
        assert [value async for value in candidates] == []
