from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
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


def _torrent(*, video_bytes=3_000_000_000, subtitle=b"Ted.Lasso.S04E01.pt-BR.srt"):
    files = [
        {b"length": video_bytes, b"path": [b"Ted.Lasso.S04E01.mkv"]},
        {b"length": 1000, b"path": [subtitle]},
    ]
    total = video_bytes + 1000
    info = {
        b"files": files, b"name": b"Ted.Lasso.S04E01",
        b"piece length": 16_777_216,
        b"pieces": b"a" * (20 * ((total + 16_777_215) // 16_777_216)),
    }
    return _bencode({b"info": info})


def _reserve(tmp_path):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    result = repo.reserve(
        request_id="seerr:3:4", source_id="3:4",
        media_key="season:tmdb:97546:4", filesystem_id="fixture-fs",
        budget_bytes=100_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert result.accepted and result.reservation_id
    return repo, PermitRegistry(db), result.reservation_id


def test_series_manifest_requires_one_episode_under_five_gb_and_pt_br():
    assert SeriesAcquirer._eligible_episode_manifest(
        _torrent(), season=4, episode=1
    ) is not None
    assert SeriesAcquirer._eligible_episode_manifest(
        _torrent(video_bytes=5_000_000_001), season=4, episode=1
    ) is None
    assert SeriesAcquirer._eligible_episode_manifest(
        _torrent(subtitle=b"Ted.Lasso.S04E01.pt-PT.srt"), season=4, episode=1
    ) is None
    assert SeriesAcquirer._eligible_episode_manifest(
        _torrent(), season=4, episode=2
    ) is None


@pytest.mark.asyncio
async def test_series_acquirer_grabs_only_due_episode_with_season_permit(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent()
    inspected = inspect_torrent(torrent)
    posts = []
    release = {
        "guid": "episode-one", "indexerId": 2,
        "title": "Ted Lasso S04E01 1080p WEB-DL Atmos",
        "size": 3_000_001_000,
        "downloadUrl": "http://prowlarr:9696/2/download?id=1",
        "infoHash": inspected.infohash, "rejected": False,
        "quality": {"quality": {
            "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
        }}, "protocol": "torrent", "episodeIds": [44],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 97546, "monitored": True,
            }])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 44, "seasonNumber": 4, "episodeNumber": 1,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False},
                {"id": 45, "seasonNumber": 4, "episodeNumber": 2,
                 "airDateUtc": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False},
            ])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            assert request.url.params["episodeId"] == "44"
            return httpx.Response(200, json=[release])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json=release)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == "grabbed"
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) != "grabbed"
    assert len(posts) == 1
    permit = permits.get_for_reservation(reservation_id, scope_key="S04E01")
    assert permit is not None
    assert permit.infohash == inspected.infohash
    assert permit.category == "sonarr"
    assert permit.budget_bytes == 3_000_001_000
