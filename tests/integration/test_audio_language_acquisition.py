import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

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
                "guid": key, "title": title + " 1080p BluRay", "rejected": False,
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

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        options = dict(
            repository=repository, permits=permits, prowlarr_url="http://prowlarr:9696",
            client=client, release_policy=ReleasePolicy(),
        )
        acquirer = (
            SeriesAcquirer(sonarr_url="http://sonarr:8989", sonarr_api_key="fixture", **options)
            if series
            else MovieAcquirer(radarr_url="http://radarr:7878", radarr_api_key="fixture", **options)
        )
        assert await acquirer.acquire(media_key, reservation.reservation_id) == "grabbed"
    assert grabbed == ["original"]
