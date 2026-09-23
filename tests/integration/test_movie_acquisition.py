from __future__ import annotations

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.acquisition import MovieAcquirer


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


def _torrent(*, subtitle: bool) -> bytes:
    files = [{b"length": 1_500_000_000, b"path": [b"movie.mkv"]}]
    if subtitle:
        files.append({b"length": 1000, b"path": [b"movie.por.srt"]})
    total = sum(item[b"length"] for item in files)
    info = {
        b"files": files,
        b"name": b"Film",
        b"piece length": 16_777_216,
        b"pieces": b"a" * (20 * ((total + 16_777_215) // 16_777_216)),
    }
    return _bencode({b"info": info})


def _reserve(tmp_path):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    result = repo.reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:1101383",
        filesystem_id="fixture-fs", budget_bytes=50_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert result.accepted and result.reservation_id
    return repo, PermitRegistry(db), result.reservation_id


@pytest.mark.asyncio
async def test_acquirer_verifies_metadata_and_grabs_once(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(subtitle=True)
    inspected = inspect_torrent(torrent)
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            assert request.url.params["tmdbId"] == "1101383"
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            assert request.url.params["movieId"] == "2"
            return httpx.Response(200, json=[{
                "guid": "release-one", "indexerId": 2, "title": "Film 1080p",
                "size": 1_500_100_000, "downloadUrl": "http://prowlarr:9696/2/download?id=1",
                "infoHash": inspected.infohash, "rejected": False,
                "quality": {"quality": {"resolution": 1080}},
                "protocol": "torrent",
            }])
        if request.url.path == "/2/download":
            return httpx.Response(
                200, content=torrent, headers={"content-type": "application/x-bittorrent"}
            )
        if request.url.path == "/api/v3/release" and request.method == "POST":
            posts.append(request)
            assert request.url.host == "radarr"
            return httpx.Response(200, json={"guid": "release-one"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "grabbed"
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "already_permitted"
    assert len(posts) == 1
    permit = permits.get_for_reservation(reservation_id)
    assert permit is not None
    assert permit.infohash == inspected.infohash
    assert permit.metadata_sha256 == inspected.metadata_sha256
    assert permit.destination == "/data/torrents"
    assert permit.category == "radarr"
    assert permit.budget_bytes == 50_000_000_000


@pytest.mark.asyncio
@pytest.mark.parametrize("subtitle", [False, True])
async def test_acquirer_rejects_ineligible_release(tmp_path, subtitle):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(subtitle=subtitle)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release":
            return httpx.Response(200, json=[{
                "guid": "bad-release", "indexerId": 2, "title": "Film 1080p",
                "size": 1_500_100_000,
                "downloadUrl": "magnet:?xt=urn:btih:abc" if subtitle else "http://prowlarr:9696/2/download?id=1",
                "rejected": False, "quality": {"quality": {"resolution": 1080}},
                "protocol": "torrent",
            }])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        raise AssertionError("grab must not occur")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "no_eligible_release"
    assert permits.get_for_reservation(reservation_id) is None


@pytest.mark.asyncio
async def test_acquirer_blocks_automatic_radarr_import(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/config/downloadclient"
        return httpx.Response(200, json={"enableCompletedDownloadHandling": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "import_guard"
    assert permits.get_for_reservation(reservation_id) is None
