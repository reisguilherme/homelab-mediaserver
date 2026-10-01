"""An explicit Jellyfin delete removes only the captured Arr media and source."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
import pytest

from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.worker.deletion_coordinator import DeletionCoordinator


def _identity(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {
        "device": stat.st_dev, "inode": stat.st_ino,
        "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
    }


def _fixture(tmp_path: Path, *, media_type: str = "Movie") -> tuple[
    DeletionJobStore, Path, Path, Path, Path
]:
    data_root = tmp_path / "data"
    media_root = data_root / "media"
    movie_dir = media_root / "movies" / "Test Film (2024)"
    movie_dir.mkdir(parents=True)
    video = movie_dir / "Test Film (2024).mkv"
    video.write_bytes(b"video fixture")
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(json.dumps({
        "filesystem_id": "media-uuid", "measured_at": time.time(),
    }), encoding="utf-8")
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    jobs.enqueue("a" * 32, media_type, {
        "media_key": "movie:tmdb:123", "file_path": str(video),
        "file_identity": _identity(video), "radarr_id": 7,
        "radarr_file_id": 9, "tmdb_id": 123,
    })
    return jobs, data_root, media_root, snapshot, video


def _coordinator(
    *, jobs: DeletionJobStore, data_root: Path, media_root: Path,
    snapshot: Path, client: httpx.AsyncClient,
) -> DeletionCoordinator:
    return DeletionCoordinator(
        jobs=jobs, data_root=data_root, media_root=media_root,
        snapshot_path=snapshot, filesystem_id="media-uuid",
        radarr_url="http://radarr", radarr_api_key="radarr-key",
        sonarr_url="http://sonarr", sonarr_api_key="sonarr-key",
        seerr_url="http://seerr", seerr_api_key="seerr-key",
        gateway_url="http://gateway", arr_token="gateway-key",
        jellyfin_url="http://jellyfin", jellyfin_api_key="jellyfin-key",
        client=client, mount_check=lambda path: path == data_root,
    )


def _add_movie_source(jobs: DeletionJobStore) -> None:
    with sqlite3.connect(jobs.path) as connection:
        connection.execute(
            "INSERT INTO requests(id,source_id,media_key,state,created_at,updated_at) "
            "VALUES ('seerr:77','77','movie:tmdb:123','available','now','now')"
        )
        connection.execute(
            "INSERT INTO reservations(id,request_id,media_key,filesystem_id,budget_bytes,state) "
            "VALUES ('reservation-1','seerr:77','movie:tmdb:123','media-uuid',0,'imported')"
        )
        connection.execute(
            """CREATE TABLE gateway_permits (
            permit_id TEXT PRIMARY KEY, token TEXT NOT NULL, operation_id TEXT NOT NULL,
            reservation_id TEXT, scope_key TEXT, infohash TEXT NOT NULL,
            metadata_sha256 TEXT, destination TEXT NOT NULL, category TEXT NOT NULL,
            selected_files_json TEXT NOT NULL, budget_bytes INTEGER,
            reported_seeders INTEGER, expires_at TEXT NOT NULL,
            state TEXT NOT NULL, result_json TEXT)"""
        )
        connection.execute(
            """INSERT INTO gateway_permits VALUES
            ('permit-1','permit-secret','operation-1','reservation-1',NULL,
             ?,NULL,'/data/torrents/movies','radarr','["Test Film.mkv"]',10,NULL,
             '2099-01-01T00:00:00+00:00','confirmed',NULL)""",
            ("f" * 40,),
        )


def _seerr_page(rows, skip):
    return {
        "pageInfo": {
            "pages": (len(rows) + 99) // 100, "pageSize": 100,
            "results": len(rows), "page": skip // 100 + 1,
        },
        "results": rows[skip:skip + 100],
    }


class SeerrCacheFixture:
    def __init__(self, media, requests=(), *, request_present=False):
        self.media = list(media)
        self.requests = list(requests)
        self.request_present = request_present
        self.calls = []
        self.media_reads = 0
        self.media_delete_status = 204
        self.mutate_on_media_read = None
        self.malformed_path = None

    def responder(self, request):
        path = request.url.path
        self.calls.append((request.method, path))
        if request.method == "GET" and path == "/api/v1/request/77":
            if not self.request_present:
                return httpx.Response(404)
            return httpx.Response(200, json={
                "id": 77,
                "media": {"id": 12, "tmdbId": 123, "mediaType": "movie"},
            })
        if request.method == "DELETE" and path == "/api/v1/request/77":
            self.request_present = False
            self.requests = [row for row in self.requests if row["id"] != 77]
            return httpx.Response(204)
        if request.method == "GET" and path in {"/api/v1/media", "/api/v1/request"}:
            assert request.url.params["take"] == "100"
            assert request.url.params["filter"] == "all"
            skip = int(request.url.params["skip"])
            if path == "/api/v1/request":
                assert request.url.params["mediaType"] == "movie"
                rows = self.requests
            else:
                self.media_reads += 1
                if self.mutate_on_media_read is not None:
                    self.mutate_on_media_read(self)
                rows = self.media
            if self.malformed_path == path:
                return httpx.Response(200, json={"results": rows})
            return httpx.Response(200, json=_seerr_page(rows, skip))
        if request.method == "DELETE" and path.startswith("/api/v1/media/"):
            assert path == "/api/v1/media/12"
            if self.media_delete_status == 204:
                self.media = [row for row in self.media if row["id"] != 12]
            return httpx.Response(self.media_delete_status)
        raise AssertionError(request)


async def _delete_movie_cache(tmp_path, service):
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    _add_movie_source(jobs)
    video.unlink()  # The Arr/Jellyfin stages already removed the captured file.

    def responder(request):
        if request.url.host == "jellyfin":
            assert request.url.path == "/Items"
            return httpx.Response(200, json={"Items": []})
        return service.responder(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        await coordinator._delete_seerr(jobs.get("a" * 32))
        await coordinator._delete_seerr_media_cache(jobs.get("a" * 32))


@pytest.mark.asyncio
async def test_missing_seerr_request_still_clears_matching_movie_cache(tmp_path):
    service = SeerrCacheFixture([
        {"id": 12, "tmdbId": 123, "mediaType": "movie", "status": 5},
        {"id": 21, "tmdbId": 123, "mediaType": "tv", "status": 5},
    ])
    await _delete_movie_cache(tmp_path, service)
    assert service.media == [{"id": 21, "tmdbId": 123, "mediaType": "tv", "status": 5}]
    assert ("DELETE", "/api/v1/media/12") in service.calls
    assert not any(path.endswith("/file") for _, path in service.calls)


@pytest.mark.asyncio
async def test_existing_seerr_request_is_removed_before_movie_cache(tmp_path):
    service = SeerrCacheFixture(
        [{"id": 12, "tmdbId": 123, "mediaType": "movie"}],
        [{"id": 77, "media": {"id": 12, "tmdbId": 123, "mediaType": "movie"}}],
        request_present=True,
    )
    await _delete_movie_cache(tmp_path, service)
    assert service.requests == [] and service.media == []
    assert service.calls.index(("DELETE", "/api/v1/request/77")) < service.calls.index(
        ("DELETE", "/api/v1/media/12"))


@pytest.mark.asyncio
async def test_seerr_cache_paginates_and_preserves_unrelated_rows(tmp_path):
    unrelated = [
        {"id": 1000 + index, "tmdbId": 2000 + index, "mediaType": "movie"}
        for index in range(100)
    ]
    service = SeerrCacheFixture(unrelated + [
        {"id": 12, "tmdbId": 123, "mediaType": "movie"},
    ])
    await _delete_movie_cache(tmp_path, service)
    assert service.media == unrelated
    assert service.calls.count(("GET", "/api/v1/media")) == 5
    assert service.calls.count(("DELETE", "/api/v1/media/12")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [
    "other_request", "other_request_on_second_page", "duplicate_movie", "malformed_media",
    "malformed_requests", "identity_changed", "duplicate_id",
])
async def test_seerr_cache_refuses_ambiguous_or_incomplete_evidence(tmp_path, unsafe):
    service = SeerrCacheFixture([{"id": 12, "tmdbId": 123, "mediaType": "movie"}])
    if unsafe == "other_request":
        service.requests = [{
            "id": 88, "media": {"id": 12, "tmdbId": 123, "mediaType": "movie"},
        }]
    elif unsafe == "other_request_on_second_page":
        service.requests = [
            {"id": index + 1000, "media": {
                "id": index + 2000, "tmdbId": index + 3000, "mediaType": "movie",
            }} for index in range(100)
        ] + [{"id": 88, "media": {"id": 12, "tmdbId": 123, "mediaType": "movie"}}]
    elif unsafe == "duplicate_movie":
        service.media.append({"id": 13, "tmdbId": 123, "mediaType": "movie"})
    elif unsafe == "malformed_media":
        service.malformed_path = "/api/v1/media"
    elif unsafe == "malformed_requests":
        service.malformed_path = "/api/v1/request"
    elif unsafe == "identity_changed":
        def change(fixture):
            if fixture.media_reads == 2:
                fixture.media[0] = {"id": 12, "tmdbId": 999, "mediaType": "movie"}
        service.mutate_on_media_read = change
    else:
        service.media.append({"id": 12, "tmdbId": 999, "mediaType": "movie"})
    with pytest.raises(ValueError):
        await _delete_movie_cache(tmp_path, service)
    assert not any(method == "DELETE" and path.startswith("/api/v1/media/")
                   for method, path in service.calls)


@pytest.mark.asyncio
async def test_seerr_cache_delete_failure_is_retryable_and_idempotent(tmp_path):
    service = SeerrCacheFixture([{"id": 12, "tmdbId": 123, "mediaType": "movie"}])
    service.media_delete_status = 503
    with pytest.raises(RuntimeError):
        await _delete_movie_cache(tmp_path / "first", service)
    assert len(service.media) == 1
    service.media_delete_status = 204
    await _delete_movie_cache(tmp_path / "second", service)
    await _delete_movie_cache(tmp_path / "third", service)
    assert service.media == []
    assert service.calls.count(("DELETE", "/api/v1/media/12")) == 2


@pytest.mark.asyncio
async def test_seerr_cache_delete_404_does_not_complete_while_entry_remains(tmp_path):
    service = SeerrCacheFixture([{"id": 12, "tmdbId": 123, "mediaType": "movie"}])
    service.media_delete_status = 404
    with pytest.raises(RuntimeError, match="cache still exists"):
        await _delete_movie_cache(tmp_path, service)
    assert len(service.media) == 1


@pytest.mark.asyncio
async def test_seerr_cache_blocks_recreated_identity_after_delete(tmp_path):
    service = SeerrCacheFixture([{"id": 12, "tmdbId": 123, "mediaType": "movie"}])

    def recreate(fixture):
        if fixture.media_reads == 3:
            fixture.media.append({"id": 13, "tmdbId": 123, "mediaType": "movie"})

    service.mutate_on_media_read = recreate
    with pytest.raises(ValueError, match="identity changed"):
        await _delete_movie_cache(tmp_path, service)
    assert ("DELETE", "/api/v1/media/12") in service.calls
    assert ("DELETE", "/api/v1/media/13") not in service.calls


@pytest.mark.asyncio
async def test_seerr_cache_waits_for_jellyfin_and_resumes_after_delete_error(tmp_path):
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    _add_movie_source(jobs)
    video.unlink()
    jobs.set_stage("a" * 32, "seerr_removed")
    service = SeerrCacheFixture([{"id": 12, "tmdbId": 123, "mediaType": "movie"}])
    service.media_delete_status = 503
    jelly_present = True
    calls = []

    def responder(request):
        nonlocal jelly_present
        calls.append((request.method, request.url.path))
        if request.url.host == "jellyfin":
            if request.url.path == "/Items":
                return httpx.Response(200, json={
                    "Items": [{"Id": "a" * 32}] if jelly_present else [],
                })
            if request.url.path == "/Library/Media/Updated":
                return httpx.Response(204)
        if request.url.host == "seerr":
            return service.responder(request)
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "retry"
        assert jobs.get("a" * 32)["stage"] == "seerr_removed"
        assert not any(path.startswith("/api/v1/media") for _, path in service.calls)
        jelly_present = False
        assert await coordinator.run_once() == "retry"
        assert jobs.get("a" * 32)["stage"] == "jellyfin_removed"
        service.media_delete_status = 204
        jelly_present = True
        assert await coordinator.run_once() == "retry"
        assert jobs.get("a" * 32)["stage"] == "jellyfin_removed"
        assert service.calls.count(("DELETE", "/api/v1/media/12")) == 1
        jelly_present = False
        assert await coordinator.run_once() == "complete"
    assert jobs.get("a" * 32)["stage"] == "complete"
    assert service.media == []
    assert calls.count(("GET", "/Items")) >= 5
    assert not any(path.startswith("/api/v3/") or path == "/api/v1/request/77"
                   for _, path in calls)


@pytest.mark.asyncio
async def test_seerr_cache_retry_does_not_remove_replaced_movie(tmp_path):
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    _add_movie_source(jobs)
    video.unlink()
    service = SeerrCacheFixture([{"id": 12, "tmdbId": 123, "mediaType": "movie"}])
    service.media_delete_status = 503

    def responder(request):
        if request.url.host == "jellyfin":
            return httpx.Response(200, json={"Items": []})
        return service.responder(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        with pytest.raises(RuntimeError):
            await coordinator._delete_seerr_media_cache(jobs.get("a" * 32))
        video.write_bytes(b"replacement")
        service.media_delete_status = 204
        with pytest.raises(ValueError, match="captured file changed"):
            await coordinator._delete_seerr_media_cache(jobs.get("a" * 32))
    assert service.calls.count(("DELETE", "/api/v1/media/12")) == 1
    assert video.read_bytes() == b"replacement"


class DeferredSourceUnlinkFixture:
    """qBit drops its catalog entry before its filesystem operation finishes."""

    def __init__(self, tmp_path):
        self.jobs, self.data, self.media, self.snapshot, self.video = _fixture(tmp_path)
        _add_movie_source(self.jobs)
        self.source = self.data / "source.mkv"
        os.link(self.video, self.source)
        self.gateway_calls = 0
        self.arr_mutations = []

    def responder(self, request):
        if request.url.host == "gateway":
            self.gateway_calls += 1
            return httpx.Response(200, json={
                "state": "deleted" if self.gateway_calls == 1 else "missing",
            })
        if request.url.host == "radarr":
            if request.method == "GET":
                return httpx.Response(200, json={
                    "id": 7, "tmdbId": 123,
                    "movieFile": {"id": 9, "path": str(self.video), "size": 13},
                })
            self.arr_mutations.append((request.method, request.url.path))
            if request.url.path == "/api/v3/moviefile/9":
                assert self.video.stat().st_nlink == 1
                self.video.unlink()
            else:
                assert not self.video.exists()
            return httpx.Response(204)
        if request.url.host == "seerr":
            if request.method == "GET" and request.url.path == "/api/v1/media":
                return httpx.Response(200, json=_seerr_page([], 0))
            return httpx.Response(404)
        if request.url.host == "jellyfin":
            return httpx.Response(200, json={"Items": []})
        raise AssertionError(request)

    def coordinator(self, client):
        return _coordinator(
            jobs=self.jobs, data_root=self.data, media_root=self.media,
            snapshot=self.snapshot, client=client,
        )


@pytest.mark.asyncio
async def test_movie_waits_for_asynchronous_source_unlink_before_arr_delete(tmp_path):
    fixture = DeferredSourceUnlinkFixture(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.responder)) as client:
        worker = fixture.coordinator(client)
        assert await worker.run_once() == "retry"
        assert fixture.jobs.get("a" * 32)["stage"] == "tombstoned"
        assert fixture.video.stat().st_nlink == 2
        assert fixture.arr_mutations == []
        fixture.source.unlink()  # qBit finishes the physical deletion between cycles.
        assert await worker.run_once() == "complete"
    assert fixture.jobs.get("a" * 32)["stage"] == "complete"
    assert fixture.gateway_calls == 2
    assert not fixture.video.exists()
    assert fixture.arr_mutations == [
        ("DELETE", "/api/v3/moviefile/9"), ("DELETE", "/api/v3/movie/7"),
    ]


@pytest.mark.asyncio
async def test_source_unlink_timeout_blocks_a_remaining_foreign_hardlink(tmp_path, monkeypatch):
    import homeserver_control.worker.deletion_coordinator as coordinator_module

    fixture = DeferredSourceUnlinkFixture(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(coordinator_module.time, "monotonic", lambda: clock[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.responder)) as client:
        worker = fixture.coordinator(client)
        assert await worker.run_once() == "retry"
        clock[0] = 129.9
        assert await worker.run_once() == "retry"
        clock[0] = 130.0
        assert await worker.run_once() == "blocked"
    assert fixture.jobs.get("a" * 32)["error"] == "media has another hardlink after torrent cleanup"
    assert fixture.arr_mutations == []
    assert fixture.video.exists() and fixture.source.exists()


@pytest.mark.asyncio
async def test_source_cleanup_with_more_than_two_links_blocks_immediately(tmp_path):
    fixture = DeferredSourceUnlinkFixture(tmp_path)
    foreign = fixture.data / "foreign.mkv"
    os.link(fixture.video, foreign)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.responder)) as client:
        assert await fixture.coordinator(client).run_once() == "blocked"
    assert fixture.video.stat().st_nlink == 3
    assert fixture.arr_mutations == []
    assert foreign.exists()


@pytest.mark.asyncio
async def test_waiting_source_unlink_revalidates_file_identity_before_retry(tmp_path):
    fixture = DeferredSourceUnlinkFixture(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.responder)) as client:
        worker = fixture.coordinator(client)
        assert await worker.run_once() == "retry"
        fixture.video.unlink()
        fixture.video.write_bytes(b"a replacement video")
        assert await worker.run_once() == "blocked"
    assert fixture.gateway_calls == 1
    assert fixture.arr_mutations == []
    assert fixture.video.read_bytes() == b"a replacement video"


@pytest.mark.asyncio
async def test_waiting_source_unlink_revalidates_mount_before_retry(tmp_path):
    fixture = DeferredSourceUnlinkFixture(tmp_path)
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.responder)) as client:
        worker = fixture.coordinator(client)
        assert await worker.run_once() == "retry"
        fixture.snapshot.write_text(json.dumps({
            "filesystem_id": "media-uuid", "measured_at": time.time() - 60,
        }))
        assert await worker.run_once() == "retry"
    assert fixture.gateway_calls == 1
    assert fixture.arr_mutations == []
    assert fixture.video.stat().st_nlink == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", ["torrent contains other media", "private permit-secret"])
async def test_gateway_block_reason_is_persisted_without_upstream_secrets(tmp_path, detail):
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    _add_movie_source(jobs)
    requests = []

    def responder(request):
        requests.append(request)
        if request.url.host == "radarr" and request.method == "GET":
            return httpx.Response(200, json={
                "id": 7, "tmdbId": 123,
                "movieFile": {"id": 9, "path": str(video), "size": 13},
            })
        if request.url.host == "gateway":
            return httpx.Response(409, json={"detail": detail})
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        worker = _coordinator(jobs=jobs, data_root=data_root, media_root=media_root,
                              snapshot=snapshot, client=client)
        assert await worker.run_once() == "blocked"
    error = jobs.get("a" * 32)["error"]
    assert "HTTP 409" in error
    assert "permit-secret" not in error
    if detail == "torrent contains other media":
        assert detail in error
    assert video.exists()
    assert not any(r.method == "DELETE" for r in requests)


@pytest.mark.asyncio
async def test_movie_cascade_removes_captured_source_and_completes(tmp_path: Path) -> None:
    """A missing Arr DELETE or wrong source selection must leave this test red."""
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    _add_movie_source(jobs)
    calls: list[tuple[str, str]] = []
    seerr_media = [{"id": 12, "tmdbId": 123, "mediaType": "movie"}]

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.host == "radarr" and request.method == "GET":
            return httpx.Response(200, json={
                "id": 7, "tmdbId": 123,
                "movieFile": {"id": 9, "path": str(video), "size": 13},
            })
        if request.url.host == "radarr" and request.url.path == "/api/v3/moviefile/9":
            assert dict(request.url.params) == {}
            video.unlink()
            return httpx.Response(204)
        if request.url.host == "radarr" and request.method == "DELETE":
            assert dict(request.url.params) == {
                "deleteFiles": "false", "addImportExclusion": "true",
            }
            assert not video.exists()
            return httpx.Response(200, json={})
        if request.url.host == "gateway":
            assert request.headers["X-Arr-Token"] == "gateway-key"
            assert json.loads(request.content) == {
                "permit_token": "permit-secret", "media_key": "movie:tmdb:123",
                "scope_key": None,
            }
            return httpx.Response(200, json={"state": "deleted"})
        if request.url.host == "seerr" and request.method == "GET":
            if request.url.path == "/api/v1/request/77":
                return httpx.Response(200, json={
                    "id": 77, "media": {"id": 12, "tmdbId": 123, "mediaType": "movie"},
                })
            if request.url.path == "/api/v1/request":
                return httpx.Response(200, json=_seerr_page([], 0))
            if request.url.path == "/api/v1/media":
                return httpx.Response(200, json=_seerr_page(seerr_media, 0))
        if request.url.host == "seerr" and request.method == "DELETE":
            if request.url.path == "/api/v1/media/12":
                seerr_media.clear()
            return httpx.Response(204)
        if request.url.host == "jellyfin":
            assert not video.exists()
            assert request.headers["X-Emby-Token"] == "jellyfin-key"
            assert request.method == "GET" and request.url.path == "/Items"
            return httpx.Response(200, json={"Items": []})
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "complete"

    assert jobs.get("a" * 32)["stage"] == "complete"
    assert jobs.is_tombstoned("movie:tmdb:123")
    assert not video.exists()
    assert calls == [
        ("GET", "/api/v3/movie/7"), ("POST", "/internal/delete-source"),
        ("GET", "/api/v3/movie/7"), ("DELETE", "/api/v3/moviefile/9"),
        ("DELETE", "/api/v3/movie/7"),
        ("GET", "/api/v1/request/77"),
        ("DELETE", "/api/v1/request/77"), ("GET", "/Items"),
        ("GET", "/api/v1/media"), ("GET", "/api/v1/request"),
        ("GET", "/api/v1/media"), ("GET", "/Items"),
        ("DELETE", "/api/v1/media/12"),
        ("GET", "/api/v1/media"),
    ]


@pytest.mark.asyncio
async def test_replaced_file_blocks_before_tombstone_or_remote_delete(tmp_path: Path) -> None:
    """An inode changed after capture cannot be deleted by a stale queued job."""
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    video.unlink()
    video.write_bytes(b"replacement")
    calls: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise AssertionError("unsafe request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "blocked"

    assert jobs.get("a" * 32)["stage"] == "blocked"
    assert not jobs.is_tombstoned("movie:tmdb:123")
    assert video.read_bytes() == b"replacement"
    assert calls == []


@pytest.mark.asyncio
async def test_exact_sidecars_are_removed_without_touching_other_media(tmp_path: Path) -> None:
    """Removing one movie may clean its subtitles, not a neighboring title's files."""
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    ptbr = video.with_name(f"{video.stem}.pt-BR.srt")
    english = video.with_name(f"{video.stem}.en.srt")
    forced = video.with_name(f"{video.stem}.pt-BR.forced.srt")
    sdh = video.with_name(f"{video.stem}.en.sdh.srt")
    neighbor = video.with_name("Another Film.pt-BR.srt")
    ptbr.write_text("pt-br", encoding="utf-8")
    english.write_text("en", encoding="utf-8")
    forced.write_text("forced", encoding="utf-8")
    sdh.write_text("sdh", encoding="utf-8")
    neighbor.write_text("neighbor", encoding="utf-8")

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "radarr" and request.method == "GET":
            return httpx.Response(200, json={
                "id": 7, "tmdbId": 123,
                "movieFile": {"id": 9, "path": str(video), "size": 13},
            })
        if request.url.host == "radarr" and request.url.path == "/api/v3/moviefile/9":
            video.unlink()
            return httpx.Response(200, json={})
        if request.url.host == "radarr" and request.method == "DELETE":
            assert dict(request.url.params)["deleteFiles"] == "false"
            return httpx.Response(200, json={})
        if request.url.host == "jellyfin":
            assert not any(path.exists() for path in (ptbr, english, forced, sdh))
            assert neighbor.read_text(encoding="utf-8") == "neighbor"
            assert request.method == "GET" and request.url.path == "/Items"
            return httpx.Response(200, json={"Items": []})
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "complete"

    assert not any(path.exists() for path in (ptbr, english, forced, sdh))
    assert neighbor.read_text(encoding="utf-8") == "neighbor"


@pytest.mark.asyncio
async def test_shared_source_conflict_keeps_arr_media_and_jellyfin_item(tmp_path: Path) -> None:
    """A gateway 409 must stop before deleting the imported file or Arr record."""
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    _add_movie_source(jobs)
    calls: list[tuple[str, str]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.host == "radarr" and request.method == "GET":
            return httpx.Response(200, json={
                "id": 7, "tmdbId": 123,
                "movieFile": {"id": 9, "path": str(video), "size": 13},
            })
        if request.url.host == "gateway":
            return httpx.Response(409, json={"detail": "shared source"})
        raise AssertionError("deletion must stop before Arr mutation")

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "blocked"

    assert video.read_bytes() == b"video fixture"
    assert jobs.get("a" * 32)["stage"] == "blocked"
    assert calls == [
        ("GET", "/api/v3/movie/7"), ("POST", "/internal/delete-source"),
    ]


@pytest.mark.asyncio
async def test_arr_must_remove_captured_file_without_local_fallback(tmp_path: Path) -> None:
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    calls: list[tuple[str, str]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.host == "radarr" and request.method == "GET":
            return httpx.Response(200, json={
                "id": 7, "tmdbId": 123,
                "movieFile": {"id": 9, "path": str(video), "size": 13},
            })
        if request.url.host == "radarr" and request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError("no Seerr or Jellyfin cleanup after Arr leaves file")

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "blocked"
    assert video.read_bytes() == b"video fixture"
    assert jobs.get("a" * 32)["stage"] == "blocked"
    assert calls[-1] == ("DELETE", "/api/v3/moviefile/9")


@pytest.mark.asyncio
async def test_episode_hardlink_is_removed_only_after_exact_sonarr_scope(tmp_path: Path) -> None:
    """The torrent link goes first; Sonarr unmonitors only the chosen episode."""
    data_root = tmp_path / "data"
    media_root = data_root / "media"
    folder = media_root / "tv" / "Test Series" / "Season 01"
    folder.mkdir(parents=True)
    video = folder / "Test Series S01E02.mkv"
    video.write_bytes(b"episode fixture")
    torrent = data_root / "torrents" / "source.mkv"
    torrent.parent.mkdir()
    os.link(video, torrent)
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(json.dumps({
        "filesystem_id": "media-uuid", "measured_at": time.time(),
    }), encoding="utf-8")
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    jobs.enqueue("b" * 32, "Episode", {
        "media_key": "episode:tmdb:321:S01E02", "file_path": str(video),
        "file_identity": _identity(video), "sonarr_series_id": 3,
        "sonarr_episode_id": 4, "sonarr_episode_file_id": 5,
        "series_tmdb_id": 321, "season": 1, "episode": 2,
    })
    with sqlite3.connect(jobs.path) as connection:
        connection.execute(
            "INSERT INTO requests(id,source_id,media_key,state,created_at,updated_at) "
            "VALUES ('seerr:88:1','88:1','season:tmdb:321:1','available','now','now')"
        )
        connection.execute(
            "INSERT INTO reservations(id,request_id,media_key,filesystem_id,budget_bytes,state) "
            "VALUES ('reservation-2','seerr:88:1','season:tmdb:321:1','media-uuid',0,'imported')"
        )
        connection.execute(
            """CREATE TABLE gateway_permits (
            permit_id TEXT PRIMARY KEY, token TEXT NOT NULL, operation_id TEXT NOT NULL,
            reservation_id TEXT, scope_key TEXT, infohash TEXT NOT NULL,
            metadata_sha256 TEXT, destination TEXT NOT NULL, category TEXT NOT NULL,
            selected_files_json TEXT NOT NULL, budget_bytes INTEGER,
            reported_seeders INTEGER, expires_at TEXT NOT NULL,
            state TEXT NOT NULL, result_json TEXT)"""
        )
        connection.execute(
            """INSERT INTO gateway_permits VALUES
            ('permit-2','episode-permit','operation-2','reservation-2','S01E02',
             ?,NULL,'/data/torrents/tv','sonarr','["source.mkv"]',10,NULL,
             '2099-01-01T00:00:00+00:00','confirmed',NULL)""",
            ("e" * 40,),
        )
    calls: list[tuple[str, str]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.host == "sonarr" and request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 4, "seriesId": 3, "seasonNumber": 1,
                 "episodeNumber": 2, "episodeFileId": 5, "monitored": True},
                {"id": 6, "seriesId": 3, "seasonNumber": 1,
                 "episodeNumber": 3, "episodeFileId": 7, "monitored": True},
            ])
        if request.url.host == "sonarr" and request.method == "GET":
            return httpx.Response(200, json={
                "id": 5, "path": str(video), "size": 15,
            })
        if request.url.host == "gateway":
            assert json.loads(request.content) == {
                "permit_token": "episode-permit", "media_key": "season:tmdb:321:1",
                "scope_key": "S01E02",
            }
            torrent.unlink()
            return httpx.Response(200, json={"state": "deleted"})
        if request.url.host == "sonarr" and request.method == "PUT":
            assert json.loads(request.content) == {
                "episodeIds": [4], "monitored": False,
            }
            return httpx.Response(200, json={})
        if request.url.host == "sonarr" and request.method == "DELETE":
            video.unlink()
            return httpx.Response(200, json={})
        if request.url.host == "jellyfin":
            assert not video.exists()
            assert request.method == "GET" and request.url.path == "/Items"
            return httpx.Response(200, json={"Items": []})
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "complete"

    assert not video.exists() and not torrent.exists()
    assert jobs.is_tombstoned("episode:tmdb:321:S01E02")
    assert calls[-1] == ("GET", "/Items")
    assert not any(path.startswith("/api/v1/request") for _, path in calls)


@pytest.mark.asyncio
async def test_stale_mount_snapshot_retries_without_any_effect(tmp_path: Path) -> None:
    """Loss of mount proof must not tombstone or call any external service."""
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    snapshot.write_text(json.dumps({
        "filesystem_id": "media-uuid", "measured_at": time.time() - 60,
    }), encoding="utf-8")

    def responder(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected remote effect: {request}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "retry"

    assert jobs.get("a" * 32)["stage"] == "queued"
    assert not jobs.is_tombstoned("movie:tmdb:123")
    assert video.exists()


@pytest.mark.asyncio
async def test_lost_radarr_response_retries_without_second_delete(tmp_path: Path) -> None:
    """A crash window after Arr deletion must resume at the missing record."""
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    delete_count = 0
    record_deleted = False

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal delete_count, record_deleted
        if request.url.host == "radarr" and request.method == "GET":
            if record_deleted:
                return httpx.Response(404)
            return httpx.Response(200, json={
                "id": 7, "tmdbId": 123,
                "movieFile": (
                    {"id": 9, "path": str(video), "size": 13}
                    if video.exists() else None
                ),
            })
        if request.url.host == "radarr" and request.url.path == "/api/v3/moviefile/9":
            delete_count += 1
            video.unlink()
            raise httpx.ReadTimeout("upstream response lost")
        if request.url.host == "radarr" and request.method == "DELETE":
            assert dict(request.url.params)["deleteFiles"] == "false"
            record_deleted = True
            return httpx.Response(204)
        if request.url.host == "jellyfin":
            assert request.method == "GET" and request.url.path == "/Items"
            return httpx.Response(200, json={"Items": []})
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "retry"
        assert jobs.get("a" * 32)["stage"] == "torrents_removed"
        assert await coordinator.run_once() == "complete"

    assert delete_count == 1
    assert jobs.get("a" * 32)["stage"] == "complete"


@pytest.mark.asyncio
async def test_symlinked_sidecar_blocks_without_touching_target(tmp_path: Path) -> None:
    """A matching subtitle symlink may not be followed or removed."""
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    outside = tmp_path / "outside.srt"
    outside.write_text("protected", encoding="utf-8")
    sidecar = video.with_name(f"{video.stem}.pt-BR.srt")
    sidecar.symlink_to(outside)

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "radarr" and request.method == "GET":
            return httpx.Response(200, json={
                "id": 7, "tmdbId": 123,
                "movieFile": {"id": 9, "path": str(video), "size": 13},
            })
        if request.url.host == "radarr" and request.url.path == "/api/v3/moviefile/9":
            video.unlink()
            return httpx.Response(200, json={})
        if request.url.host == "radarr" and request.method == "DELETE":
            assert dict(request.url.params)["deleteFiles"] == "false"
            return httpx.Response(204)
        raise AssertionError("Jellyfin must not be called after unsafe sidecar")

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "blocked"

    assert jobs.get("a" * 32)["stage"] == "blocked"
    assert sidecar.is_symlink()
    assert outside.read_text(encoding="utf-8") == "protected"


@pytest.mark.asyncio
async def test_jellyfin_catalog_sync_preserves_read_only_movie_folder(tmp_path: Path) -> None:
    """After Arr removes a file, Jellyfin must prune its catalog without deleting the folder."""
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    video.unlink()
    jobs.set_stage("a" * 32, "seerr_removed")
    notified = refreshed = False
    calls: list[tuple[str, str]] = []

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal notified, refreshed
        calls.append((request.method, request.url.path))
        if request.url.host != "jellyfin":
            raise AssertionError(request)
        if request.method == "GET" and request.url.path == "/Items":
            assert request.url.params["Ids"] == "a" * 32
            return httpx.Response(200, json={
                "Items": [] if refreshed else [{"Id": "a" * 32}],
            })
        if request.method == "POST" and request.url.path == "/Library/Media/Updated":
            assert json.loads(request.content) == {"Updates": [{
                "Path": str(video), "UpdateType": "Deleted",
            }]}
            notified = True
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/Library/Refresh":
            assert notified
            refreshed = True
            return httpx.Response(204)
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "retry"
        assert notified and not refreshed
        coordinator._jellyfin_notify_at["a" * 32] = time.monotonic() - 91
        assert await coordinator.run_once() == "complete"

    assert video.parent.is_dir()
    assert jobs.get("a" * 32)["stage"] == "complete"
    assert ("DELETE", "/Items/" + "a" * 32) not in calls
    assert ("POST", "/Library/Refresh") in calls


@pytest.mark.asyncio
async def test_jellyfin_refresh_204_does_not_complete_unpruned_item(tmp_path: Path) -> None:
    jobs, data_root, media_root, snapshot, video = _fixture(tmp_path)
    video.unlink()
    jobs.set_stage("a" * 32, "seerr_removed")

    refreshes = 0

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal refreshes
        if request.url.host != "jellyfin":
            raise AssertionError(request)
        if request.method == "GET" and request.url.path == "/Items":
            return httpx.Response(200, json={"Items": [{"Id": "a" * 32}]})
        if request.method == "POST" and request.url.path == "/Library/Refresh":
            refreshes += 1
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/Library/Media/Updated":
            return httpx.Response(204)
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "retry"
        assert await coordinator.run_once() == "retry"
        assert refreshes == 0
        coordinator._jellyfin_notify_at["a" * 32] = time.monotonic() - 91
        assert await coordinator.run_once() == "retry"
        assert await coordinator.run_once() == "retry"

    assert jobs.get("a" * 32)["stage"] == "seerr_removed"
    assert refreshes == 1


@pytest.mark.asyncio
async def test_jellyfin_item_lookup_accepts_case_variant(tmp_path: Path) -> None:
    jobs, data_root, media_root, snapshot, _video = _fixture(tmp_path)

    def responder(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Items"
        return httpx.Response(200, json={"Items": [{"Id": "a" * 32}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator._jellyfin_item_present("A" * 32)


@pytest.mark.asyncio
async def test_jellyfin_episode_sync_keeps_season_folder_and_other_episode(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    media_root = data_root / "media"
    season = media_root / "tv" / "Test Series" / "Season 01"
    season.mkdir(parents=True)
    video = season / "Test Series S01E02.mkv"
    video.write_bytes(b"episode fixture")
    neighbor = season / "Test Series S01E03.mkv"
    neighbor.write_bytes(b"keep me")
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(json.dumps({
        "filesystem_id": "media-uuid", "measured_at": time.time(),
    }), encoding="utf-8")
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    jobs.enqueue("b" * 32, "Episode", {
        "media_key": "episode:tmdb:321:S01E02", "file_path": str(video),
        "file_identity": _identity(video), "sonarr_series_id": 3,
        "sonarr_episode_id": 4, "sonarr_episode_file_id": 5,
        "series_tmdb_id": 321, "season": 1, "episode": 2,
    })
    jobs.set_stage("b" * 32, "seerr_removed")
    video.unlink()
    notified = False

    def responder(request: httpx.Request) -> httpx.Response:
        nonlocal notified
        if request.url.host != "jellyfin":
            raise AssertionError(request)
        if request.method == "GET" and request.url.path == "/Items":
            return httpx.Response(200, json={
                "Items": [] if notified else [{"Id": "b" * 32}],
            })
        if request.method == "POST" and request.url.path == "/Library/Media/Updated":
            assert json.loads(request.content) == {"Updates": [{
                "Path": str(video), "UpdateType": "Deleted",
            }]}
            notified = True
            return httpx.Response(204)
        raise AssertionError(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
        coordinator = _coordinator(
            jobs=jobs, data_root=data_root, media_root=media_root,
            snapshot=snapshot, client=client,
        )
        assert await coordinator.run_once() == "complete"

    assert jobs.get("b" * 32)["stage"] == "complete"
    assert season.is_dir()
    assert neighbor.read_bytes() == b"keep me"
