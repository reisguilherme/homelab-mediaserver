"""The worker can remove only a uniquely owned, verified torrent payload."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore

TORRENT = (
    b"d4:info"
    + b"d6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:pieces20:"
    + b"a" * 20
    + b"ee"
)


class TorrentUpstream:
    def __init__(self, infohash: str) -> None:
        self.infohash = infohash
        self.current = {
            "hash": infohash, "category": "radarr", "save_path": "/data/torrents",
        }
        self.files: list[dict] = [{"name": "test.mp4", "size": 123, "priority": 1}]
        self.deleted: list[tuple[str, bool]] = []
        self.reads: list[str] = []

    def read(self, path: str, params: dict | None = None) -> object:
        self.reads.append(path)
        if path == "/api/v2/torrents/info":
            assert params == {"hashes": self.infohash}
            return [self.current] if self.current is not None else []
        if path == "/api/v2/torrents/files":
            assert params == {"hash": self.infohash}
            return self.files
        raise AssertionError(path)

    def delete_torrent(self, infohash: str, *, delete_files: bool) -> None:
        self.deleted.append((infohash, delete_files))
        self.current = None


def _setup(tmp_path, *, media_key: str = "movie:tmdb:123", scope_key: str | None = None):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(
        request_id="seerr:1", source_id="1", media_key=media_key,
        filesystem_id="fixture", budget_bytes=123, free_bytes=1_000,
        total_bytes=2_000,
    )
    metadata = inspect_torrent(TORRENT)
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash=metadata.infohash, destination="/data/torrents",
        category="sonarr" if scope_key else "radarr",
        reservation_id=reservation.reservation_id, scope_key=scope_key,
        metadata_sha256=metadata.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    store = TorrentArtifactStore(database)
    store.put(permit, TORRENT)
    upstream = TorrentUpstream(metadata.infohash)
    upstream.current["category"] = permit.category
    client = TestClient(create_app(
        permits=permits, upstream=upstream, arr_token="secret", torrent_store=store,
    ))
    body = {"permit_token": permit.token, "media_key": media_key, "scope_key": scope_key}
    return client, permits, upstream, body


def test_worker_delete_removes_exact_torrent_and_data_and_is_idempotent(tmp_path) -> None:
    client, _, upstream, body = _setup(tmp_path)
    first = client.post("/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body)
    assert first.status_code == 200
    assert first.json() == {"state": "deleted"}
    assert upstream.deleted == [(upstream.infohash, True)]
    second = client.post("/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body)
    assert second.status_code == 200
    assert second.json() == {"state": "missing"}
    assert upstream.deleted == [(upstream.infohash, True)]
    assert client.post(
        "/api/v2/torrents/delete", headers={"X-Arr-Token": "secret"},
        data={"hashes": upstream.infohash, "deleteFiles": "true"},
    ).status_code == 404


def test_worker_delete_accepts_qbit_trailing_slash_on_save_path(tmp_path) -> None:
    client, _, upstream, body = _setup(tmp_path)
    upstream.current["save_path"] = "/data/torrents/"
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 200
    assert upstream.deleted == [(upstream.infohash, True)]


def test_worker_delete_retries_if_qbit_still_lists_torrent_after_delete(tmp_path) -> None:
    client, _, upstream, body = _setup(tmp_path)

    def delayed_delete(infohash: str, *, delete_files: bool) -> None:
        upstream.deleted.append((infohash, delete_files))

    upstream.delete_torrent = delayed_delete
    first = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert first.status_code == 503
    assert upstream.deleted == [(upstream.infohash, True)]
    upstream.current = None
    second = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert second.status_code == 200
    assert second.json() == {"state": "missing"}


def test_worker_delete_accepts_one_exact_episode_source(tmp_path) -> None:
    client, _, upstream, body = _setup(
        tmp_path, media_key="season:tmdb:123:1", scope_key="S01E02",
    )
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 200
    assert upstream.deleted == [(upstream.infohash, True)]


def test_worker_delete_rejects_episode_permit_for_another_season(tmp_path) -> None:
    client, _, upstream, body = _setup(
        tmp_path, media_key="season:tmdb:123:1", scope_key="S02E01",
    )
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 403
    assert upstream.deleted == []


@pytest.mark.parametrize("changed", [
    {"permit_token": []}, {"media_key": []}, {"scope_key": []},
])
def test_worker_delete_rejects_malformed_fields_before_upstream_read(tmp_path, changed) -> None:
    client, _, upstream, body = _setup(tmp_path)
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"},
        json=body | changed,
    )
    assert response.status_code == 422
    assert upstream.reads == []
    assert upstream.deleted == []


@pytest.mark.parametrize("headers,changed", [
    ({}, {}),
    ({"X-Arr-Token": "wrong"}, {}),
    ({"X-Arr-Token": "secret"}, {"media_key": "movie:tmdb:456"}),
    ({"X-Arr-Token": "secret"}, {"scope_key": "S01E01"}),
])
def test_worker_delete_rejects_wrong_identity_before_upstream_read(
    tmp_path, headers, changed,
) -> None:
    client, _, upstream, body = _setup(tmp_path)
    response = client.post("/internal/delete-source", headers=headers, json=body | changed)
    assert response.status_code == 403
    assert upstream.reads == []
    assert upstream.deleted == []


@pytest.mark.parametrize("change", [
    ("current", "category", "sonarr"),
    ("current", "save_path", "/data/elsewhere"),
    ("files", "name", "other.mp4"),
    ("files", "size", 124),
    ("files", "priority", 0),
])
def test_worker_delete_rejects_changed_qbit_identity_or_files(tmp_path, change) -> None:
    client, _, upstream, body = _setup(tmp_path)
    target, field, value = change
    if target == "current":
        upstream.current[field] = value
    else:
        upstream.files[0][field] = value
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 409
    assert upstream.deleted == []


def test_worker_delete_keeps_torrent_with_extra_file(tmp_path) -> None:
    client, _, upstream, body = _setup(tmp_path)
    upstream.files.append({"name": "other.mkv", "size": 5, "priority": 1})
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 409
    assert upstream.deleted == []


def test_worker_delete_keeps_torrent_claimed_by_another_permit(tmp_path) -> None:
    client, permits, upstream, body = _setup(tmp_path)
    repo = ReservationRepository(tmp_path / "control.sqlite")
    other = repo.reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:456",
        filesystem_id="fixture", budget_bytes=123, free_bytes=1_000,
        total_bytes=2_000,
    )
    conflict = permits.issue(
        infohash=upstream.infohash, destination="/data/torrents", category="radarr",
        reservation_id=other.reservation_id,
        metadata_sha256=inspect_torrent(TORRENT).metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=conflict.token, infohash=conflict.infohash,
        destination=conflict.destination, metadata_sha256=conflict.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 409
    assert upstream.deleted == []


def test_worker_delete_refuses_without_verified_manifest(tmp_path) -> None:
    client, _, upstream, body = _setup(tmp_path)
    import sqlite3

    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        connection.execute("DELETE FROM torrent_artifacts")
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 409
    assert upstream.deleted == []
