from __future__ import annotations

import json
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
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.series_acquisition import SeriesAcquirer, _series_rank
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
    *, video_bytes=3_000_000_000, subtitle=b"Ted.Lasso.S04E01.pt-BR.srt",
    extra_video_path: list[bytes] | None = None, extra_subtitle: bytes | None = None,
    season=4, episode=1, video_path=b"Ted.Lasso.S04E01.mkv",
    root_suffix: bytes = b"",
):
    episode_tag = f"S{season:02d}E{episode:02d}".encode()
    files = [{b"length": video_bytes, b"path": [
        video_path.replace(b"S04E01", episode_tag)
    ]}]
    if extra_video_path is not None:
        files.append({b"length": 79_000_000, b"path": extra_video_path})
    if subtitle is not None:
        files.append({b"length": 1000, b"path": [subtitle.replace(b"S04E01", episode_tag)]})
    if extra_subtitle is not None:
        files.append({b"length": 1000, b"path": [
            extra_subtitle.replace(b"S04E01", episode_tag)
        ]})
    total = sum(item[b"length"] for item in files)
    info = {
        b"files": files, b"name": b"Ted.Lasso." + episode_tag + root_suffix,
        b"piece length": 16_777_216,
        b"pieces": b"a" * (20 * ((total + 16_777_215) // 16_777_216)),
    }
    return _bencode({b"info": info})


def _reserve(tmp_path, *, budget=100_000_000_000, season=4):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    result = repo.reserve(
        request_id=f"seerr:3:{season}", source_id=f"3:{season}",
        media_key=f"season:tmdb:97546:{season}", filesystem_id="fixture-fs",
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


def _completed_earlier_seasons(season: int):
    return [
        {"id": 100 + number, "seasonNumber": number, "episodeNumber": 1,
         "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
         "monitored": True, "hasFile": True}
        for number in range(1, season)
    ]


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


def test_series_manifest_selects_english_sidecar_only_when_pt_br_is_absent():
    english = _torrent(subtitle=b"Ted.Lasso.S04E01.en.srt")
    english_manifest = SeriesAcquirer._eligible_episode_manifest(
        english, season=4, episode=1
    )
    assert english_manifest is not None
    assert english_manifest[2] == (
        "Ted.Lasso.S04E01/Ted.Lasso.S04E01.mkv",
        "Ted.Lasso.S04E01/Ted.Lasso.S04E01.en.srt",
    )

    both = _torrent(extra_subtitle=b"Ted.Lasso.S04E01.en.srt")
    preferred = SeriesAcquirer._eligible_episode_manifest(both, season=4, episode=1)
    assert preferred is not None
    assert preferred[2] == (
        "Ted.Lasso.S04E01/Ted.Lasso.S04E01.mkv",
        "Ted.Lasso.S04E01/Ted.Lasso.S04E01.pt-BR.srt",
    )


def test_series_manifest_ignores_subtitle_for_a_different_episode():
    wrong = _torrent(subtitle=b"Ted.Lasso.S04E02.pt-BR.srt")
    assert SeriesAcquirer._eligible_episode_manifest(
        wrong, season=4, episode=1
    ) is None
    external = SeriesAcquirer._eligible_episode_manifest(
        wrong, season=4, episode=1, allow_external_subtitle=True
    )
    assert external is not None
    assert external[2] == ("Ted.Lasso.S04E01/Ted.Lasso.S04E01.mkv",)

    matching_english = _torrent(
        subtitle=b"Ted.Lasso.S04E02.pt-BR.srt",
        extra_subtitle=b"Ted.Lasso.S04E01.en.srt",
    )
    selected = SeriesAcquirer._eligible_episode_manifest(
        matching_english, season=4, episode=1
    )
    assert selected is not None
    assert selected[2] == (
        "Ted.Lasso.S04E01/Ted.Lasso.S04E01.mkv",
        "Ted.Lasso.S04E01/Ted.Lasso.S04E01.en.srt",
    )

    wrong_video = _torrent(video_path=b"Ted.Lasso.S04E02.mkv")
    assert SeriesAcquirer._eligible_episode_manifest(
        wrong_video, season=4, episode=1
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


def test_series_release_rank_uses_seeds_only_within_same_quality() -> None:
    base = {
        "title": "Ted Lasso S04E01 1080p WEB-DL Atmos", "size": 3_000,
        "quality": {"quality": {
            "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
        }},
    }
    assert _series_rank({**base, "seeders": 50, "size": 1_000}) > _series_rank(
        {**base, "seeders": 1, "size": 3_000}
    )


@pytest.mark.asyncio
async def test_series_chooses_more_seeded_source_for_first_missing_episode(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    low = _torrent(video_bytes=3_100_000_000, root_suffix=b".Low")
    high = _torrent(video_bytes=3_000_000_000, root_suffix=b".High")
    torrents = {"low": low, "high": high}
    searched = []
    grabbed = []

    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[*_completed_earlier_seasons(4), *(
                {"id": 40 + number, "seasonNumber": 4, "episodeNumber": number,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False}
                for number in (1, 2)
            )])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            searched.append(request.url.params["episodeId"])
            assert request.url.params["episodeId"] == "41"
            return httpx.Response(200, json=[{
                "guid": name, "title": "Ted Lasso S04E01 1080p WEB-DL Atmos",
                "size": inspect_torrent(torrent).total_bytes,
                "seeders": 2 if name == "low" else 40,
                "downloadUrl": f"http://prowlarr:9696/2/download?id={name}",
                "infoHash": inspect_torrent(torrent).infohash, "rejected": False,
                "quality": {"quality": {
                    "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
                }}, "episodeIds": [41],
            } for name, torrent in torrents.items()])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrents[request.url.params["id"]])
        if request.url.path == "/api/v3/release" and request.method == "POST":
            grabbed.append(json.loads(request.content)["guid"])
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == "grabbed"
    assert searched == ["41"]
    assert grabbed == ["high"]
    assert permits.get_for_reservation(reservation_id, scope_key="S04E01").reported_seeders == 40


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
            return httpx.Response(200, json=[*_completed_earlier_seasons(4), {
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
                *_completed_earlier_seasons(4),
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


@pytest.mark.asyncio
async def test_series_does_not_grab_later_episode_while_earliest_has_no_release(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent = _torrent(episode=7)
    inspected = inspect_torrent(torrent)
    searched = []
    grabbed = []
    release = {
        "guid": "episode-seven", "indexerId": 2,
        "title": "Ted Lasso S04E07 1080p WEB-DL",
        "size": inspected.total_bytes,
        "downloadUrl": "http://prowlarr:9696/2/download?id=7",
        "infoHash": inspected.infohash, "rejected": False,
        "quality": {"quality": {
            "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
        }}, "protocol": "torrent", "episodeIds": [47],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                *_completed_earlier_seasons(4),
                *[
                    {"id": episode_id, "seasonNumber": 4, "episodeNumber": number,
                     "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                     "monitored": True, "hasFile": number in {1, 2, 3, 4, 5, 8}}
                    for episode_id, number in ((46, 6), (47, 7), (48, 8))
                ],
            ])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            episode_id = request.url.params["episodeId"]
            searched.append(episode_id)
            return httpx.Response(200, json=[release] if episode_id == "47" else [])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            grabbed.append(request.read())
            return httpx.Response(200, json=release)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == (
            "no_eligible_release"
        )
    assert searched == ["46"]
    assert grabbed == []
    assert permits.get_for_reservation(reservation_id, scope_key="S04E07") is None


@pytest.mark.asyncio
async def test_series_waits_for_downloaded_episode_import_before_grabbing_next(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path)
    torrent6 = _torrent(episode=6)
    inspected6 = inspect_torrent(torrent6)
    permit = permits.issue(
        infohash=inspected6.infohash, metadata_sha256=inspected6.metadata_sha256,
        destination="/data/torrents", category="sonarr", reservation_id=reservation_id,
        scope_key="S04E06", selected_files=(inspected6.files[0].path,),
        budget_bytes=inspected6.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"result": "ok"},
    )
    torrent7 = _torrent(episode=7)
    inspected7 = inspect_torrent(torrent7)
    release7 = {
        "guid": "episode-seven", "indexerId": 2,
        "title": "Ted Lasso S04E07 1080p WEB-DL",
        "size": inspected7.total_bytes,
        "downloadUrl": "http://prowlarr:9696/2/download?id=7",
        "infoHash": inspected7.infohash, "rejected": False,
        "quality": {"quality": {
            "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
        }}, "protocol": "torrent", "episodeIds": [47],
    }
    imported6 = False
    searched = []
    grabbed = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                *_completed_earlier_seasons(4),
                {"id": 46, "seasonNumber": 4, "episodeNumber": 6,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": imported6},
                {"id": 47, "seasonNumber": 4, "episodeNumber": 7,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False},
            ])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            searched.append(request.url.params["episodeId"])
            return httpx.Response(200, json=[release7])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent7)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            grabbed.append(request.read())
            return httpx.Response(200, json=release7)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == (
            "waiting_episodes"
        )
        assert searched == []
        imported6 = True
        assert repo.claim_episode_import(permit.permit_id)
        repo.record_episode_import(permit.permit_id, "sonarr-import-6")
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == (
            "waiting_episodes"
        )
        assert searched == []
        repo.complete_episode_import(permit.permit_id)
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == "grabbed"
    assert searched == ["47"]
    assert len(grabbed) == 1
    assert permits.get_for_reservation(reservation_id, scope_key="S04E07") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_season, missing_season", [(2, 1), (3, 2)])
async def test_series_waits_for_every_earlier_season_before_grabbing(
    tmp_path, target_season, missing_season,
):
    for earlier in range(1, target_season):
        _reserve(tmp_path, season=earlier)
    repo, permits, reservation_id = _reserve(tmp_path, season=target_season)
    torrent = _torrent(season=target_season)
    inspected = inspect_torrent(torrent)
    release = {
        "guid": "first-episode", "indexerId": 2,
        "title": f"Ted Lasso S{target_season:02d}E01 1080p WEB-DL",
        "size": inspected.total_bytes,
        "downloadUrl": "http://prowlarr:9696/2/download?id=1",
        "infoHash": inspected.infohash, "rejected": False,
        "quality": {"quality": {
            "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
        }}, "protocol": "torrent", "episodeIds": [200 + target_season],
    }
    missing_imported = False
    searched = []
    grabbed = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            earlier = _completed_earlier_seasons(target_season)
            for item in earlier:
                if item["seasonNumber"] == missing_season:
                    item["hasFile"] = missing_imported
            return httpx.Response(200, json=[*earlier, {
                "id": 200 + target_season, "seasonNumber": target_season,
                "episodeNumber": 1,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "monitored": True, "hasFile": False,
            }])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            searched.append(request.url.params["episodeId"])
            return httpx.Response(200, json=[release])
        if request.url.path == "/2/download":
            return httpx.Response(200, content=torrent)
        if request.url.path == "/api/v3/release" and request.method == "POST":
            grabbed.append(request.read())
            return httpx.Response(200, json=release)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    media_key = f"season:tmdb:97546:{target_season}"
    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire(media_key, reservation_id) == "waiting_previous_season"
        assert searched == []
        missing_imported = True
        assert await acquirer.acquire(media_key, reservation_id) == "grabbed"
    assert searched == [str(200 + target_season)]
    assert len(grabbed) == 1
    assert permits.get_for_reservation(
        reservation_id, scope_key=f"S{target_season:02d}E01"
    ) is not None


@pytest.mark.asyncio
async def test_series_does_not_block_requested_season_for_unrequested_earlier_seasons(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, season=4)
    searched = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            earlier = _completed_earlier_seasons(4)
            for item in earlier:
                item["hasFile"] = False
            return httpx.Response(200, json=[*earlier, {
                "id": 46, "seasonNumber": 4, "episodeNumber": 6,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "monitored": True, "hasFile": False,
            }])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            searched.append(request.url.params["episodeId"])
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == (
            "no_eligible_release"
        )
    assert searched == ["46"]


@pytest.mark.asyncio
async def test_series_reconciles_existing_torrents_across_seasons_in_order(tmp_path):
    _, _, season1_reservation = _reserve(tmp_path, season=1)
    repo, permits, season2_reservation = _reserve(tmp_path, season=2)
    episode_files = {(1, 1): False, (1, 2): False, (2, 1): False}
    torrent_states = {}
    actions = []

    for (season, episode), reservation_id in (
        ((1, 1), season1_reservation),
        ((1, 2), season1_reservation),
        ((2, 1), season2_reservation),
    ):
        inspected = inspect_torrent(_torrent(season=season, episode=episode))
        permit = permits.issue(
            infohash=inspected.infohash, metadata_sha256=inspected.metadata_sha256,
            destination="/data/torrents", category="sonarr", reservation_id=reservation_id,
            scope_key=f"S{season:02d}E{episode:02d}",
            selected_files=(inspected.files[0].path,), budget_bytes=inspected.total_bytes,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        permits.authorize(
            token=permit.token, infohash=permit.infohash,
            destination=permit.destination, metadata_sha256=permit.metadata_sha256,
            effect=lambda _: {"accepted": True},
        )
        torrent_states[permit.token] = "downloading"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": season * 10 + episode, "seasonNumber": season,
                 "episodeNumber": episode,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": imported}
                for (season, episode), imported in episode_files.items()
            ])
        if request.url.path == "/internal/series-queue-state":
            assert request.headers["X-Arr-Token"] == "worker-secret"
            body = json.loads(request.content)
            token, action = body["permit_token"], body["action"]
            actions.append((token, action))
            torrent_states[token] = "downloading" if action == "start" else "stoppedDL"
            return httpx.Response(
                200, json={"state": "started" if action == "start" else "stopped"}
            )
        if request.url.path == "/api/v3/release" and request.method == "GET":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        async def capacity():
            return CapacityEvidence(
                free_bytes=4_000_000_000,
                remaining_by_hash={permit.infohash: 2_000_000_000 for permit in (
                    permits.get_for_reservation(season1_reservation, scope_key="S01E01"),
                    permits.get_for_reservation(season1_reservation, scope_key="S01E02"),
                    permits.get_for_reservation(season2_reservation, scope_key="S02E01"),
                ) if permit is not None},
                other_pending_bytes=1_000_000_000,
                paused_hashes=frozenset(
                    permit.infohash for permit in (
                        permits.get_for_reservation(season1_reservation, scope_key="S01E01"),
                        permits.get_for_reservation(season1_reservation, scope_key="S01E02"),
                        permits.get_for_reservation(season2_reservation, scope_key="S02E01"),
                    ) if permit is not None
                ),
            )

        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            capacity_provider=capacity,
        )
        assert await acquirer.acquire("season:tmdb:97546:1", season1_reservation) == (
            "waiting_episodes"
        )
        assert await acquirer.acquire("season:tmdb:97546:2", season2_reservation) == (
            "waiting_previous_season"
        )
        first = permits.get_for_reservation(season1_reservation, scope_key="S01E01")
        second = permits.get_for_reservation(season1_reservation, scope_key="S01E02")
        third = permits.get_for_reservation(season2_reservation, scope_key="S02E01")
        assert first is not None and second is not None and third is not None
        assert torrent_states[first.token] == "downloading"
        assert torrent_states[second.token] == "stoppedDL"
        assert torrent_states[third.token] == "stoppedDL"

        episode_files[(1, 1)] = True
        assert repo.claim_episode_import(first.permit_id)
        repo.record_episode_import(first.permit_id, "sonarr-import-11")
        await acquirer.acquire("season:tmdb:97546:1", season1_reservation)
        assert torrent_states[second.token] == "stoppedDL"
        assert torrent_states[third.token] == "stoppedDL"
        repo.complete_episode_import(first.permit_id)
        await acquirer.acquire("season:tmdb:97546:1", season1_reservation)
        assert torrent_states[second.token] == "downloading"
        assert torrent_states[third.token] == "stoppedDL"

        episode_files[(1, 2)] = True
        assert repo.claim_episode_import(second.permit_id)
        repo.record_episode_import(second.permit_id, "sonarr-import-12")
        await acquirer.acquire("season:tmdb:97546:2", season2_reservation)
        assert torrent_states[third.token] == "stoppedDL"
        repo.complete_episode_import(second.permit_id)
        await acquirer.acquire("season:tmdb:97546:2", season2_reservation)
        assert torrent_states[third.token] == "downloading"
    assert (second.token, "stop") in actions
    assert (third.token, "stop") in actions
    assert (second.token, "start") in actions
    assert (third.token, "start") in actions


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity_state, expected", [
    ("insufficient", "waiting_space"),
    ("unavailable", "capacity_unavailable"),
])
async def test_series_stops_later_torrents_but_does_not_resume_without_space(
    tmp_path, capacity_state, expected,
):
    repo, permits, reservation_id = _reserve(tmp_path, season=1)
    issued = []
    for episode in (1, 2):
        inspected = inspect_torrent(_torrent(season=1, episode=episode))
        permit = permits.issue(
            infohash=inspected.infohash, metadata_sha256=inspected.metadata_sha256,
            destination="/data/torrents", category="sonarr", reservation_id=reservation_id,
            scope_key=f"S01E{episode:02d}", selected_files=(inspected.files[0].path,),
            budget_bytes=inspected.total_bytes,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        permits.authorize(
            token=permit.token, infohash=permit.infohash,
            destination=permit.destination, metadata_sha256=permit.metadata_sha256,
            effect=lambda _: {"accepted": True},
        )
        issued.append(permit)
    actions = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 10 + episode, "seasonNumber": 1, "episodeNumber": episode,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False}
                for episode in (1, 2)
            ])
        if request.url.path == "/internal/series-queue-state":
            actions.append(json.loads(request.content))
            return httpx.Response(200, json={"state": "stopped"})
        if request.url.path == "/api/v3/release" and request.method == "GET":
            raise AssertionError("capacity failure must not search a new release")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def capacity():
        if capacity_state == "unavailable":
            raise ValueError("stale filesystem snapshot")
        return CapacityEvidence(free_bytes=1_000_000_000, remaining_by_hash={})

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            capacity_provider=capacity,
        )
        assert await acquirer.acquire("season:tmdb:97546:1", reservation_id) == expected
    assert actions == [
        {"permit_token": issued[1].token, "action": "stop"},
        {"permit_token": issued[0].token, "action": "stop"},
    ]


@pytest.mark.asyncio
async def test_unconfirmed_permit_does_not_block_preexisting_episode_file(tmp_path):
    repo, permits, season1_reservation = _reserve(tmp_path, season=1)
    _, _, season2_reservation = _reserve(tmp_path, season=2)
    inspected = inspect_torrent(_torrent(season=1))
    permits.issue(
        infohash=inspected.infohash, metadata_sha256=inspected.metadata_sha256,
        destination="/data/torrents", category="sonarr",
        reservation_id=season1_reservation, scope_key="S01E01",
        selected_files=(inspected.files[0].path,), budget_bytes=inspected.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    searched = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546, "monitored": True}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 11, "seasonNumber": 1, "episodeNumber": 1,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": True},
                {"id": 21, "seasonNumber": 2, "episodeNumber": 1,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False},
            ])
        if request.url.path == "/api/v3/release" and request.method == "GET":
            searched.append(request.url.params["episodeId"])
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits,
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret",
            prowlarr_url="http://prowlarr:9696", client=client,
        )
        assert await acquirer.acquire("season:tmdb:97546:2", season2_reservation) == (
            "no_eligible_release"
        )
    assert searched == ["21"]


@pytest.mark.asyncio
async def test_series_replaces_stalled_first_episode_without_starting_later_episode(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
    old_torrent = _torrent()
    new_torrent = _torrent(
        video_bytes=3_100_000_000,
        video_path=b"Ted.Lasso.S04E01.Alternative.mkv",
        root_suffix=b".Alternative",
    )
    old_metadata = inspect_torrent(old_torrent)
    new_metadata = inspect_torrent(new_torrent)
    assert {item.path for item in old_metadata.files}.isdisjoint(
        item.path for item in new_metadata.files
    )
    old = permits.issue(
        infohash=old_metadata.infohash, metadata_sha256=old_metadata.metadata_sha256,
        destination="/data/torrents", category="sonarr", reservation_id=reservation_id,
        scope_key="S04E01", selected_files=tuple(item.path for item in old_metadata.files),
        budget_bytes=old_metadata.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        capacity=CapacityEvidence(free_bytes=4_000_000_000, remaining_by_hash={}),
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
    stopped = False
    searched = []
    events = []

    def release(guid, metadata, seeds):
        return {
            "guid": guid, "indexerId": 2,
            "title": "Ted Lasso S04E01 1080p WEB-DL Atmos",
            "size": metadata.total_bytes, "seeders": seeds,
            "downloadUrl": f"http://prowlarr:9696/2/download?id={guid}",
            "infoHash": metadata.infohash,
            "rejected": True,
            "rejections": ["Release in queue already meets cutoff: WEBDL-1080p v1"],
            "quality": {"quality": {
                "source": "web", "name": "WEBDL-1080p", "resolution": 1080,
            }},
            "protocol": "torrent", "episodeIds": [41],
        }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stopped
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 97546, "monitored": True,
            }])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                *_completed_earlier_seasons(4),
                {"id": 41, "seasonNumber": 4, "episodeNumber": 1,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False},
                {"id": 42, "seasonNumber": 4, "episodeNumber": 2,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False},
            ])
        if request.url.path == "/internal/series-queue-state":
            assert json.loads(request.content) == {
                "permit_token": old.token, "action": "start",
            }
            return httpx.Response(200, json={"state": "already_started"})
        if request.url.path == "/internal/torrent-health":
            assert request.headers["X-Admission-Permit"] == old.token
            assert "permit_token" not in request.url.params
            return httpx.Response(200, json={
                "hash": old.infohash, "downloaded": 0,
                "amount_left": old_metadata.total_bytes,
                "num_seeds": 0, "dlspeed": 0,
                "state": "stalledDL", "progress": 0.0,
            })
        if request.url.path == "/api/v3/release" and request.method == "GET":
            searched.append(request.url.params["episodeId"])
            assert request.url.params["episodeId"] == "41"
            return httpx.Response(200, json=[
                release("old", old_metadata, 0), release("new", new_metadata, 36),
            ])
        if request.url.path == "/2/download":
            return httpx.Response(200, content={
                "old": old_torrent, "new": new_torrent,
            }[request.url.params["id"]])
        if request.url.path == "/internal/source-state":
            assert json.loads(request.content) == {
                "permit_token": old.token, "action": "stop",
            }
            stopped = True
            events.append("stop")
            return httpx.Response(200, json={"state": "stopped"})
        if request.url.path == "/api/v2/torrents/add":
            assert stopped
            assert new_torrent in request.content
            replacement = permits.get_for_reservation(
                reservation_id, scope_key="S04E01"
            )
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
        events.append("capacity_after_stop" if stopped else "capacity_before_stop")
        return CapacityEvidence(
            free_bytes=4_000_000_000,
            remaining_by_hash={old.infohash: old_metadata.total_bytes},
            paused_hashes=frozenset({old.infohash}) if stopped else frozenset(),
        )

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=capacity,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(tmp_path / "control.sqlite"),
            torrent_store=torrents,
        )
        await acquirer.acquire("season:tmdb:97546:4", reservation_id)

    replacement = permits.get_for_reservation(reservation_id, scope_key="S04E01")
    assert replacement is not None and replacement.infohash == new_metadata.infohash
    assert replacement.state == "confirmed"
    assert permits.is_admitted(old.infohash) is False
    assert permits.get_for_reservation(reservation_id, scope_key="S04E02") is None
    assert searched == ["41"]
    assert events.index("stop") < events.index("capacity_after_stop") < events.index("add")


@pytest.mark.asyncio
async def test_series_reconciles_initial_unknown_episode_without_new_grab(tmp_path):
    repo, permits, reservation_id = _reserve(tmp_path, budget=0)
    torrent = _torrent()
    metadata = inspect_torrent(torrent)
    permit = permits.issue(
        infohash=metadata.infohash, metadata_sha256=metadata.metadata_sha256,
        destination="/data/torrents", category="sonarr", reservation_id=reservation_id,
        scope_key="S04E01", selected_files=tuple(item.path for item in metadata.files),
        budget_bytes=metadata.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        capacity=CapacityEvidence(free_bytes=4_000_000_000, remaining_by_hash={}),
    )
    torrents = TorrentArtifactStore(repo.path)
    torrents.put(permit, torrent)
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE gateway_permits SET state = 'unknown' WHERE permit_id = ?",
            (permit.permit_id,),
        )
    assert not permits.had_superseded(reservation_id, scope_key="S04E01")
    assert permits.get_for_reservation(reservation_id, scope_key="S04E01").state == (
        "unknown"
    )
    reconciled = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 97546, "monitored": True,
            }])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                *_completed_earlier_seasons(4),
                {"id": 41, "seasonNumber": 4, "episodeNumber": 1,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False},
                {"id": 42, "seasonNumber": 4, "episodeNumber": 2,
                 "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                 "monitored": True, "hasFile": False},
            ])
        if request.url.path == "/internal/reconcile-source":
            assert request.headers["X-Arr-Token"] == "worker-secret"
            assert json.loads(request.content) == {"permit_token": permit.token}
            reconciled.append(permit.token)
            with sqlite3.connect(repo.path) as connection:
                connection.execute(
                    "UPDATE gateway_permits SET state = 'confirmed', result_json = ? "
                    "WHERE permit_id = ?",
                    (json.dumps({"accepted": True, "infohash": permit.infohash}),
                     permit.permit_id),
                )
            return httpx.Response(200, json={"state": "confirmed"})
        raise AssertionError(
            "episode reconciliation must not search or add another torrent: "
            f"{request.method} {request.url}"
        )

    async def capacity_provider():
        return CapacityEvidence(
            free_bytes=4_000_000_000,
            remaining_by_hash={permit.infohash: metadata.total_bytes},
        )

    async with httpx.AsyncClient(transport=_transport(handler)) as client:
        acquirer = SeriesAcquirer(
            repository=repo, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", prowlarr_url="http://prowlarr:9696",
            client=client, capacity_provider=capacity_provider,
            gateway_url="http://download-gateway:8081", arr_token="worker-secret",
            health_store=SourceHealthStore(repo.path), torrent_store=torrents,
        )
        assert await acquirer.acquire("season:tmdb:97546:4", reservation_id) == (
            "reconciled"
        )

    assert reconciled == [permit.token]
    assert permits.get_for_reservation(reservation_id, scope_key="S04E01").state == (
        "confirmed"
    )
    assert permits.is_admitted(metadata.infohash)
    assert permits.get_for_reservation(reservation_id, scope_key="S04E02") is None
