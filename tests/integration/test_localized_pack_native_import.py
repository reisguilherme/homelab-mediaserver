"""A proved localized pack can bind one unresolved native import to its episode."""

from copy import deepcopy

import httpx
import pytest
from test_series_subtitle_publication import SRT, PublicationFixture

from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.series_identity import SeriesIdentityResolver

LOGICAL = "/data/torrents/pack1/Fixture.Show.S01E01.1080p.WEB-DL.mkv"
QUALITY = {
    "quality": {"id": 3, "name": "WEBDL-1080p", "source": "web", "resolution": 1080},
    "revision": {"version": 1, "real": 0, "isRepack": False},
}
CANDIDATE = {
    "path": LOGICAL, "series": None, "seasonNumber": None, "episodes": [],
    "rejections": [{"reason": "Unknown Series", "type": "permanent"}],
    "quality": QUALITY, "languages": [{"id": 0, "name": "Unknown"}],
    "releaseGroup": "Fixture", "indexerFlags": 0, "releaseType": "unknown",
}


class LocalizedFixture(PublicationFixture):
    def __init__(self, tmp_path, monkeypatch, *, fault=None):
        super().__init__(tmp_path, monkeypatch)
        self.repo.set_series_subtitle_publication(74606, enabled=True)
        self.manual_reads = []
        self.candidates = [deepcopy(CANDIDATE)]
        self.episode_fault = None
        self.with_alias = True
        if fault:
            fault(self)

    def handler(self, request):
        if request.url.path == "/api/v3/manualimport":
            self.manual_reads.append(dict(request.url.params))
            return httpx.Response(200, json=self.candidates)
        response = super().handler(request)
        if request.url.path == "/api/v3/series":
            rows = response.json()
            rows[0]["title"] = "Canonical Native Show"
            rows[0]["tvdbId"] = 293012
            return httpx.Response(200, json=rows)
        if request.url.path == "/api/v3/episode":
            rows = response.json()
            for row in rows:
                row["seriesId"] = 124
            if self.episode_fault:
                rows[0].update(self.episode_fault)
            return httpx.Response(200, json=rows)
        return response

    def worker(self, client):
        worker = super().worker(client)
        resolver = SeriesIdentityResolver()
        resolver.update_requests([{
            "source_id": "127:1", "kind": "season", "media_key": "season:tmdb:74606:1",
            "tvdb_id": 293012, "external_service_id": 124,
            **({"title": "Fixture Show"} if self.with_alias else {}),
        }])
        worker.series_identity = resolver
        return worker


@pytest.mark.asyncio
@pytest.mark.parametrize("unresolved_fields", [
    {}, {"series": None, "seasonNumber": None, "episodes": None},
    {"series": None, "seasonNumber": None, "episodes": []},
])
async def test_proved_localized_pack_uses_one_identity_bound_manual_import_then_existing_readback(
    tmp_path, monkeypatch, unresolved_fields,
):
    f = LocalizedFixture(tmp_path, monkeypatch)
    for key in ("series", "seasonNumber", "episodes"):
        f.candidates[0].pop(key)
    f.candidates[0].update(unresolved_fields)
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        assert await worker.finalize("season:tmdb:74606:1", f.reservations[1]) == "import_requested"
        assert f.manual_reads == [{"folder": LOGICAL, "filterExistingFiles": "false"}]
        assert f.posts == [{
            "name": "ManualImport", "importMode": "Copy", "files": [{
                "path": LOGICAL, "folderName": "pack1", "downloadId": f.parents[1].infohash,
                "seriesId": 124, "episodeIds": [1], "quality": QUALITY,
                "languages": [{"id": 0, "name": "Unknown"}], "releaseGroup": "Fixture",
                "indexerFlags": 0, "releaseType": "unknown",
            }],
        }]
        f.publish("S01E01")
        assert await worker.finalize(
            "season:tmdb:74606:1", f.reservations[1],
        ) == "subtitles_pending"
    assert len(f.posts) == 1


def _candidate(**changes):
    return lambda fixture: setattr(fixture, "candidates", [deepcopy(CANDIDATE) | changes])


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    lambda f: setattr(f, "candidates", None),
    lambda f: setattr(f, "candidates", []),
    lambda f: setattr(f, "candidates", [deepcopy(CANDIDATE), deepcopy(CANDIDATE)]),
    _candidate(path=LOGICAL.replace("S01E01", "S01E02")),
    _candidate(series={"id": 999}),
    _candidate(series=False),
    _candidate(seasonNumber=1),
    _candidate(seasonNumber=False),
    _candidate(seasonNumber={}),
    _candidate(episodes=[{"id": 1}]),
    _candidate(episodes=False),
    _candidate(episodes={}),
    _candidate(rejections=[]),
    _candidate(rejections=["Unknown Series"]),
    _candidate(rejections=[{"reason": "Unknown Series", "type": None}]),
    _candidate(rejections=[{"reason": "Unknown Series", "type": "permanent"},
                           {"reason": "Quality not wanted", "type": "permanent"}]),
    _candidate(quality=None),
    _candidate(quality={"quality": {"id": 3, "source": "web", "resolution": 1080}}),
    _candidate(quality=QUALITY | {"quality": QUALITY["quality"] | {"id": True}}),
    _candidate(quality=QUALITY | {"quality": QUALITY["quality"] | {"source": "bluray"}}),
    _candidate(quality=QUALITY | {"quality": QUALITY["quality"] | {"name": "WEBRip-1080p"}}),
    _candidate(quality=QUALITY | {"quality": QUALITY["quality"] | {"resolution": 720}}),
    _candidate(quality=QUALITY | {"revision": {"version": True, "real": 0, "isRepack": False}}),
    _candidate(languages=None),
    _candidate(languages=[{"id": True, "name": "Unknown"}]),
    _candidate(indexerFlags=True),
    lambda f: setattr(f, "episode_fault", {"id": True}),
    lambda f: setattr(f, "episode_fault", {"id": 0}),
    lambda f: setattr(f, "episode_fault", {"seriesId": 999}),
])
async def test_localized_pack_native_veto_or_malformed_proof_is_rejected_before_claim(
    tmp_path, monkeypatch, fault,
):
    f = LocalizedFixture(tmp_path, monkeypatch, fault=fault)
    child = f.children["S01E01"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        assert await f.worker(client).finalize(
            "season:tmdb:74606:1", f.reservations[1],
        ) == "import_guard"
    assert f.posts == []
    assert f.repo.episode_import_state(child.permit_id) is None


@pytest.mark.asyncio
async def test_opted_in_pack_without_current_authenticated_alias_is_not_blindly_submitted(
    tmp_path, monkeypatch,
):
    f = LocalizedFixture(tmp_path, monkeypatch)
    f.with_alias = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        assert await f.worker(client).finalize(
            "season:tmdb:74606:1", f.reservations[1],
        ) == "import_guard"
    assert f.posts == [] and f.manual_reads == []
    assert f.repo.episode_import_state(f.children["S01E01"].permit_id) is None


@pytest.mark.asyncio
async def test_canonical_pack_keeps_normal_native_scan_without_manual_discovery(
    tmp_path, monkeypatch,
):
    f = PublicationFixture(tmp_path, monkeypatch)
    f.repo.set_series_subtitle_publication(74606, enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        assert await f.worker(client).finalize(
            "season:tmdb:74606:1", f.reservations[1],
        ) == "import_requested"
    assert f.posts == [{"name": "DownloadedEpisodesScan", "path": LOGICAL, "importMode": "Copy"}]


@pytest.mark.parametrize("detail,rank,expected", [
    ({"source": "web", "name": "WEBRip-1080p"}, (1, 1080), False),
    ({"source": "bluray", "name": "Bluray-1080p"}, (3, 1080), False),
    ({"source": "bluray", "name": "Remux-1080p"}, (3, 1080), False),
    ({"source": "bluray", "name": "Bluray-1080p", "modifier": "none"},
     (3, 1080), False),
    ({"source": "bluray", "name": "Bluray-1080p", "modifier": "remux"},
     (3, 1080), True),
    ({"source": "blurayRaw", "name": "Remux-1080p"},
     (3, 1080), True),
    ({"source": "bluray", "name": "Bluray-1080p"}, (2, 1080), True),
    ({"source": "web", "name": "WEBDL-1080p", "modifier": "remux"},
     (1, 1080), False),
    ({"source": {}, "name": "WEBDL-1080p"}, (1, 1080), False),
])
def test_native_quality_facts_preserve_webdl_and_explicit_remux_distinctions(
    detail, rank, expected,
):
    quality = deepcopy(QUALITY)
    quality["quality"].update(detail)
    assert SeriesFinalizer._native_import_quality_matches(quality, rank) is expected


@pytest.mark.asyncio
async def test_default_unproven_alias_with_ready_subtitle_keeps_normal_native_scan(
    tmp_path, monkeypatch,
):
    f = LocalizedFixture(tmp_path, monkeypatch)
    f.with_alias = False
    f.repo.set_series_subtitle_publication(74606, enabled=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        child = f.children["S01E01"]
        worker.subtitle_store.put(child.reservation_id, child.scope_key, child.infohash, SRT)
        assert await worker.finalize("season:tmdb:74606:1", f.reservations[1]) == "import_requested"
    assert f.manual_reads == []
    assert f.posts == [{"name": "DownloadedEpisodesScan", "path": LOGICAL, "importMode": "Copy"}]
