"""Native profile selection controls real candidate admission for either media kind."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_audio_language_acquisition import _bencode

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.series_acquisition import SeriesAcquirer

PROFILES = [
    {"id": 1, "name": "Max Quality"},
    {"id": 2, "name": "Standard"},
    {"id": 3, "name": "Dublado"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("series", [False, True])
@pytest.mark.parametrize("animation", [False, True])
@pytest.mark.parametrize("profile_id,expected", [
    (1, "uhd-remux"), (2, "fhd-bluray"), (3, "dual-web"), (99, None),
])
async def test_selected_profile_controls_movies_and_episodes(
    tmp_path, series, animation, profile_id, expected,
):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    key = "season:tmdb:101:1" if series else "movie:tmdb:101"
    reservation = repo.reserve(
        request_id="fixture", source_id="fixture", media_key=key,
        filesystem_id="fixture", budget_bytes=20_000_000_000, free_bytes=100_000_000_000,
        total_bytes=200_000_000_000,
    ).reservation_id
    offers = {
        "few-seeds": (2160, "bluray", "remux", ["en"], 1),
        "uhd-remux": (2160, "bluray", "remux", ["en"], 5),
        "fhd-remux": (1080, "bluray", "remux", ["en"], 500),
        "fhd-bluray": (1080, "bluray", "none", ["en"], 10),
        "fhd-web": (1080, "webdl", "none", ["en"], 1000),
        "dual-web": (1080, "webdl", "none", ["pt-BR", "en"], 5),
    }
    torrents = {}
    releases = []
    for name, (resolution, source, modifier, languages, seeds) in offers.items():
        title = f"Fixture {'S01E01 ' if series else ''}{resolution}p "
        title += "BluRay REMUX" if modifier == "remux" else (
            "BluRay" if source == "bluray" else "WEB-DL"
        )
        torrents[name] = _bencode({b"info": {
            b"name": name.encode(), b"piece length": 16_777_216, b"pieces": b"a" * 3580,
            b"files": [
                {b"length": 3_000_000_000, b"path": [(title + ".mkv").encode()]},
                {b"length": 1000, b"path": [(title + ".pt-BR.srt").encode()]},
            ],
        }})
        parsed = inspect_torrent(torrents[name])
        releases.append({
            "guid": name, "title": title, "indexer": "UIndex", "indexerId": 1,
            "rejected": False, "size": parsed.total_bytes, "infoHash": parsed.infohash,
            "seeders": seeds, "languages": languages, "episodeIds": [11],
            "downloadUrl": f"http://prowlarr:9696/1/download?id={name}",
            "quality": {"quality": {
                "resolution": resolution, "source": source, "modifier": modifier,
            }},
        })
    grabbed, writes = [], []

    def handler(request):
        path = request.url.path
        if request.method == "PUT":
            writes.append(path)
            raise AssertionError("Search must preserve the explicitly selected profile")
        if path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if path in ("/api/v3/movie", "/api/v3/series"):
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "hasFile": False,
                "qualityProfileId": profile_id, "runtime": 45,
                "originalLanguage": {"name": "English"},
                "genres": ["Animation"] if animation else ["Drama"],
            }])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=PROFILES)
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 11, "seasonNumber": 1, "episodeNumber": 1, "hasFile": False,
                "monitored": True,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }, {"id": 12, "seasonNumber": 1, "episodeNumber": 2, "hasFile": False,
                "monitored": False,
                "airDateUtc": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            }])
        if path == "/api/v3/release":
            if request.method == "POST":
                grabbed.append(json.loads(request.content)["guid"])
                return httpx.Response(200, json={"accepted": True})
            return httpx.Response(200, json=releases)
        if path == "/1/download":
            return httpx.Response(200, content=torrents[request.url.params["id"]])
        raise AssertionError(f"Unexpected request {request.method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        options = dict(
            repository=repo, permits=PermitRegistry(repo.path), client=client,
            prowlarr_url="http://prowlarr:9696", release_policy=ReleasePolicy.from_environment({}),
        )
        acquirer = (
            SeriesAcquirer(sonarr_url="http://sonarr:8989", sonarr_api_key="fixture", **options)
            if series else
            MovieAcquirer(radarr_url="http://radarr:7878", radarr_api_key="fixture", **options)
        )
        result = await acquirer.acquire(key, reservation)
        assert result == ("grabbed" if expected else "quality_profile_unavailable")
        assert grabbed == ([expected] if expected else [])
        assert not writes
        assert acquirer.release_policy.profile_name == "Standard"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", [1, 2, 3])
async def test_named_profiles_apply_to_full_season_packs(tmp_path, profile_id):
    from test_series_pack_acquisition import SeriesFixture, _encode

    fixture = SeriesFixture(tmp_path, episode_count=2)
    offers = [
        ("uhd", 2160, "blurayRaw", ["en"]),
        ("bluray", 1080, "bluray", ["en"]),
        ("dual", 1080, "web", ["pt-BR", "en"]),
    ]
    for name, resolution, source, languages in offers:
        release = fixture.release(name, [1, 2], video_bytes=3_000_000_000)
        release["quality"]["quality"].update(
            resolution=resolution, source=source,
            name=f"{'WEBDL' if source == 'web' else 'Bluray'}-{resolution}p",
        )
        release.update(languages=languages)
        if resolution == 2160:
            release["title"] = "Fixture S01 2160p BluRay REMUX"
            files = [
                {b"path": [f"Fixture.S01E{number:02d}.2160p.mkv".encode()],
                 b"length": 3_000_000_000} for number in (1, 2)
            ] + [
                {b"path": [f"Fixture.S01E{number:02d}.pt-BR.srt".encode()],
                 b"length": 1000} for number in (1, 2)
            ]
            fixture.torrents[name] = _encode({b"info": {
                b"name": name.encode(), b"files": files, b"piece length": 16_777_216,
                b"pieces": b"a" * 7160,
            }})
            parsed = inspect_torrent(fixture.torrents[name])
            release.update(infoHash=parsed.infohash, size=parsed.total_bytes)
        fixture.pack_offers.append(release)

    def handler(request):
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "runtime": 45,
                "qualityProfileId": profile_id, "originalLanguage": {"name": "English"},
                "status": "ended",
            }])
        if request.url.path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=PROFILES)
        return fixture.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = fixture.acquirer(client, prefer_season_pack=True)
        acquirer.release_policy = ReleasePolicy.from_environment({})
        assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
    assert fixture.posts == [{1: "uhd", 2: "bluray", 3: "dual"}[profile_id]]


@pytest.mark.asyncio
async def test_season_decision_cache_is_scoped_to_selected_profile(tmp_path):
    from test_series_pack_acquisition import SeriesFixture

    fixture = SeriesFixture(tmp_path, episode_count=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        acquirer = fixture.acquirer(client)
        acquirer.release_policy = ReleasePolicy.from_environment({}).for_profile("Max Quality")
        await acquirer._season_releases(1, 1)
        acquirer.release_policy = acquirer.release_policy.for_profile("Standard")
        await acquirer._season_releases(1, 1)
    assert fixture.searched == ["pack", "pack"]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_id", [1, 2, 3])
async def test_completed_season_pack_precedes_better_quality_singles(tmp_path, profile_id):
    from test_series_pack_acquisition import SeriesFixture

    fixture = SeriesFixture(tmp_path, episode_count=2)
    pack = fixture.release("web-pack", [1, 2], seeds=100)
    pack["languages"] = ["pt-BR", "en"]
    fixture.pack_offers = [pack]
    for number in (1, 2):
        release = fixture.release(f"bluray-{number}", [number], seeds=10)
        release["languages"] = ["pt-BR", "en"]
        release["quality"]["quality"].update(source="bluray", name="Bluray-1080p")
        fixture.episode_offers[number] = [release]

    def handler(request):
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "runtime": 45,
                "qualityProfileId": profile_id, "originalLanguage": {"name": "English"},
                "status": "ended",
            }])
        if request.url.path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=PROFILES)
        return fixture.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = fixture.acquirer(client, prefer_season_pack=True)
        acquirer.release_policy = ReleasePolicy.from_environment({})
        assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
    assert fixture.posts == ["web-pack"]


@pytest.mark.asyncio
@pytest.mark.parametrize("target_profile", [2, 3])
async def test_manual_pack_change_honors_new_profile_instead_of_old_remux(tmp_path, target_profile):
    from test_series_pack_acquisition import SeriesFixture

    from homeserver_control.worker.capacity_evidence import CapacityEvidence
    from homeserver_control.worker.source_health import SourceHealthStore

    fixture = SeriesFixture(tmp_path, episode_count=2)
    old_release = fixture.release("old-remux", [1, 2])
    old_release.update(title="Fixture S01 1080p BluRay REMUX", languages=["en"])
    old_release["quality"]["quality"].update(source="blurayRaw", name="Bluray-1080p Remux")
    fixture.pack_offers = [old_release]
    profile_id = 1
    paused, added = set(), []

    def handler(request):
        path = request.url.path
        if path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "runtime": 45,
                "qualityProfileId": profile_id, "originalLanguage": {"name": "English"},
                "status": "ended",
            }])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=PROFILES)
        if path == "/internal/torrent-health":
            source = fixture.permits.get(request.headers["X-Admission-Permit"])
            return httpx.Response(200, json={
                "hash": source.infohash, "downloaded": 100,
                "amount_left": source.budget_bytes - 100,
                "state": "stalledDL", "num_seeds": 0, "dlspeed": 0, "progress": .01,
                "name": "Fixture S01 1080p BluRay REMUX",
            })
        if path == "/internal/source-state":
            body = json.loads(request.content)
            source = fixture.permits.get(body["permit_token"])
            (paused.add if body["action"] == "stop" else paused.discard)(source.infohash)
        if path == "/api/v2/torrents/add":
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
        acquirer.release_policy = ReleasePolicy.from_environment({})
        assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
        old = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
        profile_id = target_profile
        replacement = fixture.release("new-web", [1, 2], seeds=100)
        replacement["languages"] = ["pt-BR", "en"]
        fixture.pack_offers = [replacement]
        acquirer.capacity_provider = capacity
        assert await acquirer.manual_replace(
            "season:tmdb:101:1", fixture.reservation, old.permit_id, scope_key="S01PACK",
        ) == "replaced"
    assert len(added) == 1 and paused == {old.infohash}
