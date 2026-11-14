import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from series_metadata_fixtures import ongoing_transport

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.series_acquisition import SeriesAcquirer


def _bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    return b"d" + b"".join(
        _bencode(key) + _bencode(item) for key, item in sorted(value.items())
    ) + b"e"


@pytest.mark.asyncio
@pytest.mark.parametrize("series", [False, True])
async def test_acquisition_prefers_original_language_from_native_media_context(tmp_path, series):
    repository = ReservationRepository(tmp_path / "control.sqlite")
    repository.initialize()
    media_key = "season:tmdb:101:1" if series else "movie:tmdb:101"
    reservation = repository.reserve(
        request_id="seerr:fixture", source_id="fixture", media_key=media_key,
        filesystem_id="fixture-fs", budget_bytes=20_000_000_000,
        free_bytes=100_000_000_000, total_bytes=200_000_000_000,
    )
    permits = PermitRegistry(repository.path)
    title = "Fixture S01E01" if series else "Fixture"
    torrents = {
        key: _bencode({b"info": {
            b"name": f"Fixture.{key}".encode(), b"piece length": 16_777_216,
            b"pieces": b"a" * (20 * 179),
            b"files": [
                {b"length": 3_000_000_000, b"path": [f"{title}.mkv".encode()]},
                {b"length": 1000, b"path": [f"{title}.pt-BR.srt".encode()]},
            ],
        }})
        for key in ("original", "dubbed")
    }
    grabbed = []

    def handler(request):
        path = request.url.path
        if path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if path in ("/api/v3/movie", "/api/v3/series"):
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "hasFile": False,
                "runtime": 45, "originalLanguage": {"id": 1, "name": "English"},
            }])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 11, "seasonNumber": 1, "episodeNumber": 1, "hasFile": False,
                "monitored": True,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }])
        if path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": key, "indexer": "UIndex", "title": title + " 1080p BluRay",
                "rejected": False,
                "size": inspect_torrent(torrent).total_bytes,
                "infoHash": inspect_torrent(torrent).infohash,
                "seeders": 2 if key == "original" else 40,
                "languages": [{"name": "English" if key == "original" else "French"}],
                "downloadUrl": f"http://prowlarr:9696/2/download?id={key}",
                "quality": {"quality": {
                    "source": "bluray", "modifier": "none", "resolution": 1080,
                }},
            } for key, torrent in torrents.items()])
        if path == "/2/download":
            return httpx.Response(200, content=torrents[request.url.params["id"]])
        if path == "/api/v3/release" and request.method == "POST":
            grabbed.append(json.loads(request.content)["guid"])
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"unexpected request {request.method} {path}")

    async with httpx.AsyncClient(transport=ongoing_transport(handler)) as client:
        options = dict(
            repository=repository, permits=permits, prowlarr_url="http://prowlarr:9696",
            client=client, release_policy=ReleasePolicy(minimum_mib_per_min=((1080, 20),)),
        )
        acquirer = (
            SeriesAcquirer(sonarr_url="http://sonarr:8989", sonarr_api_key="fixture", **options)
            if series
            else MovieAcquirer(radarr_url="http://radarr:7878", radarr_api_key="fixture", **options)
        )
        assert await acquirer.acquire(media_key, reservation.reservation_id) == "grabbed"
    assert grabbed == ["original"]


@pytest.mark.asyncio
@pytest.mark.parametrize("series", [False, True])
@pytest.mark.parametrize(
    "animation,offered,expected",
    [
        (True, ("dual", "dubbed", "original"), "dual"),
        (True, ("dubbed", "original"), "dubbed"),
        (True, ("original",), "original"),
        (True, ("empty_dual", "original"), "original"),
        (True, ("unknown_dual", "original"), "original"),
        (True, ("tiny_dual", "original"), "original"),
        (True, ("foreign",), None),
        (True, ("unknown_audio",), None),
        (False, ("dual", "dubbed", "original"), "original"),
    ],
)
async def test_animation_audio_profile_reaches_movie_and_episode_selection(
    tmp_path, series, animation, offered, expected,
):
    """Catch loss of the media genre context before initial torrent admission."""
    repository = ReservationRepository(tmp_path / "control.sqlite")
    repository.initialize()
    media_key = "season:tmdb:101:1" if series else "movie:tmdb:101"
    reservation = repository.reserve(
        request_id="seerr:animation", source_id="animation", media_key=media_key,
        filesystem_id="fixture-fs", budget_bytes=20_000_000_000,
        free_bytes=100_000_000_000, total_bytes=200_000_000_000,
    )
    permits = PermitRegistry(repository.path)
    title = "Fixture S01E01" if series else "Fixture"
    torrents = {
        key: _bencode({b"info": {
            b"name": f"Animation.{key}".encode(), b"piece length": 16_777_216,
            b"pieces": b"a" * (20 * (24 if key == "tiny_dual" else 179)),
            b"files": [
                {b"length": 400_000_000 if key == "tiny_dual" else 3_000_000_000,
                 b"path": [f"{title}.mkv".encode()]},
                {b"length": 1000, b"path": [f"{title}.pt-BR.srt".encode()]},
            ],
        }})
        for key in offered
    }
    languages = {
        "dual": ["pt-BR", "en"], "dubbed": ["pt-BR"], "original": ["en"],
        "empty_dual": ["pt-BR", "en"], "tiny_dual": ["pt-BR", "en"],
        "unknown_dual": ["pt-BR", "en"],
        "foreign": ["fr"], "unknown_audio": [],
    }
    grabbed = []
    native_profile = 8
    native_profile_writes = []

    def handler(request):
        nonlocal native_profile
        path = request.url.path
        if path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if path in ("/api/v3/movie", "/api/v3/series"):
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "hasFile": False,
                "runtime": 45, "originalLanguage": {"name": "English"},
                "genres": ["Animation", "Family"] if animation else ["Family", "Drama"],
                "qualityProfileId": native_profile,
            }])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[
                {"id": 8, "name": "HomeServer", "language": {"id": -2, "name": "Original"}},
                {"id": 9, "name": "HomeServer Animation", "language": {"id": -1, "name": "Any"}},
            ])
        if path == "/api/v3/movie/1":
            if request.method == "PUT":
                body = json.loads(request.content)
                native_profile = body["qualityProfileId"]
                native_profile_writes.append(body)
            return httpx.Response(200, json={
                "id": 1, "tmdbId": 101, "qualityProfileId": native_profile,
            })
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 11, "seasonNumber": 1, "episodeNumber": 1, "hasFile": False,
                "monitored": True,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }])
        if path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": key, "indexer": "1337x" if key == "dual" else "UIndex",
                "title": title + " 1080p BluRay", "rejected": False,
                "size": inspect_torrent(torrent).total_bytes,
                "infoHash": inspect_torrent(torrent).infohash,
                "seeders": {
                    "dual": 5, "dubbed": 30, "original": 100,
                    "empty_dual": 0, "tiny_dual": 100, "unknown_dual": None,
                    "foreign": 1000, "unknown_audio": 1000,
                }[key],
                "languages": languages[key],
                "downloadUrl": f"http://prowlarr:9696/2/download?id={key}",
                "quality": {"quality": {
                    "source": "bluray", "modifier": "none", "resolution": 1080,
                }},
            } for key, torrent in torrents.items()])
        if path == "/2/download":
            return httpx.Response(200, content=torrents[request.url.params["id"]])
        if path == "/api/v3/release" and request.method == "POST":
            grabbed.append(json.loads(request.content)["guid"])
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"unexpected request {request.method} {path}")

    async with httpx.AsyncClient(transport=ongoing_transport(handler)) as client:
        options = dict(
            repository=repository, permits=permits, prowlarr_url="http://prowlarr:9696",
            client=client, release_policy=ReleasePolicy(minimum_mib_per_min=((1080, 20),)),
        )
        acquirer = (
            SeriesAcquirer(sonarr_url="http://sonarr:8989", sonarr_api_key="fixture", **options)
            if series else MovieAcquirer(
                radarr_url="http://radarr:7878", radarr_api_key="fixture", **options,
            )
        )
        assert await acquirer.acquire(media_key, reservation.reservation_id) == (
            "grabbed" if expected else "no_eligible_release"
        )
    assert grabbed == ([expected] if expected else [])
    if animation and not series:
        assert native_profile == 9
        assert len(native_profile_writes) == 1
        assert native_profile_writes[0]["genres"] == ["Animation", "Family"]
    else:
        assert native_profile == 8 and not native_profile_writes


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_animation_audio_profile_reaches_initial_and_manual_season_pack(tmp_path, manual):
    """A full-season search must carry the same audio preference as episodes."""
    from test_series_pack_acquisition import SeriesFixture

    from homeserver_control.worker.capacity_evidence import CapacityEvidence
    from homeserver_control.worker.source_health import SourceHealthStore

    fixture = SeriesFixture(tmp_path, episode_count=2)
    old = None
    added = []
    paused = set()

    def handler(request):
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "runtime": 45, "status": "ended",
                "genres": ["Animation"], "originalLanguage": {"name": "English"},
            }])
        if request.url.path == "/internal/torrent-health":
            source = fixture.permits.get(request.headers["X-Admission-Permit"])
            return httpx.Response(200, json={
                "hash": source.infohash, "downloaded": 100,
                "amount_left": source.budget_bytes - 100,
                "state": "stoppedDL" if source.infohash in paused else "stalledDL",
                "num_seeds": 0, "dlspeed": 0, "progress": .01,
                "name": "Fixture S01 1080p WEB-DL",
            })
        if request.url.path == "/internal/source-state":
            body = json.loads(request.content)
            source = fixture.permits.get(body["permit_token"])
            (paused.add if body["action"] == "stop" else paused.discard)(source.infohash)
        if request.url.path == "/api/v2/torrents/add":
            source = fixture.permits.get(request.headers["X-Admission-Permit"])
            fixture.confirm(source)
            added.append(source.infohash)
            return httpx.Response(200, json={"accepted": True})
        return fixture.handler(request)

    async def capacity():
        return CapacityEvidence(
            fixture.free_bytes, fixture.remaining, paused_hashes=frozenset(paused),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = fixture.acquirer(
            client, prefer_season_pack=True, health_store=SourceHealthStore(fixture.repo.path),
        )
        acquirer.capacity_provider = capacity
        if manual:
            fixture.pack_offers = [fixture.release("old-pack", [1, 2], seeds=10)]
            fixture.pack_offers[0]["languages"] = ["en"]
            assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
            old = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
        dual = fixture.release("dual-pack", [1, 2], seeds=10)
        dual["languages"] = ["pt-BR", "en"]
        original = fixture.release("original-pack", [1, 2], seeds=100)
        original["languages"] = ["en"]
        fixture.pack_offers = [original, dual]
        if manual:
            assert await acquirer.manual_replace(
                "season:tmdb:101:1", fixture.reservation, old.permit_id, scope_key="S01PACK",
            ) == "replaced"
        else:
            assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
    source = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
    assert source.infohash == inspect_torrent(fixture.torrents["dual-pack"]).infohash
    assert fixture.posts == (["old-pack"] if manual else ["dual-pack"])
    assert len(added) == int(manual)


@pytest.mark.asyncio
@pytest.mark.parametrize("episode_variant", ["dual", "empty_dual", "tiny_dual"])
@pytest.mark.parametrize("named_dubbed", [False, True])
async def test_complete_animation_season_preserves_original_pack_fallback_over_dual_singles(
    tmp_path, episode_variant, named_dubbed,
):
    from test_series_pack_acquisition import SeriesFixture

    fixture = SeriesFixture(tmp_path, episode_count=2)
    pack = fixture.release("original-pack", [1, 2], seeds=100)
    pack["languages"] = ["en"]
    fixture.pack_offers = [pack]
    for number in (1, 2):
        release = fixture.release(
            f"{episode_variant}-{number}", [number],
            seeds=0 if episode_variant == "empty_dual" else 100,
            video_bytes=400_000_000 if episode_variant == "tiny_dual" else 1_500_000_000,
        )
        release["languages"] = ["pt-BR", "en"]
        fixture.episode_offers[number] = [release]

    def handler(request):
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "runtime": 45, "status": "ended",
                "genres": [] if named_dubbed else ["Animation"],
                "originalLanguage": {"name": "English"}, "qualityProfileId": 3,
            }])
        if request.url.path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 3, "name": "Dublado"}])
        return fixture.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = fixture.acquirer(client, prefer_season_pack=True)
        if named_dubbed:
            acquirer.release_policy = ReleasePolicy.from_environment({})
        assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
        if episode_variant == "dual":
            assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == (
                "waiting_episodes"
            )
    assert fixture.posts == ["original-pack"]
