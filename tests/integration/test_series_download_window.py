from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.series_acquisition import SeriesAcquirer
from homeserver_control.worker.source_health import SourceHealthStore


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


def _torrent(season, number):
    tag = f"Show.S{season:02d}E{number:02d}".encode()
    return _bencode({b"info": {
        b"files": [
            {b"length": 2_000_000_000, b"path": [tag + b".mkv"]},
            {b"length": 1000, b"path": [tag + b".pt-BR.srt"]},
        ],
        b"name": tag,
        b"piece length": 16_777_216,
        b"pieces": b"a" * (20 * 120),
    }})


def _reserve(repo, season):
    result = repo.reserve(
        request_id=f"seerr:window:{season}", source_id=f"window:{season}",
        media_key=f"season:tmdb:10:{season}", filesystem_id="fixture-fs",
        budget_bytes=0, free_bytes=100_000_000_000, total_bytes=200_000_000_000,
    )
    assert result.accepted and result.reservation_id
    return result.reservation_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "window,global_limit,free_bytes,expected_posts",
    [(2, 4, 100_000_000_000, [1, 2]), (4, 2, 100_000_000_000, [1, 2]),
     (4, 4, 3_000_000_000, [1])],
)
async def test_series_admits_more_than_one_episode_inside_bounded_window(
    tmp_path, window, global_limit, free_bytes, expected_posts
):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservation = _reserve(repo, 1)
    permits = PermitRegistry(repo.path)
    posts, searched, remaining = [], [], {}

    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 10, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": number, "seasonNumber": 1, "episodeNumber": number,
                 "monitored": True, "hasFile": False,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
                for number in (1, 2, 3)
            ])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            number = int(request.url.params["episodeId"])
            searched.append(number)
            inspected = inspect_torrent(_torrent(1, number))
            return httpx.Response(200, json=[{
                "guid": f"episode-{number}", "indexerId": 2,
                "title": f"Show S01E{number:02d} 1080p WEB-DL",
                "size": inspected.total_bytes, "infoHash": inspected.infohash,
                "downloadUrl": f"http://prowlarr:9696/2/download?id={number}",
                "rejected": False, "protocol": "torrent", "episodeIds": [number],
                "quality": {"quality": {
                    "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
                }},
            }])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=_torrent(1, int(request.url.params["id"])))
        if request.url.path == "/api/v3/release" and request.method == "POST":
            number = json.loads(request.content)["episodeIds"][0]
            permit = permits.get_for_reservation(reservation, scope_key=f"S01E{number:02d}")
            assert permit is not None
            permits.authorize(
                token=permit.token, infohash=permit.infohash, destination=permit.destination,
                metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
            )
            remaining[permit.infohash] = permit.budget_bytes
            posts.append(number)
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def capacity():
        return CapacityEvidence(free_bytes=free_bytes, remaining_by_hash=remaining)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
            capacity_provider=capacity, download_window=window, max_active_downloads=global_limit,
        )
        assert await acquirer.acquire("season:tmdb:10:1", reservation) == "grabbed"
        assert await acquirer.acquire("season:tmdb:10:1", reservation) == (
            "grabbed" if len(expected_posts) == 2 else "waiting_space"
        )
        assert await acquirer.acquire("season:tmdb:10:1", reservation) != "grabbed"
    assert posts == expected_posts
    assert searched[:len(expected_posts)] == expected_posts
    admitted = [
        number for number in (1, 2, 3)
        if permits.get_for_reservation(reservation, scope_key=f"S01E{number:02d}") is not None
    ]
    assert admitted == expected_posts
    assert permits.get_for_reservation(reservation, scope_key="S01E03") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("protect_later", [False, True])
async def test_series_resumes_multiple_window_episodes_without_touching_protected_source(
    tmp_path, protect_later,
):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservation = _reserve(repo, 1)
    permits = PermitRegistry(repo.path)
    issued = {}
    for number in (1, 2, 3):
        metadata = inspect_torrent(_torrent(1, number))
        permit = permits.issue(
            infohash=metadata.infohash, metadata_sha256=metadata.metadata_sha256,
            destination="/data/torrents", category="sonarr", reservation_id=reservation,
            scope_key=f"S01E{number:02d}", selected_files=tuple(
                item.path for item in metadata.files
            ), budget_bytes=metadata.total_bytes,
            capacity=CapacityEvidence(free_bytes=20_000_000_000, remaining_by_hash={}),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        permits.authorize(
            token=permit.token, infohash=permit.infohash, destination=permit.destination,
            metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
        )
        issued[number] = permit
    health = SourceHealthStore(repo.path)
    if protect_later:
        health.protect_source(issued[3].infohash)
    paused, actions = {permit.infohash for permit in issued.values()}, []

    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 10, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": number, "seasonNumber": 1, "episodeNumber": number,
                 "monitored": True, "hasFile": False,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
                for number in issued
            ])
        if request.url.path == "/internal/series-queue-state":
            body = json.loads(request.content)
            number, permit = next(
                (number, permit) for number, permit in issued.items()
                if permit.token == body["permit_token"]
            )
            actions.append((number, body["action"]))
            if body["action"] == "start":
                paused.remove(permit.infohash)
            else:
                paused.add(permit.infohash)
            return httpx.Response(200, json={"state": "started"})
        if request.url.path == "/internal/torrent-health":
            permit = next(
                permit for permit in issued.values()
                if permit.token == request.headers["X-Admission-Permit"]
            )
            return httpx.Response(200, json={
                "hash": permit.infohash, "downloaded": 1, "amount_left": permit.budget_bytes,
                "num_seeds": 20, "dlspeed": 1_000_000, "state": "downloading", "progress": 0.1,
            })
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def capacity():
        return CapacityEvidence(
            free_bytes=5_000_000_000,
            remaining_by_hash={permit.infohash: permit.budget_bytes for permit in issued.values()},
            paused_hashes=frozenset(paused),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
            gateway_url="http://gateway:8081", arr_token="secret", health_store=health,
            capacity_provider=capacity, download_window=2,
        )
        assert await acquirer.acquire("season:tmdb:10:1", reservation) == "waiting_episodes"
    assert actions == ([] if protect_later else [(3, "stop")]) + [(1, "start"), (2, "start")]
    assert issued[1].infohash not in paused
    assert issued[2].infohash not in paused


@pytest.mark.asyncio
async def test_series_window_can_prefetch_next_season_without_importing_previous(tmp_path):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    _reserve(repo, 1)
    reservation = _reserve(repo, 2)
    permits = PermitRegistry(repo.path)
    searched = []

    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 10, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": season, "seasonNumber": season, "episodeNumber": 1,
                 "monitored": True, "hasFile": False,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
                for season in (1, 2)
            ])
        if request.url.path == "/api/v3/release":
            searched.append(request.url.params["episodeId"])
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("season:tmdb:10:2", reservation) == "no_eligible_release"
    assert searched == ["2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_season,expected_search", [(2, 215), (3, 301)])
@pytest.mark.parametrize("evidence_state", [
    "fresh", "missing_provider", "failed_provider", "unknown_hash", "authorized",
])
async def test_completed_downloads_waiting_for_import_release_acquisition_window_slots(
    tmp_path, requested_season, expected_search, evidence_state,
):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    season_two_reservation = _reserve(repo, 2)
    reservation = _reserve(repo, 3) if requested_season == 3 else season_two_reservation
    permits = PermitRegistry(repo.path)
    remaining = {}
    for number in range(5, 15):
        metadata = inspect_torrent(_torrent(2, number))
        permit = permits.issue(
            infohash=metadata.infohash, metadata_sha256=metadata.metadata_sha256,
            destination="/data/torrents", category="sonarr",
            reservation_id=season_two_reservation, scope_key=f"S02E{number:02d}",
            selected_files=tuple(item.path for item in metadata.files),
            budget_bytes=metadata.total_bytes,
            capacity=CapacityEvidence(free_bytes=100_000_000_000, remaining_by_hash=remaining),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        permits.authorize(
            token=permit.token, infohash=permit.infohash, destination=permit.destination,
            metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
        )
        remaining[permit.infohash] = permit.budget_bytes if number == 5 else 0
        if evidence_state == "authorized" and number != 5:
            with sqlite3.connect(repo.path) as connection:
                connection.execute(
                    "UPDATE gateway_permits SET state='authorized' WHERE token=?", (permit.token,),
                )
        if evidence_state == "unknown_hash" and number != 5:
            del remaining[permit.infohash]
    searched = []

    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 10, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": season * 100 + number, "seasonNumber": season, "episodeNumber": number,
                 "monitored": True, "hasFile": season == 2 and number < 5,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
                for season, numbers in ((2, range(1, 21)), (3, range(1, 3))) for number in numbers
            ])
        if request.url.path == "/api/v3/release":
            searched.append(int(request.url.params["episodeId"]))
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def capacity():
        if evidence_state == "failed_provider":
            raise ValueError("fixture stale snapshot")
        return CapacityEvidence(free_bytes=100_000_000_000, remaining_by_hash=remaining)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="fixture", prowlarr_url="http://prowlarr:9696", client=client,
            capacity_provider=None if evidence_state == "missing_provider" else capacity,
            download_window=10, max_active_downloads=10,
        )
        await acquirer.acquire(f"season:tmdb:10:{requested_season}", reservation)
    assert (expected_search in searched) is (evidence_state == "fresh")
    assert permits.get_for_reservation(season_two_reservation, scope_key="S02E05").infohash == (
        inspect_torrent(_torrent(2, 5)).infohash
    )
    assert all(repo.episode_import_state(source.permit_id) is None for source in
               permits.list_source_history(season_two_reservation, scope_key="S02E06"))


@pytest.mark.asyncio
@pytest.mark.parametrize("pack_remaining", [0, 1])
@pytest.mark.parametrize("window", [1, 2, 3])
@pytest.mark.parametrize("parent_state", ["confirmed", "authorized", "unknown"])
async def test_complete_pack_parent_releases_slots_but_partial_parent_keeps_season_priority(
    tmp_path, pack_remaining, window, parent_state,
):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    earlier_reservation, requested_reservation = _reserve(repo, 2), _reserve(repo, 3)
    permits, remaining = PermitRegistry(repo.path), {}

    def admit(torrent, scope, *, pack_episodes=()):
        metadata = inspect_torrent(torrent)
        permit = permits.issue(
            infohash=metadata.infohash, metadata_sha256=metadata.metadata_sha256,
            destination="/data/torrents", category="sonarr", reservation_id=earlier_reservation,
            scope_key=scope, selected_files=tuple(item.path for item in metadata.files),
            budget_bytes=metadata.total_bytes,
            capacity=CapacityEvidence(free_bytes=100_000_000_000, remaining_by_hash=remaining),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        if pack_episodes:
            permits.bind_season_pack(permit.token, episode_files={
                f"S02E{number:02d}": tuple(
                    path for path in permit.selected_files if f"S02E{number:02d}" in path
                ) for number in pack_episodes
            })
        permits.authorize(
            token=permit.token, infohash=permit.infohash, destination=permit.destination,
            metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
        )
        remaining[permit.infohash] = permit.budget_bytes
        return permit

    active = admit(_torrent(2, 1), "S02E01")
    torrent = _bencode({b"info": {
        b"files": [
            {b"length": 2_000_000_000 if suffix == "mkv" else 1000,
             b"path": [f"Show.S02E{number:02d}.{suffix}".encode()]}
            for number in (1, 2, 3) for suffix in ("mkv", "pt-BR.srt")
        ],
        b"name": b"pack", b"piece length": 16_777_216, b"pieces": b"a" * (20 * 358),
    }})
    parent = admit(torrent, "S02PACK", pack_episodes=(2, 3))
    remaining[parent.infohash] = pack_remaining
    if parent_state != "confirmed":
        with sqlite3.connect(repo.path) as connection:
            connection.execute(
                "UPDATE gateway_permits SET state=? WHERE token=?", (parent_state, parent.token),
            )
    searched = []

    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 10, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": season * 100 + number, "seasonNumber": season, "episodeNumber": number,
                 "monitored": True, "hasFile": False,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat()}
                for season, numbers in ((2, (1, 2, 3)), (3, (1,))) for number in numbers
            ])
        if request.url.path == "/api/v3/release":
            searched.append(int(request.url.params["episodeId"]))
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def capacity():
        return CapacityEvidence(free_bytes=100_000_000_000, remaining_by_hash=remaining)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="fixture", prowlarr_url="http://prowlarr:9696", client=client,
            capacity_provider=capacity, download_window=window, max_active_downloads=window,
        )
        selected_ids = set()
        reconcile = acquirer._reconcile_existing_queue

        async def capture_selection(**kwargs):
            selected_ids.update(kwargs["active_episode_ids"])
            return await reconcile(**kwargs)

        acquirer._reconcile_existing_queue = capture_selection
        await acquirer.acquire("season:tmdb:10:3", requested_reservation)
    complete_parent = pack_remaining == 0 and parent_state == "confirmed"
    can_prefetch = window == 3 or complete_parent and window >= 2
    assert searched == ([301] if can_prefetch else [])
    assert selected_ids == {
        201, *(() if complete_parent or window == 1 else (202, 203)),
        *((301,) if can_prefetch else ()),
    }
    assert permits.get(active.token).state == "confirmed"
    child = permits.get_for_reservation(earlier_reservation, scope_key="S02E02")
    assert child.season_pack_parent_id == parent.permit_id
    assert repo.episode_import_state(child.permit_id) is None
