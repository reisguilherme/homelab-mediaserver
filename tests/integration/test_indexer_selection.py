import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.series_acquisition import SeriesAcquirer


def _encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    return b"d" + b"".join(
        _encode(key) + _encode(item) for key, item in sorted(value.items())
    ) + b"e"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["movie", "series"])
@pytest.mark.parametrize("fallback_threshold", [5, 20])
@pytest.mark.parametrize(
    "offers,expected,inspected_names",
    [
        ([('primary', 'UIndex (Prowlarr)', 1080, 10, 2_000_000_000),
          ('backup', '1337x', 1080, 100, 2_000_000_000)], 'primary', ['primary']),
        ([('primary', 'UIndex', 1080, 2, 2_000_000_000),
          ('backup', '1337x (Prowlarr)', 1080, 20, 2_000_000_000)], 'backup',
         ['primary', 'backup']),
        ([('bad', 'UIndex', 1080, 100, 400_000_000),
          ('backup', '1337x', 1080, 10, 2_000_000_000)], 'backup', ['bad', 'backup']),
        ([('denied', 'BitSearch', 1080, 100, 2_000_000_000)], None, []),
        ([('unknown', None, 1080, 100, 2_000_000_000)], None, []),
        ([('unknown-seeds', 'UIndex.org', 1080, None, 2_000_000_000),
          ('backup', '1337x', 1080, 10, 2_000_000_000)], 'backup',
         ['unknown-seeds', 'backup']),
    ],
)
async def test_movie_and_series_choose_uindex_then_seed_fallback_after_real_quality_validation(
    tmp_path, kind, fallback_threshold, offers, expected, inspected_names,
):
    if expected == "primary" and fallback_threshold == 20:
        expected, inspected_names = "backup", ["primary", "backup"]
    status, posted, inspected_names_actual = await _choose_indexer(
        tmp_path, kind=kind, fallback_threshold=fallback_threshold, offers=offers,
    )
    assert status == ("grabbed" if expected else "no_eligible_release")
    assert posted == ([expected] if expected else [])
    assert inspected_names_actual == inspected_names


async def _choose_indexer(tmp_path, *, kind, fallback_threshold, offers):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    media_key = "season:tmdb:101:1" if kind == "series" else "movie:tmdb:101"
    reserved = repo.reserve(
        request_id="fixture", source_id="fixture", media_key=media_key,
        filesystem_id="fixture", budget_bytes=0, free_bytes=50_000_000_000,
        total_bytes=100_000_000_000,
    )
    permits = PermitRegistry(repo.path)
    title = "Fixture.S01E01" if kind == "series" else "Fixture"
    torrents = {}
    releases = []
    for name, indexer, resolution, seeds, video_bytes in offers:
        torrents[name] = _encode({b"info": {
            b"name": name.encode(), b"piece length": 16_777_216,
            b"pieces": b"a" * (20 * ((video_bytes + 1000 + 16_777_215) // 16_777_216)),
            b"files": [
                {b"path": [f"{title}.{name}.mkv".encode()], b"length": video_bytes},
                {b"path": [f"{title}.{name}.pt-BR.srt".encode()], b"length": 1000},
            ],
        }})
        metadata = inspect_torrent(torrents[name])
        releases.append({
            "guid": name, "indexer": indexer, "indexerId": 1, "seeders": seeds,
            "title": f"{title} {resolution}p WEB-DL", "rejected": False,
            "size": metadata.total_bytes, "infoHash": metadata.infohash,
            "downloadUrl": f"http://prowlarr:9696/1/download?id={name}",
            "languages": [{"name": "English"}],
            "quality": {"quality": {
                "source": "web" if kind == "series" else "webdl",
                "modifier": "none", "resolution": resolution, "name": f"WEBDL-{resolution}p",
            }},
        })
    inspected_names_actual, posted = [], []

    def handler(request):
        path = request.url.path
        if path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if path in ("/api/v3/movie", "/api/v3/series"):
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "hasFile": False, "runtime": 45,
                "originalLanguage": {"name": "English"},
            }])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 1, "seasonNumber": 1, "episodeNumber": 1, "monitored": True,
                "hasFile": False, "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }])
        if path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=releases)
        if path == "/1/download":
            name = request.url.params["id"]
            inspected_names_actual.append(name)
            return httpx.Response(200, content=torrents[name])
        if path == "/api/v3/release" and request.method == "POST":
            posted.append(json.loads(request.content)["guid"])
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def capacity():
        return CapacityEvidence(free_bytes=50_000_000_000, remaining_by_hash={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        environment = (
            {} if fallback_threshold is None else
            {"HOMESERVER_INDEXER_FALLBACK_MIN_SEEDERS": str(fallback_threshold)}
        )
        options = dict(
            repository=repo, permits=permits, prowlarr_url="http://prowlarr:9696", client=client,
            capacity_provider=capacity, release_policy=ReleasePolicy.from_environment(
                environment,
                media_kind=kind,
            ),
        )
        acquirer = (
            SeriesAcquirer(sonarr_url="http://sonarr:8989", sonarr_api_key="fixture", **options)
            if kind == "series" else
            MovieAcquirer(radarr_url="http://radarr:7878", radarr_api_key="fixture", **options)
        )
        status = await acquirer.acquire(media_key, reserved.reservation_id)
    return status, posted, inspected_names_actual


@pytest.mark.asyncio
async def test_movie_unaffordable_uindex_does_not_block_affordable_1337x(tmp_path):
    status, posted, inspected = await _choose_indexer(
        tmp_path, kind="movie", fallback_threshold=5, offers=[
            ("too-large-primary", "UIndex", 2160, 30, 60_000_000_000),
            ("backup", "1337x", 1080, 20, 2_000_000_000),
        ],
    )
    assert status == "grabbed"
    assert posted == ["backup"]
    assert inspected == ["too-large-primary", "backup"]


@pytest.mark.asyncio
async def test_movie_only_unaffordable_offer_reports_waiting_space(tmp_path):
    status, posted, inspected = await _choose_indexer(
        tmp_path, kind="movie", fallback_threshold=5, offers=[
            ("too-large-primary", "UIndex", 2160, 30, 60_000_000_000),
        ],
    )
    assert status == "waiting_space"
    assert posted == []
    assert inspected == ["too-large-primary"]


@pytest.mark.asyncio
async def test_movie_default_fallback_prefers_well_seeded_affordable_uhd(tmp_path):
    status, posted, inspected = await _choose_indexer(
        tmp_path, kind="movie", fallback_threshold=None, offers=[
            ("uindex-uhd-too-large", "UIndex", 2160, 29, 62_060_000_000),
            ("uindex-low-seeds", "UIndex", 2160, 6, 41_830_000_000),
            ("1337x-many-seeds", "1337x", 2160, 84, 35_630_000_000),
        ],
    )
    assert status == "grabbed"
    assert posted == ["1337x-many-seeds"]
    assert inspected == [
        "uindex-uhd-too-large", "uindex-low-seeds", "1337x-many-seeds",
    ]


@pytest.mark.asyncio
async def test_healthy_primary_is_inspected_before_fallback_despite_earlier_weak_primary(tmp_path):
    from contextlib import aclosing

    repo = ReservationRepository(tmp_path / 'control.sqlite')
    repo.initialize()
    inspected = []
    rows = [
        {'guid': 'weak-high-quality', 'indexer': 'UIndex', 'seeders': 2, 'rank': 30},
        {'guid': 'backup', 'indexer': '1337x', 'seeders': 100, 'rank': 40},
        {'guid': 'healthy-primary', 'indexer': 'UIndex', 'seeders': 10, 'rank': 10},
    ]
    async def eligible(group):
        for release in group:
            inspected.append(release['guid'])
            yield release, (), None, b'torrent'

    async with httpx.AsyncClient() as client:
        acquirer = MovieAcquirer(repository=repo, permits=PermitRegistry(repo.path),
            radarr_url='http://radarr', radarr_api_key='fixture',
            prowlarr_url='http://prowlarr', client=client,
            release_policy=ReleasePolicy(indexer_fallback_min_seeders=5))
        async with aclosing(acquirer._preferred_indexer_candidates(
            rows, eligible=eligible, rank=lambda row: row['rank'],
        )) as candidates:
            first = await anext(candidates)
            assert first[0]['guid'] == 'weak-high-quality'
    assert inspected == ['healthy-primary', 'weak-high-quality']
