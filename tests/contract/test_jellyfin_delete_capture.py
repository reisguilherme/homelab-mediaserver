from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from homeserver_control.api.app import ControlState, create_app
from homeserver_control.api.deletion_capture import DeletionAdmission, DeletionCaptureError
from homeserver_control.persistence.deletion_jobs import DeletionJobStore


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
