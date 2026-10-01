"""Explicit episode deletion preserves a shared pack until its last binding is deleted."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.deletion_coordinator import DeletionCoordinator


def _encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    return (
        b"d" + b"".join(_encode(key) + _encode(item) for key, item in sorted(value.items())) + b"e"
    )


class PackFixture:
    def __init__(self, tmp_path):
        self.data = tmp_path / "data"
        self.media = self.data / "media"
        self.archive = self.data / "torrents"
        (self.archive / "pack").mkdir(parents=True)
        (self.media / "tv").mkdir(parents=True)
        self.videos = {}
        self.bindings = {}
        entries = []
        for episode in (5, 6):
            names = tuple(
                f"pack/Series.S02E{episode:02d}{suffix}" for suffix in (".mkv", ".pt-BR.srt")
            )
            for name in names:
                source = self.archive / name
                source.write_bytes(b"video" if name.endswith(".mkv") else b"subtitle")
                target = self.media / "tv" / source.name
                os.link(source, target)
                entries.append({b"length": source.stat().st_size, b"path": [source.name.encode()]})
                if name.endswith(".mkv"):
                    self.videos[episode] = target
            self.bindings[f"S02E{episode:02d}"] = names
        self.neighbor = self.media / "tv" / "Another.Series.S01E01.mkv"
        self.neighbor.write_bytes(b"unrelated")
        self.torrent = _encode(
            {
                b"info": {
                    b"name": b"pack",
                    b"files": entries,
                    b"piece length": 16384,
                    b"pieces": b"a" * 20,
                }
            }
        )
        inspected = inspect_torrent(self.torrent)
        self.repo = ReservationRepository(tmp_path / "control.sqlite")
        self.repo.initialize()
        reservation = self.repo.reserve(
            request_id="seerr:123:2",
            source_id="123:2",
            media_key="season:tmdb:123:2",
            filesystem_id="fixture",
            budget_bytes=1000,
            free_bytes=2000,
            total_bytes=3000,
        )
        self.permits = PermitRegistry(self.repo.path)
        self.parent = self.permits.issue(
            infohash=inspected.infohash,
            destination="/data/torrents",
            category="sonarr",
            reservation_id=reservation.reservation_id,
            scope_key="S02PACK",
            metadata_sha256=inspected.metadata_sha256,
            selected_files=tuple(entry.path for entry in inspected.files),
            budget_bytes=inspected.total_bytes,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.permits.bind_season_pack(self.parent.token, episode_files=self.bindings)
        self.permits.authorize(
            token=self.parent.token,
            infohash=self.parent.infohash,
            destination=self.parent.destination,
            metadata_sha256=self.parent.metadata_sha256,
            effect=lambda _: {"accepted": True},
        )
        self.store = TorrentArtifactStore(self.repo.path)
        self.store.put(self.parent, self.torrent)
        self.jobs = DeletionJobStore(self.repo.path)
        self.current = {
            "hash": self.parent.infohash,
            "category": "sonarr",
            "save_path": "/data/torrents",
            "state": "uploading",
        }
        self.files = [
            {"name": entry.path, "size": entry.length, "priority": 1} for entry in inspected.files
        ]
        self.deleted = []
        self.monitored = {5: True, 6: True}
        self.present = {5: True, 6: True}
        self.gateway_states = []
        self.calls = []
        self.snapshot = tmp_path / "capacity.json"
        self.snapshot.write_text(
            json.dumps({"filesystem_id": "fixture", "measured_at": time.time()})
        )
        self.app = create_app(
            permits=self.permits, upstream=self, arr_token="fixture", torrent_store=self.store
        )

    def read(self, path, params=None):
        if path == "/api/v2/torrents/info":
            assert params == {"hashes": self.parent.infohash}
            return [deepcopy(self.current)] if self.current is not None else []
        if path == "/api/v2/torrents/files":
            assert params == {"hash": self.parent.infohash}
            return deepcopy(self.files)
        raise AssertionError(path)

    def delete_torrent(self, infohash, *, delete_files):
        assert infohash == self.parent.infohash and delete_files
        self.deleted.append(infohash)
        for entry in self.files:
            (self.archive / entry["name"]).unlink()
        self.current = None

    def capture(self, number, *, parent_item_id=None):
        video = self.videos[number]
        status = video.stat()
        return self.jobs.enqueue(
            str(number) * 32,
            "Episode",
            {
                "media_key": f"episode:tmdb:123:S02E{number:02d}",
                "file_path": str(video),
                "file_identity": {
                    "device": status.st_dev,
                    "inode": status.st_ino,
                    "size": status.st_size,
                    "mtime_ns": status.st_mtime_ns,
                },
                "sonarr_series_id": 1,
                "sonarr_episode_id": number,
                "sonarr_episode_file_id": 100 + number,
                "series_tmdb_id": 123,
                "season": 2,
                "episode": number,
                **({"parent_item_id": parent_item_id} if parent_item_id else {}),
            },
        )

    def coordinator(self, client):
        return DeletionCoordinator(
            jobs=self.jobs,
            data_root=self.data,
            media_root=self.media,
            snapshot_path=self.snapshot,
            filesystem_id="fixture",
            radarr_url="http://radarr",
            radarr_api_key="fixture",
            sonarr_url="http://sonarr",
            sonarr_api_key="fixture",
            seerr_url="http://seerr",
            seerr_api_key="fixture",
            gateway_url="http://gateway",
            arr_token="fixture",
            jellyfin_url="http://jellyfin",
            jellyfin_api_key="fixture",
            client=client,
            mount_check=lambda path: path == self.data,
        )

    async def native_response(self, request, gateway):
        self.calls.append((request.url.host, request.method, request.url.path))
        if request.url.host == "gateway":
            response = await gateway.request(
                request.method, request.url.path, headers=request.headers, content=request.content
            )
            if response.status_code == 200:
                self.gateway_states.append(response.json()["state"])
            return httpx.Response(response.status_code, json=response.json())
        if request.url.host == "sonarr":
            if request.url.path == "/api/v3/episode":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "id": number,
                            "seriesId": 1,
                            "seasonNumber": 2,
                            "episodeNumber": number,
                            "episodeFileId": 100 + number if self.present[number] else 0,
                            "monitored": self.monitored[number],
                        }
                        for number in (5, 6)
                    ],
                )
            if request.url.path == "/api/v3/episode/monitor":
                payload = json.loads(request.content)
                assert payload["monitored"] is False
                for number in payload["episodeIds"]:
                    self.monitored[number] = False
                return httpx.Response(200, json={})
            number = int(request.url.path.rsplit("/", 1)[1]) - 100
            if request.method == "GET":
                return httpx.Response(
                    200, json={"id": 100 + number, "path": str(self.videos[number]), "size": 5}
                )
            assert request.method == "DELETE"
            self.videos[number].unlink()
            self.present[number] = False
            return httpx.Response(204)
        if request.url.host == "jellyfin":
            assert request.method == "GET" and request.url.path == "/Items"
            return httpx.Response(200, json={"Items": []})
        raise AssertionError("A single episode must not delete the Seerr season")


def test_gateway_retains_exact_shared_pack_for_one_deleted_episode(tmp_path):
    fixture = PackFixture(tmp_path)
    response = TestClient(fixture.app).post(
        "/internal/delete-source",
        headers={"X-Arr-Token": "fixture"},
        json={
            "permit_token": fixture.parent.token,
            "media_key": "season:tmdb:123:2",
            "scope_key": "S02E05",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"state": "retained_shared"}
    assert fixture.deleted == []
    assert fixture.current["state"] == "uploading"


@pytest.mark.asyncio
async def test_pack_episode_deletion_cascades_then_last_episode_frees_archive(tmp_path):
    fixture = PackFixture(tmp_path)
    fixture.capture(5)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fixture.app), base_url="http://gateway"
    ) as gateway:

        async def handler(request):
            return await fixture.native_response(request, gateway)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await fixture.coordinator(client).run_once() == "complete"
            assert fixture.jobs.is_tombstoned("episode:tmdb:123:S02E05")
            assert fixture.jobs.get("5" * 32)["stage"] == "complete"
            assert not fixture.videos[5].exists()
            assert not fixture.videos[5].with_suffix(".pt-BR.srt").exists()
            assert fixture.monitored == {5: False, 6: True}
            assert fixture.videos[6].is_file()
            assert (fixture.archive / fixture.bindings["S02E05"][0]).is_file()
            assert fixture.current["state"] == "uploading"
            assert fixture.deleted == []
            assert fixture.gateway_states == ["retained_shared"]

            fixture.capture(6)
            assert await fixture.coordinator(client).run_once() == "complete"
    assert fixture.jobs.is_tombstoned("episode:tmdb:123:S02E06")
    assert fixture.jobs.get("6" * 32)["stage"] == "complete"
    assert fixture.monitored == {5: False, 6: False}
    assert fixture.gateway_states == ["retained_shared", "deleted"]
    assert fixture.deleted == [fixture.parent.infohash]
    assert not fixture.videos[6].exists()
    assert not list((fixture.archive / "pack").iterdir())
    assert fixture.neighbor.read_bytes() == b"unrelated"
    assert await fixture.coordinator(client).run_once() == "empty"


@pytest.mark.asyncio
async def test_whole_season_reuses_episode_guards_and_removes_shared_pack_only_once(tmp_path):
    fixture = PackFixture(tmp_path)
    folder = fixture.media / "tv" / "Series" / "Season 02"
    folder.mkdir(parents=True)
    for number, old_video in tuple(fixture.videos.items()):
        video = folder / old_video.name
        old_video.rename(video)
        old_video.with_suffix(".pt-BR.srt").rename(video.with_suffix(".pt-BR.srt"))
        fixture.videos[number] = video
    parent_id = "a" * 32
    children = [fixture.capture(number, parent_item_id=parent_id) for number in (5, 6)]
    status = folder.stat()
    fixture.jobs.enqueue(parent_id, "Season", {
        "media_key": "season:tmdb:123:2", "series_tmdb_id": 123, "series_tvdb_id": 456,
        "sonarr_series_id": 1, "season": 2, "file_path": str(folder),
        "directory_identity": {"device": status.st_dev, "inode": status.st_ino},
        "sonarr_episode_ids": [5, 6], "metadata_files": [],
        "episodes": [{"item_id": child["item_id"], "payload": child["payload"]}
                     for child in children],
    })
    series = {"id": 1, "tvdbId": 456, "seasons": [
        {"seasonNumber": 2, "monitored": True}, {"seasonNumber": 3, "monitored": True},
    ]}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fixture.app), base_url="http://gateway",
    ) as gateway:
        async def handler(request):
            if request.url.host == "sonarr" and request.url.path == "/api/v3/series/1":
                if request.method == "PUT":
                    series.update(json.loads(request.content))
                return httpx.Response(200, json=series)
            if request.url.host == "seerr":
                assert request.method != "DELETE"  # Request includes another season.
                return httpx.Response(200, json={
                    "id": 123, "media": {"tmdbId": 123, "mediaType": "tv"},
                    "seasons": [{"seasonNumber": 2}, {"seasonNumber": 3}],
                })
            return await fixture.native_response(request, gateway)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = fixture.coordinator(client)
            for _ in range(20):
                await worker.run_once()
                if fixture.jobs.get(parent_id)["stage"] == "complete":
                    break
    assert fixture.jobs.get(parent_id)["stage"] == "complete"
    assert fixture.gateway_states == ["retained_shared", "deleted", "missing"]
    assert fixture.deleted == [fixture.parent.infohash]
    assert not folder.exists()
    assert not list((fixture.archive / "pack").iterdir())
    assert fixture.neighbor.read_bytes() == b"unrelated"
    assert series["seasons"][1]["monitored"] is True


@pytest.mark.parametrize("failure", ["other_episode", "category", "path", "files", "artifact"])
def test_pack_retention_refuses_unverified_identity_or_payload(tmp_path, failure):
    fixture = PackFixture(tmp_path)
    body = {
        "permit_token": fixture.parent.token,
        "media_key": "season:tmdb:123:2",
        "scope_key": "S02E05",
    }
    if failure == "other_episode":
        body["scope_key"] = "S02E07"
    elif failure == "category":
        fixture.current["category"] = "radarr"
    elif failure == "path":
        fixture.current["save_path"] = "/data/other"
    elif failure == "files":
        fixture.files[0]["name"] = "other/episode.mkv"
    else:
        with sqlite3.connect(fixture.repo.path) as connection:
            connection.execute("DELETE FROM torrent_artifacts")
    response = TestClient(fixture.app).post(
        "/internal/delete-source", headers={"X-Arr-Token": "fixture"}, json=body
    )
    assert response.status_code in (403, 409)
    assert fixture.deleted == []
    assert fixture.videos[5].is_file() and fixture.videos[6].is_file()
