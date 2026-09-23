from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.source_health import SourceHealthStore, TorrentHealth
from homeserver_control.worker.subdl import SubDLSource


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


def _torrent(
    *, subtitle: bool, subtitle_name: bytes = b"movie.pt-BR.srt",
    video_bytes: int = 1_500_000_000,
    extra_video_path: list[bytes] | None = None,
    extra_subtitle_name: bytes | None = None,
    root_name: bytes = b"Film", video_name: bytes = b"movie.mkv",
) -> bytes:
    files = [{b"length": video_bytes, b"path": [video_name]}]
    if extra_video_path is not None:
        files.append({b"length": 79_000_000, b"path": extra_video_path})
    if subtitle:
        files.append({b"length": 1000, b"path": [subtitle_name]})
    if extra_subtitle_name is not None:
        files.append({b"length": 1000, b"path": [extra_subtitle_name]})
    total = sum(item[b"length"] for item in files)
    info = {
        b"files": files,
        b"name": root_name,
        b"piece length": 16_777_216,
        b"pieces": b"a" * (20 * ((total + 16_777_215) // 16_777_216)),
    }
    return _bencode({b"info": info})


def _reserve(tmp_path, *, budget=81_000_000_000):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    result = repo.reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:1101383",
        filesystem_id="fixture-fs", budget_bytes=budget,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert result.accepted and result.reservation_id
    return repo, PermitRegistry(db), result.reservation_id


def _transport(handler):
    def route(request):
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        return handler(request)
    return httpx.MockTransport(route)


@pytest.mark.asyncio
async def test_movie_requires_hardlink_import_before_grab(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": False})
        raise AssertionError("release search must not start without hardlinks")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "import_guard"


def test_manifest_allows_video_up_to_80_gb_within_reservation():
    maximum = _torrent(subtitle=True, video_bytes=80_000_000_000)
    oversized = _torrent(subtitle=True, video_bytes=80_000_000_001)
    assert MovieAcquirer._eligible_manifest(maximum, 81_000_000_000) is not None
    assert MovieAcquirer._eligible_manifest(oversized, 81_000_000_000) is not None


def test_manifest_excludes_sample_video_but_rejects_second_feature():
    with_sample = _torrent(
        subtitle=False, extra_video_path=[b"Sample", b"movie.sample.mkv"]
    )
    manifest = MovieAcquirer._eligible_manifest(
        with_sample, 81_000_000_000, allow_external_subtitle=True
    )
    assert manifest is not None
    assert len(manifest[2]) == 1
    assert manifest[2][0].endswith("/movie.mkv")

    with_second_feature = _torrent(
        subtitle=False, extra_video_path=[b"Extras", b"featurette.mkv"]
    )
    assert MovieAcquirer._eligible_manifest(
        with_second_feature, 81_000_000_000, allow_external_subtitle=True
    ) is None


def test_manifest_selects_english_sidecar_only_when_brazilian_portuguese_is_absent():
    english_only = _torrent(subtitle=True, subtitle_name=b"movie.en.srt")
    manifest = MovieAcquirer._eligible_manifest(english_only)
    assert manifest is not None
    assert manifest[2] == ("Film/movie.mkv", "Film/movie.en.srt")

    both = _torrent(
        subtitle=True, subtitle_name=b"movie.pt-BR.srt",
        extra_subtitle_name=b"movie.en.srt",
    )
    manifest = MovieAcquirer._eligible_manifest(both)
    assert manifest is not None
    assert manifest[2] == ("Film/movie.mkv", "Film/movie.pt-BR.srt")


@pytest.mark.asyncio
@pytest.mark.parametrize("subtitle_available", [True, False])
async def test_movie_grab_uses_persisted_exact_release_subdl_sidecar(tmp_path, subtitle_available):
    repo, permits, reservation_id = _reserve(tmp_path)
    store = SubtitleArtifactStore(repo.path)
    torrents = TorrentArtifactStore(repo.path)
    torrent = _torrent(subtitle=False)
    inspected = inspect_torrent(torrent)
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda brasileira\n"
    release = {
        "guid": "sparks", "indexerId": 2,
        "title": "Dallas Buyers Club 2013 1080p BluRay x264 SPARKS",
        "size": 1_500_000_000,
        "downloadUrl": "http://prowlarr:9696/2/download?id=1",
        "infoHash": inspected.infohash, "rejected": False,
        "quality": {"quality": {
            "source": "bluray", "modifier": "none", "resolution": 1080,
        }}, "protocol": "torrent",
    }
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[release])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json={
                "status": True, "results": [{"tmdb_id": 1101383, "type": "movie"}],
                "subtitles": [{"language": "BR_PT", "unpack_files": [{
                    "language": "BR_PT", "format": "srt", "size": len(srt),
                    "release_name": "Dallas.Buyers.Club.2013.1080p.BluRay.x264-SPARKS",
                    "url": "/subtitle/123/abc",
                }]}] if subtitle_available else [],
            })
        if request.url.host == "dl.subdl.com":
            return httpx.Response(200, content=srt)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            assert store.get(reservation_id, None, inspected.infohash) == (
                srt if subtitle_available else None
            )
            posts.append(request)
            return httpx.Response(200, json=release)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
            subtitle_source=SubDLSource(api_key="test-key", client=client),
            subtitle_store=store, torrent_store=torrents,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "grabbed"
    assert len(posts) == 1
    permit = permits.get_for_reservation(reservation_id)
    assert permit is not None
    assert permit.selected_files == ("Film/movie.mkv",)
    assert torrents.get(permit) == torrent


@pytest.mark.asyncio
async def test_movie_grab_searches_subdl_ptbr_even_when_torrent_has_english_sidecar(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    store = SubtitleArtifactStore(repo.path)
    torrent = _torrent(subtitle=True, subtitle_name=b"movie.en.srt")
    infohash = inspect_torrent(torrent).infohash
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda brasileira\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": "english-sidecar", "indexerId": 2,
                "title": "Dallas Buyers Club 2013 1080p BluRay x264 SPARKS",
                "size": inspect_torrent(torrent).total_bytes,
                "downloadUrl": "http://prowlarr:9696/2/download?id=1",
                "infoHash": infohash, "rejected": False,
                "quality": {"quality": {
                    "source": "bluray", "modifier": "none", "resolution": 1080,
                }}, "protocol": "torrent",
            }])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        if request.url.host == "api.subdl.com":
            assert request.url.params["languages"] == "BR_PT"
            return httpx.Response(200, json={
                "status": True, "results": [{"tmdb_id": 1101383, "type": "movie"}],
                "subtitles": [{"language": "BR_PT", "unpack_files": [{
                    "language": "BR_PT", "format": "srt", "size": len(srt),
                    "release_name": "Dallas.Buyers.Club.2013.1080p.BluRay.x264-SPARKS",
                    "url": "/subtitle/123/abc",
                }]}],
            })
        if request.url.host == "dl.subdl.com":
            return httpx.Response(200, content=srt)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            assert store.get(reservation_id, None, infohash) == srt
            return httpx.Response(200, json={"guid": "english-sidecar"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
            subtitle_source=SubDLSource(api_key="test-key", client=client),
            subtitle_store=store,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "grabbed"
    assert permits.get_for_reservation(reservation_id).selected_files == (
        "Film/movie.mkv", "Film/movie.en.srt"
    )


@pytest.mark.asyncio
async def test_acquirer_verifies_metadata_and_grabs_once(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
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
                "quality": {"quality": {
                    "source": "bluray", "modifier": "remux", "resolution": 1080,
                }},
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

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        async def capacity():
            return CapacityEvidence(free_bytes=2_000_000_000, remaining_by_hash={})
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
            capacity_provider=capacity,
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
    assert permit.budget_bytes == inspected.total_bytes


@pytest.mark.asyncio
async def test_movie_tries_next_eligible_release_when_best_does_not_fit(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
    large = _torrent(subtitle=True, video_bytes=4_000_000_000)
    small = _torrent(subtitle=True, video_bytes=2_000_000_000)
    grabbed = []

    def release(guid, torrent, modifier):
        return {
            "guid": guid, "indexerId": 2, "title": f"Film 2160p BluRay {modifier}",
            "size": inspect_torrent(torrent).total_bytes,
            "downloadUrl": f"http://prowlarr:9696/2/download?id={guid}",
            "infoHash": inspect_torrent(torrent).infohash, "rejected": False,
            "quality": {"quality": {
                "source": "bluray", "modifier": modifier, "resolution": 2160,
            }},
        }

    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[release("large", large, "remux"),
                                              release("small", small, "none")])
        if request.url.path == "/2/download":
            content = large if request.url.params["id"] == "large" else small
            return httpx.Response(200, content=content)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            grabbed.append(request.read().decode())
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def capacity():
        return CapacityEvidence(free_bytes=3_000_000_000, remaining_by_hash={})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
            capacity_provider=capacity,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "grabbed"
    assert len(grabbed) == 1 and '"guid":"small"' in grabbed[0]
    assert (
        permits.get_for_reservation(reservation_id).budget_bytes
        == inspect_torrent(small).total_bytes
    )


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
                "rejected": False, "quality": {"quality": {
                    "source": "bluray", "modifier": "remux", "resolution": 1080,
                }},
                "protocol": "torrent",
            }])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        raise AssertionError("grab must not occur")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
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

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "import_guard"
    assert permits.get_for_reservation(reservation_id) is None


@pytest.mark.asyncio
async def test_acquirer_retries_authorized_permit_after_restart(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(subtitle=True)
    inspected = inspect_torrent(torrent)
    original = permits.issue(
        infohash=inspected.infohash, metadata_sha256=inspected.metadata_sha256,
        destination="/data/torrents", category="radarr", reservation_id=reservation_id,
        selected_files=("Film/movie.mkv", "Film/movie.pt-BR.srt"),
        budget_bytes=81_000_000_000, expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": "release-one", "indexerId": 2, "title": "Film 1080p",
                "size": 1_500_100_000, "downloadUrl": "http://prowlarr:9696/2/download?id=1",
                "infoHash": inspected.infohash, "rejected": False,
                "quality": {"quality": {
                    "source": "bluray", "modifier": "remux", "resolution": 1080,
                }},
            }])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"guid": "release-one"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "grabbed"
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "already_permitted"
    assert len(posts) == 1
    assert permits.get_for_reservation(reservation_id).permit_id == original.permit_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("releases", "chosen"),
    [
        (
            [
                ("yts", "Film 2160p WEB-DL DV Atmos", "webdl", "none", 2160, "YTS"),
                ("bluray", "Film 2160p BluRay", "bluray", "none", 2160, "1337x"),
                ("remux", "Film 1080p BluRay REMUX", "bluray", "remux", 1080, "1337x"),
            ],
            "remux",
        ),
        (
            [
                ("sdr", "Film 2160p BluRay REMUX", "bluray", "remux", 2160, "1337x"),
                ("dv", "Film 2160p BluRay REMUX DV", "bluray", "remux", 2160, "1337x"),
                ("atmos", "Film 2160p BluRay REMUX DV Atmos", "bluray", "remux", 2160, "1337x"),
            ],
            "atmos",
        ),
        (
            [
                ("webrip", "Film 2160p WEBRip", "webrip", "none", 2160, "YTS"),
                ("webdl", "Film 1080p WEB-DL", "webdl", "none", 1080, "1337x"),
            ],
            "webdl",
        ),
    ],
)
async def test_acquirer_grabs_best_admissible_quality(tmp_path, releases, chosen):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(subtitle=True)
    inspected = inspect_torrent(torrent)
    grabbed = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": guid, "title": title, "indexer": indexer,
                "size": 1_500_100_000, "downloadUrl": f"http://prowlarr:9696/2/download?id={guid}",
                "infoHash": inspected.infohash, "rejected": False,
                "quality": {"quality": {
                    "name": "Remux-1080p", "source": source,
                    "modifier": modifier, "resolution": resolution,
                }},
            } for guid, title, source, modifier, resolution, indexer in releases])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            grabbed.append(request.read().decode())
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "grabbed"
    assert len(grabbed) == 1
    assert f'"guid":"{chosen}"' in grabbed[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("subtitle_name", [b"movie.por.srt", b"movie.pt.srt", b"movie.pt-PT.srt"])
async def test_acquirer_rejects_ambiguous_or_portugal_subtitle(tmp_path, subtitle_name):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(subtitle=True, subtitle_name=subtitle_name)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": "ambiguous", "title": "Film 1080p BluRay REMUX",
                "size": 1_500_100_000, "downloadUrl": "http://prowlarr:9696/2/download?id=1",
                "rejected": False, "quality": {"quality": {
                    "source": "bluray", "modifier": "remux", "resolution": 1080,
                }},
            }])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        raise AssertionError("grab must not occur")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "no_eligible_release"
    assert permits.get_for_reservation(reservation_id) is None


@pytest.mark.asyncio
async def test_acquirer_resolves_magnet_to_verified_cached_torrent(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(subtitle=True)
    inspected = inspect_torrent(torrent)
    cache_url = f"https://itorrents.net/torrent/{inspected.infohash.upper()}.torrent"
    grabbed = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": "magnet-release", "indexerId": 2,
                "title": "Film 2160p BluRay REMUX DV Atmos",
                "size": 1_500_100_000,
                "downloadUrl": "http://prowlarr:9696/2/download?id=1",
                "infoHash": inspected.infohash.upper(), "rejected": False,
                "quality": {"quality": {
                    "source": "bluray", "modifier": "remux", "resolution": 2160,
                }},
            }])
        if request.url.path == "/2/download":
            return httpx.Response(302, headers={
                "location": f"magnet:?xt=urn:btih:{inspected.infohash.upper()}&dn=Film",
            })
        if str(request.url) == cache_url:
            return httpx.Response(200, content=torrent)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            grabbed.append(request.read())
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "grabbed"
    assert len(grabbed) == 1
    assert b'"downloadUrl":"http://prowlarr:9696/2/download?id=1"' in grabbed[0]
    assert permits.get_for_reservation(reservation_id).infohash == inspected.infohash


@pytest.mark.asyncio
async def test_acquirer_rejects_magnet_cache_with_different_infohash(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(subtitle=True)
    inspected = inspect_torrent(torrent)
    wrong_hash = "a" * 40 if inspected.infohash != "a" * 40 else "b" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": "magnet-release", "title": "Film 1080p WEB-DL",
                "size": 1_500_100_000,
                "downloadUrl": "http://prowlarr:9696/2/download?id=1",
                "rejected": False, "quality": {"quality": {
                    "source": "webdl", "modifier": "none", "resolution": 1080,
                }},
            }])
        if request.url.path == "/2/download":
            return httpx.Response(301, headers={"location": f"magnet:?xt=urn:btih:{wrong_hash}"})
        if request.url.host == "itorrents.net":
            return httpx.Response(200, content=torrent)
        raise AssertionError("grab must not occur")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "no_eligible_release"
    assert permits.get_for_reservation(reservation_id) is None


@pytest.mark.asyncio
async def test_acquirer_rejects_redirecting_torrent_cache(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    inspected = inspect_torrent(_torrent(subtitle=True))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{"id": 2, "tmdbId": 1101383, "hasFile": False}])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": "loop", "title": "Film 1080p BluRay",
                "size": 1_500_100_000,
                "downloadUrl": "http://prowlarr:9696/2/download?id=1",
                "rejected": False, "quality": {"quality": {
                    "source": "bluray", "modifier": "none", "resolution": 1080,
                }},
            }])
        if request.url.path in {"/2/download", f"/torrent/{inspected.infohash.upper()}.torrent"}:
            return httpx.Response(301, headers={
                "location": f"magnet:?xt=urn:btih:{inspected.infohash}",
            })
        raise AssertionError("grab must not occur")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "no_eligible_release"
    assert permits.get_for_reservation(reservation_id) is None


@pytest.mark.asyncio
async def test_movie_replaces_persistently_stalled_source_with_verified_seeded_release(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
    old_torrent = _torrent(subtitle=True)
    new_torrent = _torrent(
        subtitle=True, video_bytes=1_600_000_000,
        root_name=b"Film.Alternative", video_name=b"movie.alternative.mkv",
    )
    old_metadata = inspect_torrent(old_torrent)
    new_metadata = inspect_torrent(new_torrent)
    assert {item.path for item in old_metadata.files}.isdisjoint(
        item.path for item in new_metadata.files
    )
    old = permits.issue(
        infohash=old_metadata.infohash, metadata_sha256=old_metadata.metadata_sha256,
        destination="/data/torrents", category="radarr", reservation_id=reservation_id,
        selected_files=tuple(item.path for item in old_metadata.files),
        budget_bytes=old_metadata.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        capacity=CapacityEvidence(free_bytes=2_000_000_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    torrents = TorrentArtifactStore(repo.path)
    torrents.put(old, old_torrent)
    SourceHealthStore(tmp_path / "control.sqlite").observe(
        old.permit_id,
        TorrentHealth(
            infohash=old.infohash, downloaded=0,
            amount_left=old_metadata.total_bytes, num_seeds=0, dlspeed=0,
            state="stalledDL", progress=0.0,
        ),
        now=time.time() - 31 * 60,
    )
    events = []
    stopped = False

    def release(guid, metadata, seeds):
        return {
            "guid": guid, "indexerId": 2,
            "title": "Film 1080p BluRay REMUX DV Atmos",
            "size": metadata.total_bytes, "seeders": seeds,
            "downloadUrl": f"http://prowlarr:9696/2/download?id={guid}",
            "infoHash": metadata.infohash, "rejected": False,
            "quality": {"quality": {
                "source": "bluray", "modifier": "remux", "resolution": 1080,
            }},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stopped
        if request.url.path == "/internal/torrent-health":
            assert request.headers["X-Arr-Token"] == "worker-secret"
            assert request.headers["X-Admission-Permit"] == old.token
            assert "permit_token" not in request.url.params
            return httpx.Response(200, json={
                "hash": old.infohash, "downloaded": 0,
                "amount_left": old_metadata.total_bytes,
                "num_seeds": 0, "dlspeed": 0,
                "state": "stalledDL", "progress": 0.0,
            })
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{
                "id": 2, "tmdbId": 1101383, "hasFile": False,
            }])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[
                release("old", old_metadata, 0), release("new", new_metadata, 42),
            ])
        if request.url.path == "/2/download":
            return httpx.Response(200, content={
                "old": old_torrent, "new": new_torrent,
            }[request.url.params["id"]])
        if request.url.path == "/internal/source-state":
            body = request.read().decode()
            assert request.headers["X-Arr-Token"] == "worker-secret"
            assert '"permit_token":"' + old.token + '"' in body
            assert '"action":"stop"' in body
            stopped = True
            events.append("stop")
            return httpx.Response(200, json={"state": "stopped"})
        if request.url.path == "/api/v2/torrents/add":
            assert stopped, "old source must stop before adding the replacement"
            assert request.headers["X-Arr-Token"] == "worker-secret"
            assert new_torrent in request.content
            replacement = permits.get_for_reservation(reservation_id)
            assert replacement is not None
            assert request.headers["X-Admission-Permit"] == replacement.token
            permits.authorize(
                token=replacement.token, infohash=replacement.infohash,
                destination=replacement.destination,
                metadata_sha256=replacement.metadata_sha256,
                effect=lambda _: {"accepted": True},
            )
            events.append("add")
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def capacity():
        events.append("capacity")
        return CapacityEvidence(
            free_bytes=2_000_000_000,
            remaining_by_hash={old.infohash: old_metadata.total_bytes},
            paused_hashes=frozenset({old.infohash}) if stopped else frozenset(),
        )

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=capacity,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(tmp_path / "control.sqlite"),
            torrent_store=torrents,
        )
        await acquirer.acquire("movie:tmdb:1101383", reservation_id)

    replacement = permits.get_for_reservation(reservation_id)
    assert replacement is not None and replacement.infohash == new_metadata.infohash
    assert replacement.state == "confirmed"
    assert permits.is_admitted(old.infohash) is False
    assert events.index("stop") < events.index("capacity") < events.index("add")


@pytest.mark.asyncio
async def test_movie_keeps_stalled_source_when_alternatives_have_no_seeders(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
    old_torrent = _torrent(subtitle=True)
    old_metadata = inspect_torrent(old_torrent)
    old = permits.issue(
        infohash=old_metadata.infohash, metadata_sha256=old_metadata.metadata_sha256,
        destination="/data/torrents", category="radarr", reservation_id=reservation_id,
        selected_files=tuple(item.path for item in old_metadata.files),
        budget_bytes=old_metadata.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        capacity=CapacityEvidence(free_bytes=2_000_000_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    torrents = TorrentArtifactStore(repo.path)
    torrents.put(old, old_torrent)
    SourceHealthStore(tmp_path / "control.sqlite").observe(
        old.permit_id,
        TorrentHealth(
            infohash=old.infohash, downloaded=0,
            amount_left=old_metadata.total_bytes, num_seeds=0, dlspeed=0,
            state="stalledDL", progress=0.0,
        ),
        now=time.time() - 31 * 60,
    )
    web_torrent = _torrent(subtitle=True, video_bytes=1_400_000_000)
    web_metadata = inspect_torrent(web_torrent)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/torrent-health":
            assert request.headers["X-Admission-Permit"] == old.token
            assert "permit_token" not in request.url.params
            return httpx.Response(200, json={
                "hash": old.infohash, "downloaded": 0,
                "amount_left": old_metadata.total_bytes,
                "num_seeds": 0, "dlspeed": 0,
                "state": "stalledDL", "progress": 0.0,
            })
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{
                "id": 2, "tmdbId": 1101383, "hasFile": False,
            }])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": "old", "title": "Film 1080p BluRay REMUX",
                "size": old_metadata.total_bytes, "seeders": 0,
                "downloadUrl": "http://prowlarr:9696/2/download?id=old",
                "infoHash": old.infohash, "rejected": False,
                "quality": {"quality": {
                    "source": "bluray", "modifier": "remux", "resolution": 1080,
                }},
            }, {
                "guid": "web", "title": "Film 2160p WEB-DL DV Atmos",
                "size": web_metadata.total_bytes, "seeders": 0,
                "downloadUrl": "http://prowlarr:9696/2/download?id=web",
                "infoHash": web_metadata.infohash, "rejected": False,
                "quality": {"quality": {
                    "source": "webdl", "modifier": "none", "resolution": 2160,
                }},
            }])
        if request.url.path == "/internal/source-state":
            raise AssertionError("unseeded alternative must not stop the old source")
        if request.url.path == "/api/v2/torrents/add":
            raise AssertionError("unseeded alternative must not be added")
        if request.url.path == "/2/download":
            return httpx.Response(200, content={
                "old": old_torrent, "web": web_torrent,
            }[request.url.params["id"]])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=lambda: None,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(tmp_path / "control.sqlite"),
            torrent_store=torrents,
        )
        await acquirer.acquire("movie:tmdb:1101383", reservation_id)

    assert permits.get_for_reservation(reservation_id).permit_id == old.permit_id
    assert permits.is_admitted(old.infohash)


@pytest.mark.asyncio
@pytest.mark.parametrize("collision", ["selected", "extra"])
async def test_movie_does_not_replace_with_torrent_writing_an_existing_path(
    tmp_path, collision,
):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
    shared_extra = [b"Extras", b"shared.bin"] if collision == "extra" else None
    old_torrent = _torrent(subtitle=True, extra_video_path=shared_extra)
    overlapping_torrent = _torrent(
        subtitle=True, video_bytes=1_600_000_000,
        extra_video_path=shared_extra,
        video_name=(b"movie.alternative.mkv" if collision == "extra"
                    else b"movie.mkv"),
        subtitle_name=(b"movie.alternative.pt-BR.srt" if collision == "extra"
                       else b"movie.pt-BR.srt"),
    )
    old_metadata = inspect_torrent(old_torrent)
    overlapping_metadata = inspect_torrent(overlapping_torrent)
    assert old_metadata.infohash != overlapping_metadata.infohash
    assert {item.path for item in old_metadata.files}.intersection(
        item.path for item in overlapping_metadata.files
    )
    old_selected = tuple(
        item.path for item in old_metadata.files
        if item.path.endswith((".mkv", ".srt"))
    )
    overlapping_selected = tuple(
        item.path for item in overlapping_metadata.files
        if item.path.endswith((".mkv", ".srt"))
    )
    if collision == "extra":
        assert set(old_selected).isdisjoint(overlapping_selected)
    old = permits.issue(
        infohash=old_metadata.infohash, metadata_sha256=old_metadata.metadata_sha256,
        destination="/data/torrents", category="radarr", reservation_id=reservation_id,
        selected_files=old_selected,
        budget_bytes=old_metadata.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        capacity=CapacityEvidence(free_bytes=2_000_000_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    torrents = TorrentArtifactStore(repo.path)
    torrents.put(old, old_torrent)
    SourceHealthStore(tmp_path / "control.sqlite").observe(
        old.permit_id,
        TorrentHealth(
            infohash=old.infohash, downloaded=0,
            amount_left=old_metadata.total_bytes, num_seeds=0, dlspeed=0,
            state="stalledDL", progress=0.0,
        ),
        now=time.time() - 31 * 60,
    )
    metadata_fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/torrent-health":
            assert request.headers["X-Admission-Permit"] == old.token
            assert "permit_token" not in request.url.params
            return httpx.Response(200, json={
                "hash": old.infohash, "downloaded": 0,
                "amount_left": old_metadata.total_bytes,
                "num_seeds": 0, "dlspeed": 0,
                "state": "stalledDL", "progress": 0.0,
            })
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{
                "id": 2, "tmdbId": 1101383, "hasFile": False,
            }])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": guid, "title": "Film 1080p BluRay REMUX DV Atmos",
                "size": metadata.total_bytes, "seeders": seeds,
                "downloadUrl": f"http://prowlarr:9696/2/download?id={guid}",
                "infoHash": metadata.infohash, "rejected": False,
                "quality": {"quality": {
                    "source": "bluray", "modifier": "remux", "resolution": 1080,
                }},
            } for guid, metadata, seeds in (
                ("old", old_metadata, 0),
                ("overlap", overlapping_metadata, 45),
            )])
        if request.url.path == "/2/download":
            metadata_fetched.append(request.url.params["id"])
            return httpx.Response(200, content={
                "old": old_torrent, "overlap": overlapping_torrent,
            }[request.url.params["id"]])
        if request.url.path == "/internal/source-state":
            raise AssertionError("overlapping paths must not stop the old source")
        if request.url.path == "/api/v2/torrents/add":
            raise AssertionError("overlapping paths must not be added")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def capacity():
        return CapacityEvidence(
            free_bytes=4_000_000_000,
            remaining_by_hash={old.infohash: old_metadata.total_bytes},
        )

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=capacity,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(tmp_path / "control.sqlite"),
            torrent_store=torrents,
        )
        await acquirer.acquire("movie:tmdb:1101383", reservation_id)

    assert "overlap" in metadata_fetched
    assert permits.get_for_reservation(reservation_id).permit_id == old.permit_id
    assert permits.is_admitted(old.infohash)


@pytest.mark.asyncio
async def test_movie_retains_source_when_original_torrent_metadata_is_missing(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
    old_torrent = _torrent(subtitle=True)
    new_torrent = _torrent(
        subtitle=True, video_bytes=1_600_000_000,
        root_name=b"Film.Alternative", video_name=b"movie.alternative.mkv",
    )
    old_metadata = inspect_torrent(old_torrent)
    new_metadata = inspect_torrent(new_torrent)
    assert {item.path for item in old_metadata.files}.isdisjoint(
        item.path for item in new_metadata.files
    )
    old = permits.issue(
        infohash=old_metadata.infohash, metadata_sha256=old_metadata.metadata_sha256,
        destination="/data/torrents", category="radarr", reservation_id=reservation_id,
        selected_files=tuple(item.path for item in old_metadata.files),
        budget_bytes=old_metadata.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        capacity=CapacityEvidence(free_bytes=2_000_000_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    SourceHealthStore(repo.path).observe(
        old.permit_id,
        TorrentHealth(
            infohash=old.infohash, downloaded=0,
            amount_left=old_metadata.total_bytes, num_seeds=0, dlspeed=0,
            state="stalledDL", progress=0.0,
        ),
        now=time.time() - 31 * 60,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/torrent-health":
            assert request.headers["X-Admission-Permit"] == old.token
            assert "permit_token" not in request.url.params
            return httpx.Response(200, json={
                "hash": old.infohash, "downloaded": 0,
                "amount_left": old_metadata.total_bytes,
                "num_seeds": 0, "dlspeed": 0,
                "state": "stalledDL", "progress": 0.0,
            })
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{
                "id": 2, "tmdbId": 1101383, "hasFile": False,
            }])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[{
                "guid": "new", "title": "Film 1080p BluRay REMUX DV Atmos",
                "size": new_metadata.total_bytes, "seeders": 45,
                "downloadUrl": "http://prowlarr:9696/2/download?id=new",
                "infoHash": new_metadata.infohash, "rejected": False,
                "quality": {"quality": {
                    "source": "bluray", "modifier": "remux", "resolution": 1080,
                }},
            }])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=new_torrent)
        if request.url.path in {"/internal/source-state", "/api/v2/torrents/add"}:
            raise AssertionError("missing original metadata must block replacement")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def capacity():
        return CapacityEvidence(
            free_bytes=2_000_000_000,
            remaining_by_hash={old.infohash: old_metadata.total_bytes},
        )

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=capacity,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(repo.path),
            torrent_store=TorrentArtifactStore(repo.path),
        )
        await acquirer.acquire("movie:tmdb:1101383", reservation_id)

    assert permits.get_for_reservation(reservation_id).permit_id == old.permit_id
    assert permits.is_admitted(old.infohash)


def _uncertain_movie_replacement(tmp_path, *, state):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
    old_torrent = _torrent(subtitle=True)
    new_torrent = _torrent(
        subtitle=True, video_bytes=1_600_000_000,
        root_name=b"Film.Alternative", video_name=b"movie.alternative.mkv",
    )
    old_metadata = inspect_torrent(old_torrent)
    new_metadata = inspect_torrent(new_torrent)
    old = permits.issue(
        infohash=old_metadata.infohash, metadata_sha256=old_metadata.metadata_sha256,
        destination="/data/torrents", category="radarr", reservation_id=reservation_id,
        selected_files=tuple(item.path for item in old_metadata.files),
        budget_bytes=old_metadata.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        capacity=CapacityEvidence(free_bytes=2_000_000_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    torrents = TorrentArtifactStore(repo.path)
    torrents.put(old, old_torrent)
    stopped_capacity = CapacityEvidence(
        free_bytes=2_000_000_000,
        remaining_by_hash={old.infohash: old_metadata.total_bytes},
        paused_hashes=frozenset({old.infohash}),
    )
    replacement = permits.replace_confirmed(
        old.token, infohash=new_metadata.infohash,
        metadata_sha256=new_metadata.metadata_sha256,
        selected_files=tuple(item.path for item in new_metadata.files),
        budget_bytes=new_metadata.total_bytes, capacity=stopped_capacity,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    torrents.put(replacement, new_torrent)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE gateway_permits SET state = ? WHERE permit_id = ?",
            (state, replacement.permit_id),
        )
    assert permits.get(old.token).state == "superseded"
    assert old.infohash in stopped_capacity.paused_hashes
    assert permits.get_for_reservation(reservation_id).state == state
    return repo, permits, reservation_id, old, replacement, torrents, stopped_capacity


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain_state", ["unknown", "dispatching"])
async def test_movie_reconciles_uncertain_replacement_without_duplicate_add(
    tmp_path, uncertain_state,
):
    repo, permits, reservation_id, old, replacement, torrents, capacity = (
        _uncertain_movie_replacement(tmp_path, state=uncertain_state)
    )
    reconciled = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/reconcile-source":
            assert request.headers["X-Arr-Token"] == "worker-secret"
            assert request.read() == (
                '{"permit_token":"' + replacement.token + '"}'
            ).encode()
            reconciled.append(replacement.token)
            permits.confirm_replacement(replacement.token)
            return httpx.Response(200, json={"state": "confirmed"})
        raise AssertionError(
            "reconciliation must not search, add or resume a source: "
            f"{request.method} {request.url}"
        )

    async def capacity_provider():
        return capacity

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=capacity_provider,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(repo.path), torrent_store=torrents,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "replaced"
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == (
            "already_permitted"
        )

    assert reconciled == [replacement.token]
    assert permits.get(old.token).state == "superseded"
    assert permits.get_for_reservation(reservation_id).state == "confirmed"
    assert permits.get_for_reservation(reservation_id).infohash == replacement.infohash


@pytest.mark.asyncio
async def test_movie_missing_uncertain_replacement_requires_manual_reconciliation(tmp_path):
    repo, permits, reservation_id, old, replacement, torrents, capacity = (
        _uncertain_movie_replacement(tmp_path, state="unknown")
    )
    reconciled = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/reconcile-source":
            assert request.headers["X-Arr-Token"] == "worker-secret"
            assert request.read() == (
                '{"permit_token":"' + replacement.token + '"}'
            ).encode()
            reconciled.append(replacement.token)
            return httpx.Response(200, json={"state": "missing"})
        raise AssertionError(
            "missing replacement must not search, add or resume a source: "
            f"{request.method} {request.url}"
        )

    async def capacity_provider():
        return capacity

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=capacity_provider,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(repo.path), torrent_store=torrents,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == (
            "replacement_uncertain"
        )

    assert reconciled == [replacement.token]
    assert permits.get(old.token).state == "superseded"
    assert permits.get_for_reservation(reservation_id).state == "unknown"


@pytest.mark.asyncio
async def test_movie_renews_expired_authorized_replacement_before_gateway_add(tmp_path):
    repo, permits, reservation_id, old, replacement, torrents, stopped_capacity = (
        _uncertain_movie_replacement(tmp_path, state="authorized")
    )
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE gateway_permits SET expires_at = ? WHERE permit_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
             replacement.permit_id),
        )
    assert permits.get(replacement.token).expires_at <= datetime.now(UTC)
    fresh_snapshots = []
    additions = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/torrents/add":
            assert request.headers["X-Arr-Token"] == "worker-secret"
            assert request.headers["X-Admission-Permit"] == replacement.token
            assert torrents.get(replacement) in request.content
            current = permits.get_for_reservation(reservation_id)
            assert current is not None and current.permit_id == replacement.permit_id
            assert current.expires_at > datetime.now(UTC)
            permits.authorize(
                token=current.token, infohash=current.infohash,
                destination=current.destination,
                metadata_sha256=current.metadata_sha256,
                effect=lambda _: {"accepted": True},
            )
            additions.append(current.permit_id)
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(
            "renewal must not search releases or restart the old source: "
            f"{request.method} {request.url}"
        )

    async def capacity_provider():
        fresh_snapshots.append(True)
        return stopped_capacity

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = MovieAcquirer(
            repository=repo, permits=permits, radarr_url="http://radarr:7878",
            radarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=capacity_provider,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(repo.path), torrent_store=torrents,
        )
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == "replaced"
        assert await acquirer.acquire("movie:tmdb:1101383", reservation_id) == (
            "already_permitted"
        )

    assert len(fresh_snapshots) == 1
    assert additions == [replacement.permit_id]
    assert permits.get(old.token).state == "superseded"
    assert old.infohash in stopped_capacity.paused_hashes
    assert permits.get_for_reservation(reservation_id).permit_id == replacement.permit_id
    assert permits.get_for_reservation(reservation_id).state == "confirmed"
