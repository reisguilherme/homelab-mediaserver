from __future__ import annotations

import json
import os
import time
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from homeserver_control.api.app import ControlState, create_app
from homeserver_control.api.deletion_capture import DeletionAdmission, DeletionCaptureError
from homeserver_control.persistence.deletion_jobs import DeletionJobStore


def test_default_docker_api_enables_delete_capture_without_uuid(tmp_path, monkeypatch):
    import homeserver_control.api.app as api

    constructed = []
    media = tmp_path / "data"
    media.mkdir()
    monkeypatch.delenv("HOMESERVER_MEDIA_UUID", raising=False)
    monkeypatch.setenv("HOMESERVER_DB_PATH", str(tmp_path / "control.sqlite"))
    monkeypatch.setenv("HOMESERVER_MEDIA_ROOT", str(media))
    monkeypatch.setattr(api, "DeletionAdmission", lambda **kwargs: constructed.append(kwargs))
    api.create_app()
    assert len(constructed) == 1
    assert constructed[0]["filesystem_id"] == f"device:{media.stat().st_dev}"


def _snapshot(path: Path) -> Path:
    snapshot = path / "capacity.json"
    snapshot.write_text(
        json.dumps(
            {"filesystem_id": "test-media-uuid", "total_bytes": 100_000,
             "free_bytes": 50_000, "measured_at": time.time()}
        ),
        encoding="utf-8",
    )
    return snapshot


@pytest.mark.asyncio
async def test_movie_delete_captures_exact_file_before_mutation(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    movie_file = media_root / "movies" / "Example (2026)" / "Example.mkv"
    movie_file.parent.mkdir(parents=True)
    movie_file.write_bytes(b"video fixture")
    torrent_link = tmp_path / "source.mkv"
    os.link(movie_file, torrent_link)
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    seen: list[tuple[str, str]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/Users/Me":
            return httpx.Response(200, json={"Id": "user-1", "Policy": {
                "IsAdministrator": True, "EnableContentDeletion": True,
            }})
        if request.url.path == "/Users/user-1/Items/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa":
            return httpx.Response(200, json={
                "Id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Type": "Movie",
                "Path": str(movie_file), "ProviderIds": {"Tmdb": "123"},
            })
        if request.url.path == "/api/v3/movie":
            assert dict(request.url.params) == {"tmdbId": "123"}
            return httpx.Response(200, json=[{
                "id": 7, "tmdbId": 123, "path": str(movie_file.parent),
                "hasFile": True, "movieFileId": 8,
                "movieFile": {"id": 8, "path": str(movie_file), "size": len(b"video fixture")},
            }])
        raise AssertionError(f"unexpected upstream call {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        admission = DeletionAdmission(
            jobs=jobs, media_root=media_root, snapshot_path=_snapshot(tmp_path),
            filesystem_id="test-media-uuid", jellyfin_url="http://jellyfin:8096",
            radarr_url="http://radarr:7878", radarr_api_key="radarr-test",
            sonarr_url="http://sonarr:8989", sonarr_api_key="sonarr-test",
            client=client,
        )
        job = await admission.capture("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "user-token")
        repeated = await admission.capture("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "user-token")

    assert job["stage"] == "queued"
    assert repeated == job
    assert job["payload"]["media_key"] == "movie:tmdb:123"
    assert job["payload"]["file_path"] == str(movie_file)
    assert job["payload"]["radarr_id"] == 7
    assert job["payload"]["file_identity"]["inode"] == movie_file.stat().st_ino
    assert job["payload"]["file_identity"]["nlink"] == 2
    assert jobs.get("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa") == job
    assert movie_file.exists()
    assert all(method == "GET" for method, _ in seen)
    assert sum(path == "/api/v3/movie" for _, path in seen) == 1


@pytest.mark.asyncio
async def test_non_admin_cannot_enqueue_jellyfin_delete(tmp_path: Path) -> None:
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Users/Me"
        return httpx.Response(200, json={"Id": "viewer", "Policy": {
            "IsAdministrator": False, "EnableContentDeletion": True,
        }})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        admission = DeletionAdmission(
            jobs=jobs, media_root=tmp_path, snapshot_path=_snapshot(tmp_path),
            filesystem_id="test-media-uuid", jellyfin_url="http://jellyfin:8096",
            radarr_url="http://radarr:7878", radarr_api_key="radarr-test",
            sonarr_url="http://sonarr:8989", sonarr_api_key="sonarr-test",
            client=client,
        )
        with pytest.raises(DeletionCaptureError) as error:
            await admission.capture("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "viewer-token")

    assert error.value.status_code == 403
    assert jobs.next_queued() is None


@pytest.mark.asyncio
async def test_episode_capture_requires_unique_sonarr_file(tmp_path: Path) -> None:
    media_root = tmp_path / "media"
    video = media_root / "tv" / "Example" / "Season 01" / "E02.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"episode")
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    item_id = "b" * 32
    series_id = "c" * 32

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/Users/Me":
            return httpx.Response(200, json={"Id": "admin", "Policy": {
                "IsAdministrator": True, "EnableContentDeletion": True,
            }})
        if request.url.path.endswith(f"/Items/{item_id}"):
            return httpx.Response(200, json={
                "Id": item_id, "Type": "Episode", "Path": str(video),
                "SeriesId": series_id, "ParentIndexNumber": 1, "IndexNumber": 2,
            })
        if request.url.path.endswith(f"/Items/{series_id}"):
            return httpx.Response(200, json={
                "ProviderIds": {"Tvdb": "456", "Tmdb": "123"},
            })
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 10, "tvdbId": 456}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 11, "seriesId": 10, "seasonNumber": 1,
                "episodeNumber": 2, "episodeFileId": 12,
            }])
        if request.url.path == "/api/v3/episodefile/12":
            return httpx.Response(200, json={
                "id": 12, "path": str(video), "size": 7,
            })
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        admission = DeletionAdmission(
            jobs=jobs, media_root=media_root, snapshot_path=_snapshot(tmp_path),
            filesystem_id="test-media-uuid", jellyfin_url="http://jellyfin:8096",
            radarr_url="http://radarr:7878", radarr_api_key="radarr-test",
            sonarr_url="http://sonarr:8989", sonarr_api_key="sonarr-test",
            client=client,
        )
        job = await admission.capture(item_id, "admin-token")
    assert job["payload"]["media_key"] == "episode:tmdb:123:S01E02"
    assert job["payload"]["sonarr_episode_file_id"] == 12
    assert video.exists()


def test_jellyfin_delete_route_extracts_ui_token_and_queues_only() -> None:
    class Capturer:
        calls: list[tuple[str, str]] = []

        async def capture(self, item_id: str, token: str) -> dict[str, object]:
            self.calls.append((item_id, token))
            return {"stage": "queued"}

    capturer = Capturer()
    state = ControlState(deletion_admission=capturer)  # type: ignore[arg-type]
    client = TestClient(create_app(state=state))
    item_id = "d" * 32
    response = client.delete(
        f"/Items/{item_id}",
        headers={"Authorization": 'MediaBrowser Client="Web", Token="user-token"'},
    )
    assert response.status_code == 204
    assert capturer.calls == [(item_id, "user-token")]
    prefixed = client.delete(
        f"/jellyfin/Items/{item_id}", headers={"X-Emby-Token": "other-token"}
    )
    assert prefixed.status_code == 204
    assert capturer.calls[-1] == (item_id, "other-token")


def test_jellyfin_delete_route_fails_closed_without_admission() -> None:
    client = TestClient(create_app(state=ControlState()))
    assert client.delete(f"/Items/{'d' * 32}").status_code == 503


def test_deletion_job_status_requires_admin_and_omits_capture_paths(tmp_path: Path) -> None:
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    jobs.enqueue("a" * 32, "Movie", {"file_path": "/data/media/movies/private.mkv"})

    class Admission:
        pass

    admission = Admission()
    admission.jobs = jobs
    state = ControlState(
        admin_token="admin-token", deletion_admission=admission,  # type: ignore[arg-type]
    )
    client = TestClient(create_app(state=state))
    assert client.get("/api/v1/deletions/jobs").status_code == 401
    response = client.get(
        "/api/v1/deletions/jobs", headers={"X-Admin-Token": "admin-token"}
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["stage"] == "queued"
    assert "file_path" not in str(response.json())


class SeasonCaptureFixture:
    def __init__(self, tmp_path: Path) -> None:
        self.media_root = tmp_path / "media"
        self.directory = self.media_root / "tv" / "Example" / "Season 02"
        self.series_directory = self.directory.parent
        self.directory.mkdir(parents=True)
        self.item_id = "a" * 32
        self.series_id = "b" * 32
        self.children = []
        self.sonarr_episodes = []
        self.files = {}
        for number, item_id in ((1, "c" * 32), (2, "d" * 32)):
            video = self.directory / f"E{number:02d}.mkv"
            video.write_bytes(b"episode")
            self.children.append({
                "Id": item_id, "Type": "Episode", "Path": str(video),
                "SeriesId": self.series_id, "SeasonId": self.item_id,
                "ParentIndexNumber": 2, "IndexNumber": number,
            })
            self.sonarr_episodes.append({
                "id": 10 + number, "seriesId": 10, "seasonNumber": 2,
                "episodeNumber": number, "episodeFileId": 20 + number,
            })
            self.files[20 + number] = {"id": 20 + number, "path": str(video), "size": 7}
        self.sonarr_episodes.extend([
            {"id": 13, "seriesId": 10, "seasonNumber": 2,
             "episodeNumber": 3, "episodeFileId": 0},
            {"id": 14, "seriesId": 10, "seasonNumber": 3,
             "episodeNumber": 1, "episodeFileId": 24},
        ])
        self.total = 2
        self.jobs = DeletionJobStore(tmp_path / "control.sqlite")
        self.jobs.initialize()
        self.snapshot = _snapshot(tmp_path)
        self.calls: list[tuple[str, str]] = []

    def response(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/Users/Me":
            return httpx.Response(200, json={"Id": "admin", "Policy": {
                "IsAdministrator": True, "EnableContentDeletion": True,
            }})
        if request.url.path.endswith(f"/Items/{self.item_id}"):
            return httpx.Response(200, json={
                "Id": self.item_id, "Type": "Season", "Path": str(self.directory),
                "SeriesId": self.series_id, "IndexNumber": 2,
            })
        if request.url.path.endswith(f"/Items/{self.series_id}"):
            return httpx.Response(200, json={
                "Id": self.series_id, "Type": "Series",
                "Path": str(self.series_directory),
                "ProviderIds": {"Tvdb": "456", "Tmdb": "123"},
            })
        if request.url.path == f"/Shows/{self.series_id}/Episodes":
            assert dict(request.url.params) == {
                "userId": "admin", "seasonId": self.item_id,
                "fields": "Path", "isMissing": "false",
            }
            return httpx.Response(200, json={
                "Items": deepcopy(self.children), "TotalRecordCount": self.total,
                "StartIndex": 0,
            })
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 10, "tvdbId": 456, "path": str(self.series_directory),
            }])
        if request.url.path == "/api/v3/episode":
            assert dict(request.url.params) == {"seriesId": "10"}
            return httpx.Response(200, json=deepcopy(self.sonarr_episodes))
        if request.url.path.startswith("/api/v3/episodefile/"):
            file_id = int(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json=deepcopy(self.files[file_id]))
        raise AssertionError(request.url)

    def admission(self, client: httpx.AsyncClient) -> DeletionAdmission:
        return DeletionAdmission(
            jobs=self.jobs, media_root=self.media_root, snapshot_path=self.snapshot,
            filesystem_id="test-media-uuid", jellyfin_url="http://jellyfin:8096",
            radarr_url="http://radarr:7878", radarr_api_key="radarr-test",
            sonarr_url="http://sonarr:8989", sonarr_api_key="sonarr-test", client=client,
        )


@pytest.mark.asyncio
async def test_season_capture_atomically_queues_only_selected_season(tmp_path: Path) -> None:
    fixture = SeasonCaptureFixture(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.response)) as client:
        job = await fixture.admission(client).capture(fixture.item_id, "admin-token")
        before_repeat = len(fixture.calls)
        repeated = await fixture.admission(client).capture(fixture.item_id, "admin-token")
    assert job == repeated
    assert len(fixture.calls) == before_repeat + 1  # Session rechecked; no second snapshot.
    assert job["item_type"] == "Season"
    assert job["payload"]["media_key"] == "season:tmdb:123:2"
    assert job["payload"]["series_tmdb_id"] == 123
    assert job["payload"]["series_tvdb_id"] == 456
    assert job["payload"]["sonarr_series_id"] == 10
    assert job["payload"]["sonarr_episode_ids"] == [11, 12, 13]
    assert job["payload"]["file_path"] == str(fixture.directory)
    assert job["payload"]["directory_identity"] == {
        "device": fixture.directory.stat().st_dev, "inode": fixture.directory.stat().st_ino,
    }
    assert [entry["item_id"] for entry in fixture.jobs.list()] == [
        "a" * 32, "c" * 32, "d" * 32,
    ]
    assert fixture.jobs.get("c" * 32)["payload"]["media_key"] == "episode:tmdb:123:S02E01"
    assert fixture.jobs.get("c" * 32)["payload"]["parent_item_id"] == fixture.item_id
    assert all(method == "GET" for method, _ in fixture.calls)
    assert len(list(fixture.directory.iterdir())) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "pagination", "duplicate_child", "other_season", "other_series",
    "other_season_id", "unlisted_file", "shared_file", "outside_folder", "changed_file",
    "duplicate_path", "malformed_sonarr", "duplicate_sonarr", "multi_episode_file",
    "malformed_children", "season_symlink", "metadata_symlink", "series_directory",
])
async def test_season_capture_refuses_partial_or_changed_scope_without_jobs(
    tmp_path: Path, failure: str,
) -> None:
    fixture = SeasonCaptureFixture(tmp_path)
    if failure == "pagination":
        fixture.total = 3
    elif failure == "duplicate_child":
        fixture.children[1]["Id"] = fixture.children[0]["Id"]
    elif failure == "other_season":
        fixture.children[1]["ParentIndexNumber"] = 3
    elif failure == "other_series":
        fixture.children[1]["SeriesId"] = "e" * 32
    elif failure == "other_season_id":
        fixture.children[1]["SeasonId"] = "e" * 32
    elif failure == "unlisted_file":
        fixture.sonarr_episodes[2]["episodeFileId"] = 23
    elif failure == "shared_file":
        fixture.sonarr_episodes[3]["episodeFileId"] = 21
    elif failure == "outside_folder":
        outside = fixture.directory.parent / "other.mkv"
        outside.write_bytes(b"episode")
        fixture.children[1]["Path"] = str(outside)
        fixture.files[22]["path"] = str(outside)
    elif failure == "changed_file":
        fixture.files[22]["size"] = 100
    elif failure == "duplicate_path":
        fixture.children[1]["Path"] = fixture.children[0]["Path"]
        fixture.files[22]["path"] = fixture.children[0]["Path"]
    elif failure == "malformed_sonarr":
        fixture.sonarr_episodes[2] = None
    elif failure == "duplicate_sonarr":
        fixture.sonarr_episodes[2]["id"] = 11
    elif failure == "multi_episode_file":
        fixture.sonarr_episodes[1]["episodeFileId"] = 21
    elif failure == "malformed_children":
        fixture.children[1] = None
    elif failure == "season_symlink":
        linked = fixture.directory.parent / "Season link"
        linked.symlink_to(fixture.directory, target_is_directory=True)
        fixture.directory = linked
    elif failure == "metadata_symlink":
        (fixture.directory / "folder.jpg").symlink_to(fixture.directory / "E01.mkv")
    else:
        # A Season cannot authorize deleting the containing Series directory.
        fixture.directory = fixture.directory.parent
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.response)) as client:
        with pytest.raises(DeletionCaptureError) as error:
            await fixture.admission(client).capture(fixture.item_id, "admin-token")
    assert error.value.status_code == 409
    assert fixture.jobs.list() == []
    assert all(Path(entry["path"]).exists() for entry in fixture.files.values())


@pytest.mark.asyncio
async def test_season_capture_rejects_colliding_child_without_partial_queue(tmp_path: Path) -> None:
    fixture = SeasonCaptureFixture(tmp_path)
    fixture.jobs.enqueue("d" * 32, "Episode", {"media_key": "unrelated"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.response)) as client:
        with pytest.raises(DeletionCaptureError) as error:
            await fixture.admission(client).capture(fixture.item_id, "admin-token")
    assert error.value.status_code == 409
    assert [entry["item_id"] for entry in fixture.jobs.list()] == ["d" * 32]


@pytest.mark.asyncio
async def test_season_capture_records_only_known_direct_metadata_files(tmp_path: Path) -> None:
    fixture = SeasonCaptureFixture(tmp_path)
    poster = fixture.directory / "folder.jpg"
    poster.write_bytes(b"image")
    unknown = fixture.directory / "personal.txt"
    unknown.write_bytes(b"keep")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.response)) as client:
        job = await fixture.admission(client).capture(fixture.item_id, "admin-token")
    metadata = job["payload"]["metadata_files"]
    assert len(metadata) == 1
    assert metadata[0]["file_path"] == str(poster)
    assert metadata[0]["file_identity"]["size"] == 5
    assert metadata[0]["file_identity"]["inode"] == poster.stat().st_ino
    assert poster.exists() and unknown.exists()
