from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.series_acquisition import SeriesAcquirer, _series_rank
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
    *, video_bytes=3_000_000_000, subtitle=b"Ted.Lasso.S04E01.pt-BR.srt",
    extra_video_path: list[bytes] | None = None,
):
    files = [{b"length": video_bytes, b"path": [b"Ted.Lasso.S04E01.mkv"]}]
    if extra_video_path is not None:
        files.append({b"length": 79_000_000, b"path": extra_video_path})
    if subtitle is not None:
        files.append({b"length": 1000, b"path": [subtitle]})
    total = sum(item[b"length"] for item in files)
    info = {
        b"files": files, b"name": b"Ted.Lasso.S04E01",
        b"piece length": 16_777_216,
        b"pieces": b"a" * (20 * ((total + 16_777_215) // 16_777_216)),
    }
    return _bencode({b"info": info})


def _reserve(tmp_path, *, budget=100_000_000_000):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    result = repo.reserve(
        request_id="seerr:3:4", source_id="3:4",
        media_key="season:tmdb:97546:4", filesystem_id="fixture-fs",
        budget_bytes=budget,
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
async def test_series_requires_hardlink_import_before_grab(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": False})
        raise AssertionError("release search must not start without hardlinks")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == "import_guard"


def test_series_manifest_requires_one_episode_and_pt_br():
    assert SeriesAcquirer._eligible_episode_manifest(
        _torrent(), season=4, episode=1
    ) is not None
    assert SeriesAcquirer._eligible_episode_manifest(
        _torrent(video_bytes=5_000_000_001), season=4, episode=1
    ) is not None


def test_series_manifest_excludes_sample_video_but_rejects_second_episode():
    with_sample = _torrent(
        subtitle=None, extra_video_path=[b"Sample", b"Ted.Lasso.S04E01.sample.mkv"]
    )
    manifest = SeriesAcquirer._eligible_episode_manifest(
        with_sample, season=4, episode=1, allow_external_subtitle=True
    )
    assert manifest is not None
    assert len(manifest[2]) == 1
    assert manifest[2][0].endswith("/Ted.Lasso.S04E01.mkv")

    with_second_episode = _torrent(
        subtitle=None, extra_video_path=[b"Ted.Lasso.S04E02.mkv"]
    )
    assert SeriesAcquirer._eligible_episode_manifest(
        with_second_episode, season=4, episode=1, allow_external_subtitle=True
    ) is None


def test_sonarr_web_label_is_allowed_only_when_arr_classifies_webdl():
    release = {
        "title": "Ted Lasso S04E01 1080p WEB H264 CAKES",
        "size": 3_000_000_000,
        "quality": {"quality": {
            "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
        }},
    }
    assert _series_rank(release) is not None
    assert _series_rank({**release, "title": "Ted Lasso S04E01 1080p WEBRip CAKES"}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("subtitle_available", [True, False])
async def test_series_grab_uses_persisted_exact_release_subdl_sidecar(tmp_path, subtitle_available):
    repo, permits, reservation_id = _reserve(tmp_path)
    store = SubtitleArtifactStore(repo.path)
    torrents = TorrentArtifactStore(repo.path)
    torrent = _torrent(subtitle=None)
    inspected = inspect_torrent(torrent)
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda brasileira\n"
    release = {
        "guid": "cakes", "indexerId": 2,
        "title": "Ted Lasso S04E01 1080p WEB H264 CAKES",
        "size": 3_000_000_000,
        "downloadUrl": "http://prowlarr:9696/2/download?id=1",
        "infoHash": inspected.infohash, "rejected": False,
        "quality": {"quality": {
            "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
        }}, "protocol": "torrent", "episodeIds": [44],
    }
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 44, "seasonNumber": 4, "episodeNumber": 1,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "monitored": True, "hasFile": False,
            }])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[release])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        if request.url.host == "api.subdl.com":
            return httpx.Response(200, json={
                "status": True, "results": [{"tmdb_id": 97546, "type": "tv"}],
                "subtitles": [{
                    "language": "BR_PT", "season": 4, "episode": 1,
                    "unpack_files": [{
                        "language": "BR_PT", "season": 4, "episode": 1,
                        "release_name": "Ted.Lasso.S04E01.1080p.WEB.H264-CAKES",
                        "format": "srt", "size": len(srt),
                        "url": "/subtitle/123/abc",
                    }],
                }] if subtitle_available else [],
            })
        if request.url.host == "dl.subdl.com":
            return httpx.Response(200, content=srt)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            assert store.get(reservation_id, "S04E01", inspected.infohash) == (
                srt if subtitle_available else None
            )
            posts.append(request)
            return httpx.Response(200, json=release)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
            subtitle_source=SubDLSource(api_key="test-key", client=client),
            subtitle_store=store, torrent_store=torrents,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == "grabbed"
    assert len(posts) == 1
    permit = permits.get_for_reservation(reservation_id, scope_key="S04E01")
    assert permit is not None
    assert permit.selected_files == ("Ted.Lasso.S04E01/Ted.Lasso.S04E01.mkv",)
    assert torrents.get(permit) == torrent
    assert SeriesAcquirer._eligible_episode_manifest(
        _torrent(subtitle=b"Ted.Lasso.S04E01.pt-PT.srt"), season=4, episode=1
    ) is None
    assert SeriesAcquirer._eligible_episode_manifest(
        _torrent(), season=4, episode=2
    ) is None


@pytest.mark.asyncio
async def test_series_acquirer_grabs_only_due_episode_with_season_permit(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
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
    large_torrent = _torrent(video_bytes=6_000_000_000)
    large_release = {
        **release, "guid": "episode-remux", "title": "Ted Lasso S04E01 2160p BluRay REMUX",
        "size": inspect_torrent(large_torrent).total_bytes,
        "downloadUrl": "http://prowlarr:9696/2/download?id=2",
        "infoHash": inspect_torrent(large_torrent).infohash,
        "quality": {"quality": {
            "source": "bluray", "name": "Remux-2160p", "resolution": 2160,
        }},
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
            return httpx.Response(200, json=[large_release, release])
        if request.url.path == "/2/download":
            return httpx.Response(
                200, content=large_torrent if request.url.params["id"] == "2" else torrent
            )
        if request.url.path == "/api/v3/release" and request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json=release)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        async def capacity():
            return CapacityEvidence(free_bytes=4_000_000_000, remaining_by_hash={})
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
            capacity_provider=capacity,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == "grabbed"
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) != "grabbed"
    assert len(posts) == 1
    assert b'"guid":"episode-one"' in posts[0].read()
    permit = permits.get_for_reservation(reservation_id, scope_key="S04E01")
    assert permit is not None
    assert permit.infohash == inspected.infohash
    assert permit.category == "sonarr"
    assert permit.budget_bytes == 3_000_001_000
