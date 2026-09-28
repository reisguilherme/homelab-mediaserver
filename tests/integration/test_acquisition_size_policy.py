from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.series_acquisition import SeriesAcquirer
from homeserver_control.worker.source_health import SourceHealthStore


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    return (
        b"d" + b"".join(bencode(key) + bencode(item) for key, item in sorted(value.items())) + b"e"
    )


def torrent(video_bytes, inflated):
    files = [
        {b"path": [b"Fixture.S02E04.mkv"], b"length": video_bytes},
        {b"path": [b"Fixture.S02E04.pt-BR.srt"], b"length": 1000},
    ]
    if inflated:
        files.append({b"path": [b"sample.mkv"], b"length": 2_000_000_000})
    total = sum(item[b"length"] for item in files)
    return bencode(
        {
            b"info": {
                b"name": b"Fixture",
                b"files": files,
                b"piece length": 16_777_216,
                b"pieces": b"x" * (20 * ((total + 16_777_215) // 16_777_216)),
            }
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["movie", "series"])
@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize(
    "video_bytes,inflated,accepted",
    [
        (400_000_000, False, False),
        (400_000_000, True, False),
        (1_800_000_000, False, True),
    ],
)
async def test_candidates_enforce_actual_video_size_before_grab_or_probe(
    tmp_path, kind, replacement, video_bytes, inflated, accepted
):
    offered = {
        "title": "Fixture.S02E04.1080p.WEB-DL",
        "size": 5_000_000_000,
        "rejected": False,
        "downloadUrl": "http://prowlarr:9696/1/download",
        "quality": {"quality": {"source": "webdl", "modifier": "none", "resolution": 1080}},
    }

    def handler(request):
        assert request.method == "GET", "a size-rejected candidate must never reach mutation"
        if request.url.path == "/api/v3/release":
            return httpx.Response(200, json=[offered])
        if request.url.path == "/1/download":
            return httpx.Response(200, content=torrent(video_bytes, inflated))
        raise AssertionError(request.url.path)

    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    permits = PermitRegistry(tmp_path / "control.sqlite")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        kwargs = dict(
            repository=repo,
            permits=permits,
            prowlarr_url="http://prowlarr:9696",
            client=client,
            release_policy=ReleasePolicy(),
        )
        reason = "slow" if replacement else None
        if kind == "movie":
            acquirer = MovieAcquirer(
                **kwargs, radarr_url="http://radarr:7878", radarr_api_key="fixture"
            )
            candidates = acquirer._eligible_movie_releases(
                [offered], replacement_reason=reason, excluded_infohashes=set(), runtime_minutes=45
            )
        else:
            acquirer = SeriesAcquirer(
                **kwargs, sonarr_url="http://sonarr:8989", sonarr_api_key="fixture"
            )
            candidates = acquirer._eligible_releases(
                series_id=1,
                episode_id=2,
                season=2,
                episode=4,
                tmdb_id=3,
                replacement_reason=reason,
                runtime_minutes=45,
            )
        observed = [item async for item in candidates]
    assert bool(observed) is accepted
    assert permits.get_for_reservation("not-created") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [True, False])
async def test_series_ordering_does_not_start_or_stop_protected_episode(tmp_path, active):
    path = tmp_path / "control.sqlite"
    repo = ReservationRepository(path)
    repo.initialize()
    reserved = repo.reserve(
        request_id="fixture",
        source_id="fixture",
        media_key="season:tmdb:3:2",
        filesystem_id="fixture",
        budget_bytes=10_000,
        free_bytes=100_000,
        total_bytes=200_000,
    )
    registry = PermitRegistry(path)
    permit = registry.issue(
        infohash="a" * 40,
        metadata_sha256="b" * 64,
        destination="/data/torrents",
        category="sonarr",
        reservation_id=reserved.reservation_id,
        scope_key="S02E04",
        selected_files=("Fixture/S02E04.mkv",),
        budget_bytes=1000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    registry.authorize(
        token=permit.token,
        infohash=permit.infohash,
        metadata_sha256=permit.metadata_sha256,
        destination=permit.destination,
        effect=lambda _: {"accepted": True, "infohash": permit.infohash},
    )
    health = SourceHealthStore(path)
    health.protect_source(permit.infohash)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"accepted": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def capacity():
            return CapacityEvidence(free_bytes=100_000, remaining_by_hash={})

        acquirer = SeriesAcquirer(
            repository=repo,
            permits=registry,
            sonarr_url="http://sonarr",
            sonarr_api_key="fixture",
            prowlarr_url="http://prowlarr",
            client=client,
            gateway_url="http://gateway",
            arr_token="fixture",
            health_store=health,
            capacity_provider=capacity,
        )
        assert (
            await acquirer._reconcile_existing_queue(
                episodes=[{"id": 4, "seasonNumber": 2, "episodeNumber": 4}],
                season=2,
                reservation_id=reserved.reservation_id,
                active_episode_ids={4} if active else {3},
                imported_episode_ids=set(),
            )
            is None
        )
    assert requests == []
    assert health.is_protected(permit.infohash)
