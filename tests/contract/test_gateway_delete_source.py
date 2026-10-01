"""The worker can remove only a uniquely owned, verified torrent payload."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
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


def _setup(
    tmp_path, *, media_key: str = "movie:tmdb:123", scope_key: str | None = None,
    torrent: bytes = TORRENT, selected_files: tuple[str, ...] | None = None,
):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    metadata = inspect_torrent(torrent)
    reservation = repo.reserve(
        request_id="seerr:1", source_id="1", media_key=media_key,
        filesystem_id="fixture", budget_bytes=metadata.total_bytes,
        free_bytes=metadata.total_bytes + 1000, total_bytes=metadata.total_bytes + 2000,
    )
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash=metadata.infohash, destination="/data/torrents",
        category="sonarr" if scope_key else "radarr",
        reservation_id=reservation.reservation_id, scope_key=scope_key,
        metadata_sha256=metadata.metadata_sha256,
        selected_files=(
            selected_files if selected_files is not None else tuple(f.path for f in metadata.files)
        ), budget_bytes=metadata.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    store = TorrentArtifactStore(database)
    store.put(permit, torrent)
    upstream = TorrentUpstream(metadata.infohash)
    upstream.current["category"] = permit.category
    upstream.files = [{"name": f.path, "size": f.length, "priority": 1} for f in metadata.files]
    client = TestClient(create_app(
        permits=permits, upstream=upstream, arr_token="secret", torrent_store=store,
    ))
    body = {"permit_token": permit.token, "media_key": media_key, "scope_key": scope_key}
    return client, permits, upstream, body


def _encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    return b"d" + b"".join(_encode(k) + _encode(v) for k, v in sorted(value.items())) + b"e"


def _movie_with_auxiliary(name="AUDIO LIST ENG LATINO SPANISH FRENCH.txt", size=18712):
    total = 123 + size
    return _encode({b"info": {
        b"name": b"Movie", b"piece length": 16384,
        b"pieces": b"a" * (((total + 16383) // 16384) * 20),
        b"files": [
            {b"length": 123, b"path": [b"Movie.mkv"]},
            {b"length": size, b"path": [name.encode()]},
        ],
    }})


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


@pytest.mark.parametrize("state", ["superseded", "probe_rejected"])
def test_explicit_season_deletion_purges_verified_historical_episode_source(tmp_path, state):
    client, _, upstream, body = _setup(
        tmp_path, media_key="season:tmdb:123:1", scope_key="S01E02",
    )
    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        connection.execute("UPDATE gateway_permits SET state=?", (state,))
    DeletionJobStore(tmp_path / "control.sqlite").tombstone("season:tmdb:123:1", "a" * 32)
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 200
    assert response.json() == {"state": "deleted"}
    assert upstream.deleted == [(upstream.infohash, True)]
    repeated = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert repeated.status_code == 200
    assert repeated.json() == {"state": "missing"}
    assert upstream.deleted == [(upstream.infohash, True)]


@pytest.mark.parametrize("state", ["superseded", "probe_rejected"])
@pytest.mark.parametrize("tombstone", [None, "season:tmdb:123:2", "episode:tmdb:123:S01E02"])
def test_historical_episode_source_requires_exact_whole_season_deletion(
    tmp_path, state, tombstone,
):
    client, _, upstream, body = _setup(
        tmp_path, media_key="season:tmdb:123:1", scope_key="S01E02",
    )
    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        connection.execute("UPDATE gateway_permits SET state=?", (state,))
    if tombstone:
        DeletionJobStore(tmp_path / "control.sqlite").tombstone(tombstone, "a" * 32)
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 403
    assert upstream.reads == []
    assert upstream.deleted == []


@pytest.mark.parametrize("state", ["superseded", "probe_rejected"])
def test_historical_movie_source_cannot_use_the_season_deletion_permission(tmp_path, state):
    client, _, upstream, body = _setup(tmp_path)
    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        connection.execute("UPDATE gateway_permits SET state=?", (state,))
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.tombstone("movie:tmdb:123", "a" * 32)
    jobs.tombstone("season:tmdb:123:1", "b" * 32)
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 403
    assert upstream.reads == []
    assert upstream.deleted == []


@pytest.mark.parametrize("state", ["authorized", "dispatching", "unknown"])
def test_whole_season_deletion_does_not_allow_uncertain_episode_source(tmp_path, state):
    client, _, upstream, body = _setup(
        tmp_path, media_key="season:tmdb:123:1", scope_key="S01E02",
    )
    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        connection.execute("UPDATE gateway_permits SET state=?", (state,))
    DeletionJobStore(tmp_path / "control.sqlite").tombstone("season:tmdb:123:1", "a" * 32)
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 403
    assert upstream.reads == []
    assert upstream.deleted == []


@pytest.mark.parametrize("state", ["superseded", "probe_rejected"])
def test_historical_season_source_retains_manifest_and_ownership_guards(tmp_path, state):
    client, permits, upstream, body = _setup(
        tmp_path, media_key="season:tmdb:123:1", scope_key="S01E02",
    )
    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        connection.execute("UPDATE gateway_permits SET state=?", (state,))
    DeletionJobStore(tmp_path / "control.sqlite").tombstone("season:tmdb:123:1", "a" * 32)
    other = ReservationRepository(tmp_path / "control.sqlite").reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:456",
        filesystem_id="fixture", budget_bytes=123, free_bytes=1000, total_bytes=2000,
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
    assert upstream.reads == []
    assert upstream.deleted == []


@pytest.mark.parametrize("state", ["superseded", "probe_rejected"])
@pytest.mark.parametrize("change", ["manifest", "category", "path", "file", "size", "extra"])
def test_historical_season_source_revalidates_every_torrent_before_purge(tmp_path, state, change):
    client, _, upstream, body = _setup(
        tmp_path, media_key="season:tmdb:123:1", scope_key="S01E02",
    )
    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        connection.execute("UPDATE gateway_permits SET state=?", (state,))
        if change == "manifest":
            connection.execute("DELETE FROM torrent_artifacts")
    DeletionJobStore(tmp_path / "control.sqlite").tombstone("season:tmdb:123:1", "a" * 32)
    if change == "category":
        upstream.current["category"] = "radarr"
    elif change == "path":
        upstream.current["save_path"] = "/data/other"
    elif change == "file":
        upstream.files[0]["name"] = "another.mkv"
    elif change == "size":
        upstream.files[0]["size"] = 124
    elif change == "extra":
        upstream.files.append({"name": "extra.mkv", "size": 10, "priority": 1})
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 409
    assert upstream.deleted == []


@pytest.mark.parametrize("priority", [0, 1])
def test_movie_delete_accepts_unselected_audio_list_txt_from_verified_manifest(tmp_path, priority):
    client, _, upstream, body = _setup(
        tmp_path, torrent=_movie_with_auxiliary(), selected_files=("Movie/Movie.mkv",),
    )
    upstream.files[1]["priority"] = priority
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 200
    assert response.json() == {"state": "deleted"}
    assert upstream.deleted == [(upstream.infohash, True)]


@pytest.mark.parametrize("name", ["movie.nfo", "pt-BR.srt", "en.ass", "subs.ssa",
                                  "captions.vtt", "poster.jpg", "folder.jpeg",
                                  "cover.png", "thumb.webp"])
def test_movie_delete_accepts_small_verified_unselected_metadata_and_subtitles(tmp_path, name):
    client, _, upstream, body = _setup(
        tmp_path, torrent=_movie_with_auxiliary(name), selected_files=("Movie/Movie.mkv",),
    )
    upstream.files[1]["priority"] = 0
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 200
    assert upstream.deleted == [(upstream.infohash, True)]


@pytest.mark.parametrize("name,size", [
    ("other.mkv", 100), ("sample.mp4", 100), ("download.exe", 100), ("data.bin", 100),
    ("poster.jpg", 50_000_001), ("audio.txt", 50_000_001),
])
def test_movie_delete_refuses_unselected_extra_media_unknown_files_or_large_auxiliary(
    tmp_path, name, size,
):
    client, _, upstream, body = _setup(
        tmp_path, torrent=_movie_with_auxiliary(name, size), selected_files=("Movie/Movie.mkv",),
    )
    upstream.files[1]["priority"] = 0
    response = client.post(
        "/internal/delete-source", headers={"X-Arr-Token": "secret"}, json=body,
    )
    assert response.status_code == 409
    assert upstream.deleted == []


@pytest.mark.parametrize("change", [
    "selected_priority", "aux_negative_priority", "aux_bool_priority", "aux_string_priority",
    "aux_name", "aux_size", "aux_zero_size", "missing_file", "extra_file", "artifact",
    "artifact_tamper", "other_owner",
])
def test_movie_auxiliary_exception_preserves_selection_and_full_manifest_readback(
    tmp_path, change,
):
    client, _, upstream, body = _setup(
        tmp_path, torrent=_movie_with_auxiliary(), selected_files=("Movie/Movie.mkv",),
    )
    if change == "selected_priority":
        upstream.files[0]["priority"] = 0
    elif change == "aux_negative_priority":
        upstream.files[1]["priority"] = -1
    elif change == "aux_bool_priority":
        upstream.files[1]["priority"] = False
    elif change == "aux_string_priority":
        upstream.files[1]["priority"] = "0"
    elif change == "aux_name":
        upstream.files[1]["name"] = "Movie/foreign.txt"
    elif change == "aux_size":
        upstream.files[1]["size"] = 18713
    elif change == "aux_zero_size":
        upstream.files[1]["size"] = 0
    elif change == "missing_file":
        upstream.files.pop()
    elif change == "extra_file":
        upstream.files.append({"name": "foreign.txt", "size": 10, "priority": 0})
    elif change == "artifact":
        with sqlite3.connect(tmp_path / "control.sqlite") as connection:
            connection.execute("DELETE FROM torrent_artifacts")
    elif change == "artifact_tamper":
        with sqlite3.connect(tmp_path / "control.sqlite") as connection:
            connection.execute("UPDATE torrent_artifacts SET content=?", (TORRENT,))
    else:
        metadata = inspect_torrent(_movie_with_auxiliary())
        other = ReservationRepository(tmp_path / "control.sqlite").reserve(
            request_id="seerr:2", source_id="2", media_key="movie:tmdb:456",
            filesystem_id="fixture", budget_bytes=metadata.total_bytes,
            free_bytes=metadata.total_bytes * 5, total_bytes=metadata.total_bytes * 6,
        )
        permits = PermitRegistry(tmp_path / "control.sqlite")
        conflict = permits.issue(
            infohash=upstream.infohash, destination="/data/torrents", category="radarr",
            reservation_id=other.reservation_id, metadata_sha256=metadata.metadata_sha256,
            selected_files=("Movie/Movie.mkv",), budget_bytes=metadata.total_bytes,
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
