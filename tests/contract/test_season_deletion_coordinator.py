"""A season deletion validates the batch before removing its exact episode files."""

from __future__ import annotations

import json
import sqlite3
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.worker.deletion_coordinator import DeletionBlocked, DeletionCoordinator


def identity(path):
    s = path.stat()
    return dict(device=s.st_dev, inode=s.st_ino, size=s.st_size, mtime_ns=s.st_mtime_ns)


class SeasonFixture:
    def __init__(self, tmp_path):
        self.data = tmp_path / "data"
        self.media = self.data / "media"
        self.folder = self.media / "tv" / "Example" / "Season 01"
        self.folder.mkdir(parents=True)
        self.neighbor = self.folder.parent / "Season 02" / "E01.mkv"
        self.neighbor.parent.mkdir()
        self.neighbor.write_bytes(b"other season")
        self.videos = {}
        self.present = {1: True, 2: True}
        self.episodes = []
        children = []
        self.item_id = "a" * 32
        for number in (1, 2):
            video = self.folder / f"E{number:02d}.mkv"
            video.write_bytes(b"video")
            self.videos[number] = video
            self.episodes.append(dict(id=number, seriesId=10, seasonNumber=1,
                                      episodeNumber=number, episodeFileId=100 + number))
            children.append({"item_id": str(number) * 32, "payload": {
                "parent_item_id": self.item_id,
                "media_key": f"episode:tmdb:123:S01E{number:02d}",
                "file_path": str(video), "file_identity": identity(video),
                "sonarr_series_id": 10, "sonarr_episode_id": number,
                "sonarr_episode_file_id": 100 + number, "series_tmdb_id": 123,
                "season": 1, "episode": number,
            }})
        self.episodes += [dict(id=3, seriesId=10, seasonNumber=1, episodeNumber=3,
                               episodeFileId=0),
                          dict(id=4, seriesId=10, seasonNumber=2, episodeNumber=1,
                               episodeFileId=104)]
        self.series = dict(id=10, tvdbId=456, path=str(self.folder.parent),
                           seasons=[dict(seasonNumber=1, monitored=True),
                                    dict(seasonNumber=2, monitored=True)])
        self.repo = ReservationRepository(tmp_path / "control.sqlite")
        self.repo.initialize()
        self.reservation = self.repo.reserve(
            request_id="seerr:77:1", source_id="77:1", media_key="season:tmdb:123:1",
            filesystem_id="fixture", budget_bytes=30, free_bytes=1000, total_bytes=2000,
        )
        self.permits = PermitRegistry(self.repo.path)
        for number in (1, 2, 3):
            permit = self.permits.issue(
                infohash=str(number) * 40, destination="/data/torrents", category="sonarr",
                reservation_id=self.reservation.reservation_id,
                scope_key=f"S01E{number:02d}", metadata_sha256="f" * 64,
                selected_files=(f"E{number:02d}.mkv",), budget_bytes=5,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            self.permits.authorize(
                token=permit.token, infohash=permit.infohash, destination=permit.destination,
                metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
            )
        self.jobs = DeletionJobStore(self.repo.path)
        self.payload = {
            "media_key": "season:tmdb:123:1", "series_tmdb_id": 123,
            "series_tvdb_id": 456, "sonarr_series_id": 10, "season": 1,
            "file_path": str(self.folder), "directory_identity": identity(self.folder),
            "episodes": children, "sonarr_episode_ids": [1, 2, 3], "metadata_files": [],
        }
        self.jobs.enqueue(self.item_id, "Season", self.payload)
        for child in children:
            self.jobs.enqueue(child["item_id"], "Episode", child["payload"])
        self.snapshot = tmp_path / "capacity.json"
        self.snapshot.write_text(json.dumps(dict(filesystem_id="fixture", measured_at=time.time())))
        self.mutations = []
        self.sources_removed = set()
        self.request_seasons = [1, 2]
        self.request_deleted = False
        self.syncs = 0

    def responder(self, r):
        if r.method != "GET":
            self.mutations.append((r.method, r.url.path))
        if r.url.host == "sonarr":
            if r.url.path == "/api/v3/series/10":
                if r.method == "PUT":
                    self.series = json.loads(r.content)
                return httpx.Response(200, json=deepcopy(self.series))
            if r.url.path == "/api/v3/episode":
                rows = deepcopy(self.episodes)
                for row in rows:
                    if row["id"] in self.present and not self.present[row["id"]]:
                        row["episodeFileId"] = 0
                return httpx.Response(200, json=rows)
            if r.url.path == "/api/v3/episode/monitor":
                data = json.loads(r.content)
                assert set(data["episodeIds"]).issubset({1, 2, 3})
                assert data["monitored"] is False
                return httpx.Response(200, json=[])
            if r.url.path.startswith("/api/v3/episodefile/"):
                number = int(r.url.path.rsplit("/", 1)[1]) - 100
                assert number in (1, 2)
                if r.method == "DELETE":
                    self.videos[number].unlink()
                    self.present[number] = False
                    return httpx.Response(200, json={})
                if not self.present[number]:
                    return httpx.Response(404)
                return httpx.Response(200, json=dict(id=100 + number,
                                                    path=str(self.videos[number]), size=5))
        if r.url.host == "gateway":
            data = json.loads(r.content)
            assert data["media_key"] == "season:tmdb:123:1"
            assert data["scope_key"] in ("S01E01", "S01E02", "S01E03")
            with sqlite3.connect(self.repo.path) as connection:
                assert connection.execute("SELECT state FROM reservations").fetchone()[0] != (
                    "cancelled"
                )  # Running pending sources must keep their capacity reservation.
            if data["scope_key"] == "S01E03":
                assert not any(v.exists() for v in self.videos.values())
            self.sources_removed.add(data["scope_key"])
            return httpx.Response(200, json=dict(state="deleted"))
        if r.url.host == "seerr":
            if r.method == "GET":
                return httpx.Response(200, json=dict(id=77, media=dict(tmdbId=123, mediaType="tv"),
                                                     seasons=[dict(seasonNumber=s)
                                                              for s in self.request_seasons]))
            if r.method == "DELETE":
                self.request_deleted = True
                return httpx.Response(204)
            assert r.url.path == "/api/v1/settings/jellyfin/sync"
            self.syncs += 1
            return httpx.Response(200, json={})
        if r.url.host == "jellyfin":
            assert r.method == "GET" and r.url.path == "/Items"
            return httpx.Response(200, json=dict(Items=[]))
        raise AssertionError(r)

    def coordinator(self, client):
        return DeletionCoordinator(
            jobs=self.jobs, data_root=self.data, media_root=self.media,
            snapshot_path=self.snapshot, filesystem_id="fixture",
            radarr_url="http://radarr", radarr_api_key="fixture",
            sonarr_url="http://sonarr", sonarr_api_key="fixture",
            seerr_url="http://seerr", seerr_api_key="fixture",
            gateway_url="http://gateway", arr_token="fixture",
            jellyfin_url="http://jellyfin", jellyfin_api_key="fixture", client=client,
            mount_check=lambda _: True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("only_selected_season", [False, True])
async def test_season_cascade_preserves_other_seasons_and_cleans_pending_sources(
    tmp_path: Path, only_selected_season: bool,
):
    f = SeasonFixture(tmp_path)
    if only_selected_season:
        f.request_seasons = [1]
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.responder)) as client:
        worker = f.coordinator(client)
        for _ in range(15):
            await worker.run_once()
            if f.jobs.get(f.item_id)["stage"] == "complete":
                break
    assert f.jobs.get(f.item_id)["stage"] == "complete"
    assert f.jobs.is_tombstoned("season:tmdb:123:1")
    assert f.jobs.is_tombstoned("episode:tmdb:123:S01E03")
    assert not f.folder.exists()
    assert f.neighbor.read_bytes() == b"other season"
    assert f.series["seasons"] == [dict(seasonNumber=1, monitored=False),
                                   dict(seasonNumber=2, monitored=True)]
    assert f.sources_removed == {"S01E01", "S01E02", "S01E03"}
    assert f.request_deleted is only_selected_season
    assert f.syncs == 1
    with sqlite3.connect(f.repo.path) as c:
        assert c.execute("SELECT state FROM reservations").fetchone()[0] == "cancelled"


@pytest.mark.asyncio
async def test_changed_child_blocks_entire_season_before_any_effect(tmp_path):
    f = SeasonFixture(tmp_path)
    f.videos[2].write_bytes(b"replacement")
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.responder)) as client:
        worker = f.coordinator(client)
        assert await worker.run_once() == "blocked"
        for _ in range(3):
            await worker.run_once()
    assert f.jobs.get(f.item_id)["stage"] == "blocked"
    assert not f.jobs.is_tombstoned("season:tmdb:123:1")
    assert f.mutations == []
    assert all(v.exists() for v in f.videos.values())


@pytest.mark.asyncio
async def test_new_sonarr_file_after_capture_blocks_before_mutation(tmp_path):
    f = SeasonFixture(tmp_path)
    f.episodes[2]["episodeFileId"] = 103
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.responder)) as client:
        assert await f.coordinator(client).run_once() == "blocked"
    assert f.mutations == []
    assert not f.jobs.is_tombstoned("season:tmdb:123:1")


@pytest.mark.asyncio
async def test_season_retry_does_not_restart_or_duplicate_completed_child_deletes(tmp_path):
    f = SeasonFixture(tmp_path)
    attempts = 0

    def responder(r):
        nonlocal attempts
        if r.url.path == "/api/v3/episodefile/102" and r.method == "DELETE":
            attempts += 1
            if attempts == 1:
                return httpx.Response(503)
        return f.responder(r)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        worker = f.coordinator(client)
        for _ in range(20):
            await worker.run_once()
            if f.jobs.get(f.item_id)["stage"] == "complete":
                break
    assert f.jobs.get(f.item_id)["stage"] == "complete"
    assert f.mutations.count(("DELETE", "/api/v3/episodefile/101")) == 1
    assert attempts == 2
    assert f.neighbor.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["path", "metadata", "pending_scope", "pending_unknown"])
async def test_invalid_season_snapshot_blocks_every_child_before_mutation(tmp_path, failure):
    f = SeasonFixture(tmp_path)
    payload = deepcopy(f.payload)
    if failure == "path":
        payload["file_path"] = None
    elif failure == "metadata":
        payload["metadata_files"] = [{"file_path": str(f.folder / "poster.jpg")}]
    else:
        with sqlite3.connect(f.repo.path) as connection:
            if failure == "pending_scope":
                connection.execute("UPDATE gateway_permits SET scope_key='S02E03' "
                                   "WHERE infohash=?", ("3" * 40,))
            else:
                connection.execute("UPDATE gateway_permits SET state='unknown' "
                                   "WHERE infohash=?", ("3" * 40,))
    if failure in ("path", "metadata"):
        with sqlite3.connect(f.repo.path) as connection:
            connection.execute("UPDATE deletion_jobs SET payload_json=? WHERE item_id=?",
                               (json.dumps(payload), f.item_id))
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.responder)) as client:
        worker = f.coordinator(client)
        assert await worker.run_once() == "blocked"
        for _ in range(3):
            await worker.run_once()
    assert f.mutations == []
    assert all(v.exists() for v in f.videos.values())


@pytest.mark.asyncio
async def test_changed_season_metadata_blocks_before_episode_deletion(tmp_path):
    f = SeasonFixture(tmp_path)
    poster = f.folder / "poster.jpg"
    poster.write_bytes(b"poster")
    payload = deepcopy(f.payload)
    payload["metadata_files"] = [{"file_path": str(poster), "file_identity": identity(poster)}]
    with sqlite3.connect(f.repo.path) as c:
        c.execute("UPDATE deletion_jobs SET payload_json=? WHERE item_id=?",
                  (json.dumps(payload), f.item_id))
    poster.write_bytes(b"changed poster")
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.responder)) as client:
        assert await f.coordinator(client).run_once() == "blocked"
    assert f.mutations == []
    assert all(v.exists() for v in f.videos.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["superseded", "probe_rejected"])
async def test_historical_pack_blocks_before_any_season_mutation(tmp_path, state):
    f = SeasonFixture(tmp_path)
    permit = f.permits.get_for_reservation(f.reservation.reservation_id, scope_key="S01E03")
    with sqlite3.connect(f.repo.path) as connection:
        connection.execute("UPDATE gateway_permits SET scope_key='S01PACK',selected_files_json=? "
                           "WHERE token=?", (json.dumps(["S01E03.mkv"]), permit.token))
    f.permits.bind_season_pack(permit.token, episode_files={"S01E03": ("S01E03.mkv",)})
    with sqlite3.connect(f.repo.path) as connection:
        connection.execute("UPDATE gateway_permits SET state=? WHERE token=?",
                           (state, permit.token))
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.responder)) as client:
        assert await f.coordinator(client).run_once() == "blocked"
    assert f.mutations == []
    assert all(v.exists() for v in f.videos.values())


def test_metadata_replaced_during_cleanup_is_preserved(tmp_path, monkeypatch):
    f = SeasonFixture(tmp_path)
    poster = f.folder / "poster.jpg"
    poster.write_bytes(b"poster")
    payload = deepcopy(f.payload)
    payload["metadata_files"] = [{"file_path": str(poster), "file_identity": identity(poster)}]
    job = {"item_id": f.item_id, "item_type": "Season", "payload": payload}
    worker = f.coordinator(None)
    monkeypatch.setattr(worker, "_guard_mount", lambda: poster.write_bytes(b"new poster"))
    with pytest.raises(DeletionBlocked, match="metadata changed"):
        worker._remove_season_folder(job)
    assert poster.read_bytes() == b"new poster"
