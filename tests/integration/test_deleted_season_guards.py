from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.series_acquisition import SeriesAcquirer
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.validation import MediaProbe, ValidationResult


def _reserve(repo, season):
    result = repo.reserve(
        request_id=f"seerr:deleted-season:{season}", source_id=f"deleted-season:{season}",
        media_key=f"season:tmdb:10:{season}", filesystem_id="fixture",
        budget_bytes=1000, free_bytes=100_000, total_bytes=200_000,
    )
    assert result.accepted and result.reservation_id
    return result.reservation_id


def _worker(kind, repo, permits, jobs, client, tmp_path, **kwargs):
    common = dict(
        repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
        sonarr_api_key="secret", client=client, is_tombstoned=jobs.is_tombstoned,
    )
    if kind == "acquirer":
        return SeriesAcquirer(prowlarr_url="http://prowlarr:9696", **common, **kwargs)
    return SeriesFinalizer(
        torrent_root=tmp_path, gateway_url="http://gateway:8081", arr_token="secret",
        **common, **kwargs,
    )


async def _run(worker, kind, season, reservation):
    method = worker.acquire if kind == "acquirer" else worker.finalize
    return await method(f"season:tmdb:10:{season}", reservation)


def _settings(request):
    if request.url.path == "/api/v3/config/downloadclient":
        return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
    if request.url.path == "/api/v3/config/mediamanagement":
        return httpx.Response(200, json={"copyUsingHardlinks": True})
    if request.url.path == "/api/v3/series":
        return httpx.Response(200, json=[{"id": 1, "tmdbId": 10, "monitored": True}])
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acquirer", "finalizer"])
async def test_deleted_season_never_starts_worker_network_operations(tmp_path, kind):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservation = _reserve(repo, 1)
    permits, jobs = PermitRegistry(repo.path), DeletionJobStore(repo.path)
    jobs.tombstone("season:tmdb:10:1", "jellyfin-season")
    requests = []

    def handler(request):
        requests.append(request)
        settings = _settings(request)
        if settings is not None:
            return settings
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = _worker(kind, repo, permits, jobs, client, tmp_path)
        assert await _run(worker, kind, 1, reservation) == "reservation_inactive"
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acquirer", "finalizer"])
@pytest.mark.parametrize("earlier_catalog_missing", [False, True])
async def test_deleted_earlier_season_does_not_block_subsequent_season(
    tmp_path, kind, earlier_catalog_missing,
):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    _reserve(repo, 1)
    reservation = _reserve(repo, 2)
    permits, jobs = PermitRegistry(repo.path), DeletionJobStore(repo.path)
    jobs.tombstone("season:tmdb:10:1", "jellyfin-season")
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
        category="sonarr", reservation_id=reservation, scope_key="S02E01",
        selected_files=("Show.S02E01.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    if kind == "finalizer":
        permits.authorize(
            token=permit.token, infohash=permit.infohash, destination=permit.destination,
            metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
        )

    def handler(request):
        settings = _settings(request)
        if settings is not None:
            return settings
        if request.url.path == "/api/v3/episode":
            episodes = [{
                "id": 21, "seasonNumber": 2, "episodeNumber": 1,
                "monitored": True, "hasFile": False,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }]
            if not earlier_catalog_missing:
                episodes.insert(0, {
                    "id": 11, "seasonNumber": 1, "episodeNumber": 1,
                    "monitored": True, "hasFile": False,
                    "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                })
            return httpx.Response(200, json=episodes)
        if request.url.path == "/api/v3/release":
            assert request.url.params["episodeId"] == "21"
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v2/torrents/info":
            assert request.url.params["hashes"] == "a" * 40
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = _worker(kind, repo, permits, jobs, client, tmp_path, download_window=1) if (
            kind == "acquirer"
        ) else _worker(kind, repo, permits, jobs, client, tmp_path)
        assert await _run(worker, kind, 2, reservation) == (
            "no_eligible_release" if kind == "acquirer" else "downloading"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["acquirer", "finalizer"])
@pytest.mark.parametrize("cancelled", [False, True])
async def test_season_deleted_during_catalog_lookup_stops_processing(tmp_path, kind, cancelled):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservation = _reserve(repo, 1)
    permits, jobs = PermitRegistry(repo.path), DeletionJobStore(repo.path)
    if kind == "finalizer":
        permit = permits.issue(
            infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
            category="sonarr", reservation_id=reservation, scope_key="S01E01",
            selected_files=("Show.S01E01.mkv",), budget_bytes=100,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        permits.authorize(
            token=permit.token, infohash=permit.infohash, destination=permit.destination,
            metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
        )

    def handler(request):
        settings = _settings(request)
        if settings is not None:
            return settings
        if request.url.path == "/api/v3/episode":
            if cancelled:
                with sqlite3.connect(repo.path) as connection:
                    connection.execute(
                        "UPDATE reservations SET state='cancelled' WHERE id=?", (reservation,),
                    )
            else:
                jobs.tombstone("season:tmdb:10:1", "jellyfin-season")
            return httpx.Response(200, json=[{
                "id": 11, "seasonNumber": 1, "episodeNumber": 1,
                "monitored": True, "hasFile": False,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }])
        if request.url.path == "/api/v3/release" or request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = _worker(kind, repo, permits, jobs, client, tmp_path)
        assert await _run(worker, kind, 1, reservation) == "reservation_inactive"


@pytest.mark.asyncio
async def test_season_deleted_during_import_guard_cannot_be_imported(tmp_path, monkeypatch):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservation = _reserve(repo, 1)
    permits, jobs = PermitRegistry(repo.path), DeletionJobStore(repo.path)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
        category="sonarr", reservation_id=reservation, scope_key="S01E01",
        selected_files=("Show.S01E01.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    (tmp_path / "Show.S01E01.mkv").write_bytes(b"video")
    media_root = tmp_path / "library"
    media_root.mkdir()

    def probe(path, **_kwargs):
        return ValidationResult(
            Path(path), 5, MediaProbe(1920, 1080, ("pt-BR",), (), {"streams": [{
                "codec_type": "audio", "tags": {"language": "pt-BR"},
                "disposition": {"original": 1},
            }]}),
        )

    monkeypatch.setattr("homeserver_control.worker.series_finalization.validate_media", probe)

    def handler(request):
        if request.url.path == "/api/v3/config/mediamanagement":
            jobs.tombstone("season:tmdb:10:1", "jellyfin-season")
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        settings = _settings(request)
        if settings is not None:
            return settings
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 11, "seasonNumber": 1, "episodeNumber": 1,
                "monitored": True, "hasFile": False,
            }])
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "progress": 1, "amount_left": 0,
                "content_path": "/data/torrents/Show.S01E01.mkv",
            }])
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[{"name": "Show.S01E01.mkv", "size": 5}])
        if request.url.path == "/api/v3/command":
            return httpx.Response(200, json={"id": 42})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def capacity():
        return CapacityEvidence(free_bytes=100_000, remaining_by_hash={permit.infohash: 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = _worker(
            "finalizer", repo, permits, jobs, client, tmp_path,
            media_root=media_root, capacity_provider=capacity,
        )
        assert await _run(worker, "finalizer", 1, reservation) == "reservation_inactive"
    assert repo.episode_import_state(permit.permit_id) is None
