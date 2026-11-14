import json

import httpx
import pytest
from test_series_pack_acquisition import SeriesFixture, _encode

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.release_quality import ReleasePolicy


class MetadataQualityFixture(SeriesFixture):
    def __init__(self, tmp_path, *, free_bytes=100_000_000_000):
        super().__init__(tmp_path, episode_count=2, free_bytes=free_bytes)
        self.media = {
            "id": 1, "tmdbId": 101, "title": "Fixture", "qualityProfileId": 3,
            "monitored": True, "runtime": 45, "status": "ended",
            "originalLanguage": {"name": "English"},
        }
        self.parsed_series = dict(self.media)
        self.parsed_episodes = [
            {"id": number, "episodeNumber": number, "seasonNumber": 1, "seriesId": 1}
            for number in (1, 2)
        ]
        self.parsed_titles, self.pushed = [], []
        self.native_quality = {"quality": {
            "source": "web", "resolution": 1080, "name": "WEBDL-1080p",
        }}
        self.offer = self.release("asc-pack", [1, 2]) | {
            "indexer": "Amigos Share Club (Prowlarr)", "indexerId": 8,
            "title": "Fixture S01 WEB-DL Dual-Audio Brazilian Original",
            "seriesId": 1, "seasonNumber": 1, "fullSeason": True,
            "protocol": "torrent", "publishDate": "2026-01-01T00:00:00Z",
            "rejected": True, "rejections": ["WEBDL-480p is not wanted in profile"],
            "quality": {"quality": {
                "source": "web", "resolution": 480, "name": "WEBDL-480p",
            }},
        }
        self.pack_offers = [self.offer]
        self.set_video_resolutions((1080, 1080))

    def set_video_resolutions(self, resolutions):
        files = [
            {b"path": [f"Fixture.S01E{number:02d}.{resolution}p.WEB-DL.mkv".encode()],
             b"length": 1_500_000_000}
            for number, resolution in enumerate(resolutions, 1)
        ] + [
            {b"path": [f"Fixture.S01E{number:02d}.pt-BR.srt".encode()], b"length": 1000}
            for number in (1, 2)
        ]
        total = sum(item[b"length"] for item in files)
        torrent = _encode({b"info": {
            b"name": b"asc-pack", b"files": files, b"piece length": 16_777_216,
            b"pieces": b"a" * (20 * ((total + 16_777_215) // 16_777_216)),
        }})
        self.torrents["asc-pack"] = torrent
        self.offer.update(infoHash=inspect_torrent(torrent).infohash, size=total)

    def handler(self, request):
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[self.media])
        if request.url.path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 3, "name": "Dublado"}])
        if request.url.path == "/api/v3/parse":
            self.parsed_titles.append(request.url.params["title"])
            return httpx.Response(200, json={
                "series": self.parsed_series, "episodes": self.parsed_episodes,
                "parsedEpisodeInfo": {
                    "seasonNumber": 1, "fullSeason": True,
                    "languages": [{"name": "Portuguese"}, {"name": "English"}],
                    "quality": self.native_quality,
                },
            })
        if request.url.path == "/api/v3/release/push":
            self.pushed.append(json.loads(request.content))
            parent = self.permits.get_for_reservation(self.reservation, scope_key="S01PACK")
            assert parent is not None
            self.confirm(parent)
            return httpx.Response(200, json=[{"rejected": False}])
        return super().handler(request)

    def acquirer(self, client, **preferences):
        acquirer = super().acquirer(client, prefer_season_pack=True, **preferences)
        acquirer.release_policy = ReleasePolicy(
            profile_name="Dublado", indexer_priority=("amigosshare", "uindex", "1337x"),
        )
        return acquirer


@pytest.mark.asyncio
async def test_unnamed_resolution_asc_pack_uses_verified_paths_and_native_push(tmp_path):
    fixture = MetadataQualityFixture(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert len(fixture.parsed_titles) == 1
    assert "1080p" in fixture.parsed_titles[0]
    assert len(fixture.pushed) == 1
    assert fixture.pushed[0]["quality"] == fixture.native_quality
    assert fixture.pushed[0]["seriesId"] == 1
    assert fixture.pushed[0]["episodeIds"] == [1, 2]
    assert "requiresNativeDecision" not in fixture.pushed[0]
    assert fixture.posts == []
    parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
    assert parent is not None and parent.state == "confirmed"
    assert parent.quality_rank == (1, 1080, 0, 0, 0, 0)
    assert parent.budget_bytes == 3_000_002_000
    assert TorrentArtifactStore(fixture.repo.path).get(parent) == fixture.torrents["asc-pack"]
    assert fixture.repo.active_reservation(fixture.reservation)["budget_bytes"] == 3_000_002_000
    for number in (1, 2):
        child = fixture.permits.get_for_reservation(
            fixture.reservation, scope_key=f"S01E{number:02d}",
        )
        assert child is not None and child.season_pack_parent_id == parent.permit_id


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"title": "Fixture S01 480p WEB-DL Dual-Audio Brazilian Original"},
    {"title": "Fixture S01 720p WEB-DL Dual-Audio Brazilian Original"},
    {"indexer": "UIndex (Prowlarr)"},
    {"fullSeason": False},
    {"rejections": ["WEBDL-480p is not wanted in profile", "Unknown Series"]},
    {"rejections": ["Quality is below minimum size"]},
])
async def test_metadata_quality_recovery_does_not_expand_native_rejection_defaults(
    tmp_path, changed,
):
    fixture = MetadataQualityFixture(tmp_path)
    fixture.offer.update(changed)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "waiting_complete_season_pack"
    assert fixture.posts == fixture.pushed == []
    assert fixture.parsed_titles == []
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("resolutions", [(480, 480), (1080, 480), (1080, 720)])
async def test_metadata_quality_recovery_rejects_low_or_mixed_resolution_paths(
    tmp_path, resolutions,
):
    fixture = MetadataQualityFixture(tmp_path)
    fixture.set_video_resolutions(resolutions)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "waiting_complete_season_pack"
    assert fixture.posts == fixture.pushed == []
    assert fixture.parsed_titles == []
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["series", "tmdb", "episodes", "quality"])
async def test_metadata_quality_recovery_requires_matching_native_identity(tmp_path, mismatch):
    fixture = MetadataQualityFixture(tmp_path)
    if mismatch == "series":
        fixture.parsed_series["id"] = 2
    elif mismatch == "tmdb":
        fixture.parsed_series["tmdbId"] = 202
    elif mismatch == "episodes":
        fixture.parsed_episodes[1]["id"] = 99
    else:
        fixture.native_quality["quality"].update(resolution=480, name="WEBDL-480p")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "waiting_complete_season_pack"
    assert len(fixture.parsed_titles) == 1
    assert fixture.posts == fixture.pushed == []
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None


@pytest.mark.asyncio
async def test_metadata_quality_recovery_still_requires_capacity_before_dispatch(tmp_path):
    fixture = MetadataQualityFixture(tmp_path, free_bytes=2_000_000_000)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "waiting_complete_season_pack"
    assert len(fixture.parsed_titles) == 1
    assert fixture.posts == fixture.pushed == []
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None


def failing_metadata_handler(fixture, failure):
    def handler(request):
        if (failure == "torrent" and request.url.path == "/1/download"
                and request.url.params["id"] == "asc-pack"):
            return httpx.Response(200, content=b"bad")
        if request.url.path == "/api/v3/parse":
            if failure == "parse_http":
                return httpx.Response(503, json={"message": "Temporarily unavailable"})
            if failure == "parse_json":
                return httpx.Response(200, content=b"not JSON")
        return fixture.handler(request)
    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["torrent", "parse_http", "parse_json"])
async def test_metadata_quality_external_failure_declines_pack_without_aborting_cycle(
    tmp_path, failure,
):
    fixture = MetadataQualityFixture(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(failing_metadata_handler(fixture, failure)),
    ) as client:
        assert await fixture.acquirer(client).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "waiting_complete_season_pack"
    assert fixture.posts == fixture.pushed == []
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None
    assert fixture.repo.active_reservation(fixture.reservation)["budget_bytes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["torrent", "parse_http", "parse_json"])
async def test_metadata_quality_external_failure_keeps_next_native_candidate_eligible(
    tmp_path, failure,
):
    fixture = MetadataQualityFixture(tmp_path)
    healthy = fixture.release("healthy-uindex", [1, 2]) | {
        "seriesId": 1, "seasonNumber": 1, "fullSeason": True,
        "languages": [{"id": 1, "name": "English"}],
    }
    fixture.pack_offers.append(healthy)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(failing_metadata_handler(fixture, failure)),
    ) as client:
        assert await fixture.acquirer(client).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == ["healthy-uindex"]
    assert fixture.pushed == []
    parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
    assert parent is not None and parent.state == "confirmed"
    assert parent.infohash == healthy["infoHash"]
