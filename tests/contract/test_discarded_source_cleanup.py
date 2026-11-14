"""Discarded download trials can be removed without touching active media."""

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import homeserver_control.gateway.app as gateway_module
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.heartbeat import WorkerHeartbeatStore
from homeserver_control.persistence.source_requests import SourceReplacementRequests
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence

TORRENT = (
    b"d4:info"
    + b"d6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:pieces20:"
    + b"a" * 20
    + b"ee"
)


class PhysicalStorage:
    pool_ids = ("ssd", "hdd")

    def __init__(self, root):
        self.root = root
        self.offline = set()

    def inspect(self, pool_id, *, writable=False):
        if pool_id in self.offline:
            raise RuntimeError("pool offline")
        return SimpleNamespace(filesystem_id=f"{pool_id}-uuid", free_bytes=10_000)

    def resolve(self, pool_id, logical, *, writable=False):
        self.inspect(pool_id)
        path = self.root / pool_id / Path(logical).relative_to("/data")
        if path.is_symlink():
            raise RuntimeError("symlink in placement")
        return path

    def prepare_destination(self, pool_id, permit_id):
        destination = f"/data/torrents/.placements/{permit_id}"
        self.resolve(pool_id, destination).mkdir(parents=True)
        return destination

    def validate_destination(self, pool_id, permit_id):
        destination = f"/data/torrents/.placements/{permit_id}"
        if not self.resolve(pool_id, destination).is_dir():
            raise RuntimeError("placement missing")
        return destination


class Upstream:
    def __init__(self, permit, storage):
        self.permit = permit
        self.storage = storage
        self.current = {
            "hash": permit.infohash, "save_path": permit.destination,
            "category": permit.category, "state": "stoppedDL", "progress": 0.1,
            "amount_left": 111, "total_size": 123,
        }
        self.files = [{"name": "test.mp4", "size": 123, "priority": 1}]
        self.deleted = []
        self.running = []
        self.remove_immediately = True

    def read(self, path, params=None):
        if path == "/api/v2/torrents/info":
            assert params == {"hashes": self.permit.infohash}
            return [self.current] if self.current else []
        assert path == "/api/v2/torrents/files"
        assert params == {"hash": self.permit.infohash}
        return self.files

    def delete_torrent(self, infohash, *, delete_files):
        assert infohash == self.permit.infohash and delete_files is True
        self.deleted.append(infohash)
        if self.remove_immediately:
            source = self.storage.resolve(self.permit.pool_id, self.permit.destination) / "test.mp4"
            source.unlink(missing_ok=True)
            self.current = None

    def set_running(self, infohash, *, running):
        assert infohash == self.permit.infohash
        self.running.append(running)
        self.current["state"] = "downloading" if running else "stoppedDL"


def setup(tmp_path, *, state="probe_rejected", scope=None, protected=False):
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    reservation = repository.reserve(
        request_id="seerr:1", source_id="1",
        media_key="season:tmdb:1:1" if scope else "movie:tmdb:1",
        filesystem_id="ssd-uuid", budget_bytes=1, free_bytes=10_000, total_bytes=20_000,
    )
    storage = PhysicalStorage(tmp_path / "physical")
    registry = PermitRegistry(database, storage_registry=storage)
    metadata = inspect_torrent(TORRENT)
    capacity = CapacityEvidence(10_000, {}, pools=tuple(
        PoolCapacityEvidence(pool, f"{pool}-uuid", 10_000, {})
        for pool in storage.pool_ids
    ))
    permit = registry.issue(
        infohash=metadata.infohash, destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        category="sonarr" if scope else "radarr", scope_key=scope,
        reservation_id=reservation.reservation_id,
        metadata_sha256=metadata.metadata_sha256, selected_files=("test.mp4",),
        budget_bytes=metadata.total_bytes, capacity=capacity,
    )
    store = TorrentArtifactStore(database)
    store.put(permit, TORRENT)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE gateway_permits SET state=?", (state,))
    upstream = Upstream(permit, storage)
    source = storage.resolve(permit.pool_id, permit.destination) / "test.mp4"
    source.write_bytes(b"source")
    client = TestClient(create_app(
        permits=registry, upstream=upstream, torrent_store=store, arr_token="secret",
        protected_source_provider=lambda _: protected,
    ))
    return client, registry, upstream, permit, source


def discard(client, permit, *, headers=None):
    return client.post(
        "/internal/discard-source", headers=headers or {"X-Arr-Token": "secret"},
        json={"permit_token": permit.token},
    )


def queue_client(registry, upstream):
    return TestClient(create_app(
        permits=registry, upstream=upstream, arr_token="secret",
        torrent_store=TorrentArtifactStore(registry._db_path),
        capacity_provider=lambda: CapacityEvidence(10_000, {}, pools=tuple(
            PoolCapacityEvidence(pool, f"{pool}-uuid", 10_000, {})
            for pool in ("ssd", "hdd")
        )),
    ))


def queue_action(client, permit, action):
    return client.post(
        "/internal/series-queue-state", headers={"X-Arr-Token": "secret"},
        json={"permit_token": permit.token, "action": action},
    )


def test_controller_window_pause_survives_restart_and_cleanup_then_resumes(tmp_path):
    _, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    upstream.current["state"] = "downloading"
    client = queue_client(registry, upstream)

    assert queue_action(client, permit, "stop").json() == {"state": "stopped"}
    registry = PermitRegistry(registry._db_path, storage_registry=registry.storage_registry)
    client = queue_client(registry, upstream)
    response = discard(client, permit)
    assert response.status_code == 409
    assert response.json()["detail"] == "source paused by controller"
    assert registry.controller_pause_held(permit)
    assert registry.list_discarded_sources(stopped_infohashes=frozenset({permit.infohash})) == []
    assert source.exists() and upstream.deleted == []
    assert registry.probe_reservation_active(permit)

    assert queue_action(client, permit, "start").json() == {"state": "started"}
    assert not registry.controller_pause_held(permit)
    assert upstream.running == [False, True]
    upstream.current["state"] = "stoppedDL"
    assert discard(client, permit).json() == {"state": "deleted"}


def test_series_controller_does_not_restart_a_manual_stop_before_cleanup(tmp_path):
    _, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    client = queue_client(registry, upstream)

    assert queue_action(client, permit, "start").json() == {"state": "operator_stopped"}
    assert upstream.running == [] and source.exists()
    assert not registry.controller_pause_held(permit)
    assert discard(client, permit).json() == {"state": "deleted"}


def test_already_stopped_manual_episode_is_not_reclassified_as_controller_pause(tmp_path):
    _, registry, upstream, permit, _ = setup(tmp_path, state="confirmed", scope="S01E01")
    client = queue_client(registry, upstream)

    assert queue_action(client, permit, "stop").json() == {"state": "already_stopped"}
    assert not registry.controller_pause_held(permit)
    assert discard(client, permit).json() == {"state": "deleted"}


def test_controller_pause_is_committed_before_uncertain_native_stop(tmp_path):
    _, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    upstream.current["state"] = "downloading"
    original = upstream.set_running

    def uncertain_stop(infohash, *, running):
        assert registry.controller_pause_held(permit)
        original(infohash, running=running)
        raise RuntimeError("native response lost")

    upstream.set_running = uncertain_stop
    client = queue_client(registry, upstream)

    with pytest.raises(RuntimeError, match="response lost"):
        queue_action(client, permit, "stop")
    assert registry.controller_pause_held(permit)
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


def test_controller_hold_clears_on_already_started_readback(tmp_path):
    _, registry, upstream, permit, _ = setup(tmp_path, state="confirmed", scope="S01E01")
    upstream.current["state"] = "downloading"
    client = queue_client(registry, upstream)
    assert queue_action(client, permit, "stop").json() == {"state": "stopped"}
    assert registry.controller_pause_held(permit)
    upstream.current["state"] = "downloading"

    assert queue_action(client, permit, "start").json() == {"state": "already_started"}
    assert not registry.controller_pause_held(permit)
    assert upstream.running == [False]


def test_explicit_source_resume_clears_the_controller_pause_for_later_manual_stops(tmp_path):
    _, registry, upstream, permit, _ = setup(tmp_path, state="confirmed", scope="S01E01")
    upstream.current["state"] = "downloading"
    client = queue_client(registry, upstream)
    assert queue_action(client, permit, "stop").json() == {"state": "stopped"}

    response = client.post(
        "/internal/source-state", headers={"X-Arr-Token": "secret"},
        json={"permit_token": permit.token, "action": "start"},
    )

    assert response.json() == {"state": "started"}
    assert not registry.controller_pause_held(permit)
    upstream.current["state"] = "stoppedDL"
    assert queue_action(client, permit, "start").json() == {"state": "operator_stopped"}


def test_delayed_native_start_keeps_controller_hold_until_readback(tmp_path):
    _, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    upstream.current["state"] = "downloading"
    client = queue_client(registry, upstream)
    assert queue_action(client, permit, "stop").json() == {"state": "stopped"}
    upstream.set_running = lambda infohash, running: upstream.running.append(running)

    assert queue_action(client, permit, "start").json() == {"state": "start_pending"}
    assert registry.controller_pause_held(permit)
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


@pytest.mark.parametrize("bad_state", [None, [], "unknown"])
def test_invalid_native_start_readback_preserves_controller_hold(tmp_path, bad_state):
    _, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    upstream.current["state"] = "downloading"
    client = queue_client(registry, upstream)
    assert queue_action(client, permit, "stop").json() == {"state": "stopped"}
    upstream.set_running = lambda infohash, running: upstream.running.append(running)
    original_read = upstream.read
    reads = 0

    def invalid_readback(path, params=None):
        nonlocal reads
        result = original_read(path, params)
        if path.endswith("/info"):
            reads += 1
            if reads == 2:
                return [{**result[0], "state": bad_state}]
        return result

    upstream.read = invalid_readback
    response = queue_action(client, permit, "start")

    assert response.status_code == (200 if bad_state == "unknown" else 502)
    if response.status_code == 200:
        assert response.json() == {"state": "start_pending"}
    assert registry.controller_pause_held(permit)
    assert source.exists() and upstream.deleted == []


def test_historical_source_remains_cleanable_after_controller_pause_and_replacement(tmp_path):
    _, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    upstream.current["state"] = "downloading"
    client = queue_client(registry, upstream)
    assert queue_action(client, permit, "stop").json() == {"state": "stopped"}
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute("UPDATE gateway_permits SET state='superseded'")

    assert not registry.controller_pause_held(registry.get(permit.token))
    assert [item.permit_id for item in registry.list_discarded_sources()] == [permit.permit_id]
    assert discard(client, permit).json() == {"state": "deleted"}
    assert not source.exists() and upstream.deleted == [permit.infohash]


def test_controller_cannot_hold_a_pending_episode_cleanup(tmp_path):
    _, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    client = queue_client(registry, upstream)
    upstream.remove_immediately = False
    assert discard(client, permit).status_code == 503

    with pytest.raises(PermissionError):
        registry.mark_controller_pause(permit)
    assert queue_action(client, permit, "stop").json() == {"state": "already_stopped"}
    assert not registry.controller_pause_held(permit)
    assert registry.source_cleanup_intended(permit)
    upstream.remove_immediately = True
    assert discard(client, permit).json() == {"state": "deleted"}
    assert not source.exists()


@pytest.mark.parametrize("native_state", ["stoppedDL", "pausedDL"])
def test_current_stopped_movie_cleanup_cancels_only_its_generation(tmp_path, native_state):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed")
    upstream.current["state"] = native_state
    library = tmp_path / "library.mp4"
    os.link(source, library)

    response = discard(client, permit)

    assert response.status_code == 200, response.json()
    assert response.json() == {"state": "deleted"}
    assert not source.exists() and library.read_bytes() == b"source"
    assert upstream.deleted == [permit.infohash]
    assert registry.get(permit.token).state == "revoked"
    assert registry.source_cleanup_complete(permit)
    assert not registry.probe_reservation_active(permit)
    with sqlite3.connect(registry._db_path) as connection:
        assert connection.execute("SELECT state FROM requests").fetchone() == ("cancelled",)
        assert connection.execute("SELECT state,budget_bytes FROM reservations").fetchone() == (
            "cancelled", 0,
        )
        assert connection.execute("SELECT COUNT(*) FROM tombstones").fetchone() == (0,)
    assert discard(client, permit).json() == {"state": "missing"}
    with pytest.raises(PermissionError):
        replacement_of(registry, permit)


def test_current_stopped_episode_cleanup_preserves_the_other_episode_and_season_request(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    metadata = inspect_torrent(TORRENT.replace(b"test.mp4", b"next.mp4"))
    capacity = CapacityEvidence(10_000, {}, pools=tuple(
        PoolCapacityEvidence(pool, f"{pool}-uuid", 10_000, {})
        for pool in ("ssd", "hdd")
    ))
    other = registry.issue(
        infohash=metadata.infohash, destination="/data/torrents", category="sonarr",
        reservation_id=permit.reservation_id, scope_key="S01E02",
        metadata_sha256=metadata.metadata_sha256, selected_files=("next.mp4",),
        budget_bytes=metadata.total_bytes, capacity=capacity,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute("UPDATE gateway_permits SET state='confirmed'")
        before = connection.execute("SELECT state FROM requests").fetchone()

    response = discard(client, permit)

    assert response.status_code == 200, response.json()
    assert not source.exists() and upstream.deleted == [permit.infohash]
    assert registry.get(permit.token).state == "revoked"
    assert registry.get(other.token).state == "confirmed"
    assert not registry.probe_reservation_active(permit)
    assert registry.probe_reservation_active(other)
    with sqlite3.connect(registry._db_path) as connection:
        assert connection.execute("SELECT state FROM requests").fetchone() == before
        assert connection.execute("SELECT state,budget_bytes FROM reservations").fetchone() == (
            "reserved", 123,
        )
        assert connection.execute("SELECT media_key FROM tombstones").fetchall() == [
            ("episode:tmdb:1:S01E01",),
        ]


@pytest.mark.parametrize("scope", [None, "S01E01"])
def test_current_cleanup_intent_survives_async_native_unlink_and_blocks_restart(tmp_path, scope):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope=scope)

    def delayed_unlink(infohash, *, delete_files):
        upstream.deleted.append(infohash)
        upstream.current = None

    upstream.delete_torrent = delayed_unlink

    assert discard(client, permit).status_code == 503
    assert source.exists() and not registry.source_cleanup_complete(permit)
    assert registry.source_cleanup_intended(permit)
    assert not registry.probe_reservation_active(permit)
    assert [item.permit_id for item in registry.list_discarded_sources(
        stopped_infohashes=frozenset(),
    )] == [permit.permit_id]
    assert discard(client, permit).status_code == 409
    source.unlink()
    assert discard(client, permit).json() == {"state": "missing"}
    assert registry.source_cleanup_complete(permit)
    assert registry.get(permit.token).state == "revoked"


def test_current_missing_source_without_cleanup_intent_is_not_cancelled(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed")
    upstream.current = None
    source.unlink()

    assert discard(client, permit).status_code == 409
    assert registry.probe_reservation_active(permit)
    assert not registry.source_cleanup_complete(permit)
    assert not registry.source_cleanup_intended(permit)
    assert registry.list_discarded_sources(stopped_infohashes=frozenset()) == []


def test_current_native_effect_requires_a_committed_cleanup_intent(tmp_path):
    _, registry, _, permit, source = setup(tmp_path, state="confirmed")
    effects = []

    with pytest.raises(ValueError, match="cleanup intent required"):
        registry.discard_source(
            permit.token, lambda _: effects.append(True) or {"state": "deleted"},
        )

    assert effects == [] and source.exists()
    assert registry.probe_reservation_active(permit)


@pytest.mark.parametrize("field", ["infohash", "metadata_sha256", "pool_id", "filesystem_id",
                                   "destination"])
def test_current_cleanup_intent_never_applies_to_a_changed_identity(tmp_path, field):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed")
    upstream.remove_immediately = False
    assert discard(client, permit).status_code == 503
    assert source.exists() and registry.source_cleanup_intended(permit)

    setattr(permit, field, "changed")

    assert not registry.source_cleanup_intended(permit)
    assert not registry.source_cleanup_complete(permit)


@pytest.mark.parametrize("state", ["queued", "running"])
def test_current_manual_source_request_prevents_stopped_cleanup(tmp_path, state):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed")
    requests = SourceReplacementRequests(registry._db_path)
    request = requests.enqueue(requests.source(permit.infohash))
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute("UPDATE source_replacement_requests SET state=?", (state,))

    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []
    assert requests.latest(permit.permit_id)["request_id"] == request["request_id"]
    assert registry.probe_reservation_active(permit)


@pytest.mark.parametrize("state", ["dispatching", "accepted", "subtitles_pending", "complete"])
def test_current_movie_import_in_any_stage_prevents_stopped_cleanup(tmp_path, state):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed")
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute(
            "INSERT INTO movie_imports(reservation_id,state,updated_at) VALUES (?,?,'now')",
            (permit.reservation_id, state),
        )

    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []
    assert registry.probe_reservation_active(permit)


@pytest.mark.parametrize("change", [
    {"state": "stalledDL"}, {"state": "queuedDL"}, {"state": "downloading"},
    {"state": "stoppedUP"}, {"state": "pausedUP"}, {"state": "uploading"},
    {"state": "queuedUP"}, {"state": "stalledUP"},
    {"progress": 1, "amount_left": 0},
])
def test_current_cleanup_preserves_completed_queued_stalled_and_running_torrents(tmp_path, change):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed")
    upstream.current.update(change)

    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []
    assert registry.probe_reservation_active(permit)


def test_current_season_pack_is_deferred_without_cancelling_the_season(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01PACK")

    response = discard(client, permit)

    assert response.status_code == 409
    assert response.json()["detail"] == "season pack cleanup requires review"
    assert source.exists() and upstream.deleted == []
    assert registry.probe_reservation_active(permit)


def test_current_source_resumed_before_native_effect_is_preserved(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed")
    original_read = upstream.read
    reads = 0

    def read(path, params=None):
        nonlocal reads
        if path.endswith("/info"):
            reads += 1
            if reads == 4:
                upstream.current["state"] = "downloading"
        return original_read(path, params)

    upstream.read = read

    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []
    assert registry.source_cleanup_intended(permit)
    assert not registry.probe_reservation_active(permit)


def test_cancelled_episode_cleanup_intent_blocks_series_queue_restart(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path, state="confirmed", scope="S01E01")
    upstream.remove_immediately = False
    assert discard(client, permit).status_code == 503

    response = client.post(
        "/internal/series-queue-state", headers={"X-Arr-Token": "secret"},
        json={"permit_token": permit.token, "action": "start"},
    )

    assert response.status_code == 409
    assert source.exists() and upstream.deleted == [permit.infohash]
    assert not registry.probe_reservation_active(permit)


@pytest.mark.parametrize("state", ["probe_rejected", "superseded"])
def test_discards_only_historical_partial_source_and_retries_idempotently(tmp_path, state):
    client, registry, upstream, permit, source = setup(tmp_path, state=state)
    assert discard(client, permit).json() == {"state": "deleted"}
    assert not source.exists()
    assert upstream.deleted == [permit.infohash]
    assert registry.get(permit.token).state == state
    assert discard(client, permit).json() == {"state": "missing"}
    assert upstream.deleted == [permit.infohash]


@pytest.mark.parametrize("permit_state", ["authorized", "dispatching", "unknown", "revoked"])
def test_preserves_every_source_that_is_not_explicitly_discarded(tmp_path, permit_state):
    client, _, upstream, permit, source = setup(tmp_path, state=permit_state)
    assert discard(client, permit).status_code == 403
    assert source.exists() and upstream.deleted == []


@pytest.mark.parametrize("change", [
    {"state": "downloading"}, {"state": "stalledDL"}, {"state": "stoppedUP"},
    {"state": "uploading"}, {"progress": 1}, {"amount_left": 0},
    {"category": "other"}, {"save_path": "/data/media/movies"},
    {"total_size": 124}, {"progress": True}, {"amount_left": -1},
])
def test_preserves_running_completed_or_changed_torrent(tmp_path, change):
    client, _, upstream, permit, source = setup(tmp_path)
    upstream.current.update(change)
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


def test_preserves_published_library_hardlink_when_source_is_removed(tmp_path):
    client, _, upstream, permit, source = setup(tmp_path)
    library = tmp_path / "library.mp4"
    os.link(source, library)
    assert discard(client, permit).status_code == 200
    assert library.read_bytes() == b"source"
    assert not source.exists() and upstream.deleted == [permit.infohash]


def test_production_health_guard_reads_heartbeat_inside_cleanup_write_transaction(
    tmp_path, monkeypatch,
):
    _, registry, upstream, permit, source = setup(tmp_path)
    heartbeat = WorkerHeartbeatStore(registry._db_path)
    heartbeat.write(now=100, initialized=True, state="running", success=True)
    monkeypatch.setattr(gateway_module, "_database_path", registry._db_path)
    monkeypatch.setattr(gateway_module, "_production_heartbeat", heartbeat, raising=False)
    monkeypatch.setattr(gateway_module.time, "time", lambda: 100)
    client = TestClient(create_app(
        permits=registry, upstream=upstream,
        torrent_store=TorrentArtifactStore(registry._db_path), arr_token="secret",
        worker_health_provider=gateway_module._configured_worker_health,
    ))

    response = discard(client, permit)

    assert response.status_code == 200, response.json()
    assert response.json() == {"state": "deleted"}
    assert not source.exists() and upstream.deleted == [permit.infohash]
    assert registry.source_cleanup_complete(permit)


def test_production_health_guard_fails_closed_without_initialized_store(tmp_path, monkeypatch):
    database = tmp_path / "control.sqlite"
    heartbeat = WorkerHeartbeatStore(database)
    heartbeat.write(now=100, initialized=True, state="running", success=True)
    monkeypatch.setattr(gateway_module, "_database_path", str(database))
    monkeypatch.setattr(gateway_module, "_production_heartbeat", None, raising=False)
    monkeypatch.setattr(gateway_module.time, "time", lambda: 100)

    assert gateway_module._configured_worker_health() is False


def test_refuses_protected_source_and_missing_authentication(tmp_path):
    client, _, upstream, permit, source = setup(tmp_path, protected=True)
    assert discard(client, permit).status_code == 403
    assert discard(client, permit, headers={"X-Arr-Token": "wrong"}).status_code == 403
    assert source.exists() and upstream.deleted == []


def test_refuses_offline_pool_and_shared_legacy_destination(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path)
    registry.storage_registry.offline.add("ssd")
    assert discard(client, permit).status_code == 409
    registry.storage_registry.offline.clear()
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute("UPDATE gateway_permits SET destination='/data/torrents'")
    upstream.current["save_path"] = "/data/torrents"
    assert discard(client, permit).status_code == 403
    assert source.exists() and upstream.deleted == []


@pytest.mark.parametrize("changed", [
    [{"name": "other.mp4", "size": 123, "priority": 1}],
    [{"name": "test.mp4", "size": 124, "priority": 1}],
    [{"name": "test.mp4", "size": 123, "priority": 1},
     {"name": "extra.mp4", "size": 40, "priority": 1}],
])
def test_refuses_torrent_manifest_changes(tmp_path, changed):
    client, _, upstream, permit, source = setup(tmp_path)
    upstream.files = changed
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


@pytest.mark.parametrize("state", ["probe_rejected", "confirmed"])
def test_refuses_alias_owned_by_active_permit(tmp_path, state):
    client, registry, upstream, permit, source = setup(tmp_path, state=state)
    alias = ReservationRepository(registry._db_path).reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:2",
        filesystem_id="ssd-uuid", budget_bytes=1, free_bytes=10_000, total_bytes=20_000,
    )
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute(
            "INSERT INTO gateway_permits SELECT 'alias','alias-token','alias-op',"
            "?,scope_key,infohash,metadata_sha256,destination,category,"
            "selected_files_json,budget_bytes,reported_seeders,expires_at,'confirmed',"
            "result_json,probe_parent_id,quality_rank_json,pool_id,filesystem_id "
            "FROM gateway_permits WHERE token=?", (alias.reservation_id, permit.token),
        )
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


def test_refuses_pending_handover_or_started_episode_import(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path, scope="S01E01")
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute(
            "INSERT INTO source_handovers(candidate_id,parent_id) VALUES ('new',?)",
            (permit.permit_id,),
        )
    assert discard(client, permit).status_code == 409
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute("DELETE FROM source_handovers")
        connection.execute(
            "INSERT INTO episode_imports(permit_id,state,updated_at) VALUES (?,'importing','now')",
            (permit.permit_id,),
        )
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


def test_retries_uncertain_deletion_without_marking_source_as_cleaned(tmp_path):
    client, _, upstream, permit, source = setup(tmp_path)
    upstream.remove_immediately = False
    assert discard(client, permit).status_code == 503
    assert source.exists()
    upstream.remove_immediately = True
    assert discard(client, permit).json() == {"state": "deleted"}


def test_rechecks_database_ownership_immediately_before_mutation(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path)
    original_read = upstream.read

    def read(path, params=None):
        result = original_read(path, params)
        if path.endswith("/files"):
            with sqlite3.connect(registry._db_path) as connection:
                connection.execute("UPDATE gateway_permits SET state='confirmed'")
        return result

    upstream.read = read
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


def test_candidate_listing_includes_current_unimported_sources_and_rotates(tmp_path):
    _, registry, _, permit, _ = setup(tmp_path)
    candidates = registry.list_discarded_sources(limit=10)
    assert [item.permit_id for item in candidates] == [permit.permit_id]
    assert registry.list_discarded_sources(limit=10, after_id=permit.permit_id) == []
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute("UPDATE gateway_permits SET state='confirmed'")
    assert [item.permit_id for item in registry.list_discarded_sources(limit=10)] == [
        permit.permit_id,
    ]
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute(
            "INSERT INTO movie_imports(reservation_id,state,updated_at) "
            "VALUES (?,'complete','now')",
            (permit.reservation_id,),
        )
    assert registry.list_discarded_sources(limit=10) == []


def test_missing_metadata_prevents_cleanup(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path)
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute("DELETE FROM torrent_artifacts")
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


@pytest.mark.parametrize("guard", ["recovery", "worker"])
def test_operator_recovery_and_worker_guards_prevent_cleanup(tmp_path, guard):
    _, registry, upstream, permit, source = setup(tmp_path)
    marker = tmp_path / "RECOVERY_MODE"
    if guard == "recovery":
        marker.write_text("admission_enabled=false")
    client = TestClient(create_app(
        permits=registry, upstream=upstream, arr_token="secret",
        torrent_store=TorrentArtifactStore(registry._db_path),
        recovery_mode_path=marker,
        worker_health_provider=lambda: guard != "worker",
    ))
    assert discard(client, permit).status_code == 503
    assert source.exists() and upstream.deleted == []


@pytest.mark.parametrize("state", ["probe_rejected", "confirmed"])
def test_physical_symlink_and_directory_are_never_deleted(tmp_path, state):
    client, _, upstream, permit, source = setup(tmp_path, state=state)
    source.unlink()
    source.mkdir()
    assert discard(client, permit).status_code == 409
    assert source.is_dir() and upstream.deleted == []
    source.rmdir()
    other = tmp_path / "other.mp4"
    other.write_bytes(b"keep")
    source.symlink_to(other)
    assert discard(client, permit).status_code == 409
    assert source.is_symlink() and other.read_bytes() == b"keep"
    assert upstream.deleted == []


def test_started_pack_episode_import_prevents_whole_pack_discard(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path, scope="S01PACK")
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute(
            "INSERT INTO episode_imports(permit_id,state,updated_at) VALUES (?,'complete','now')",
            (f"{permit.permit_id}:S01E01",),
        )
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


def test_library_import_for_replacement_does_not_preserve_rejected_trials(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path)
    with sqlite3.connect(registry._db_path) as connection:
        connection.execute(
            "INSERT INTO movie_imports(reservation_id,state,updated_at) "
            "VALUES (?,'complete','now')", (permit.reservation_id,),
        )
    assert discard(client, permit).json() == {"state": "deleted"}
    assert not source.exists() and upstream.deleted == [permit.infohash]


def test_owner_change_during_final_native_read_prevents_deletion(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path)
    original_read = upstream.read
    reads = 0

    def read(path, params=None):
        nonlocal reads
        result = original_read(path, params)
        if path.endswith("/info"):
            reads += 1
            if reads == 2:
                with sqlite3.connect(registry._db_path) as connection:
                    connection.execute("UPDATE gateway_permits SET state='confirmed'")
        return result

    upstream.read = read
    assert discard(client, permit).status_code == 409
    assert source.exists() and upstream.deleted == []


def test_native_absence_does_not_confirm_cleanup_while_physical_payload_remains(tmp_path):
    client, registry, upstream, permit, source = setup(tmp_path)

    def delayed_unlink(infohash, *, delete_files):
        upstream.deleted.append(infohash)
        upstream.current = None

    upstream.delete_torrent = delayed_unlink
    assert discard(client, permit).status_code == 503
    assert source.exists()
    assert discard(client, permit).status_code == 409
    source.unlink()
    assert discard(client, permit).json() == {"state": "missing"}
    assert registry.source_cleanup_complete(permit)


def test_removed_history_does_not_block_a_later_verified_replacement(tmp_path):
    client, registry, upstream, old, source = setup(tmp_path, state="confirmed")
    capacity = CapacityEvidence(10_000, {}, pools=tuple(
        PoolCapacityEvidence(pool, f"{pool}-uuid", 10_000,
                             {old.infohash: 111} if pool == "ssd" else {},
                             paused_hashes=frozenset({old.infohash}))
        for pool in ("ssd", "hdd")
    ))
    new_metadata = inspect_torrent(TORRENT.replace(b"test.mp4", b"new_.mp4"))
    current = registry.replace_confirmed(
        old.token, infohash=new_metadata.infohash,
        metadata_sha256=new_metadata.metadata_sha256, selected_files=("new_.mp4",),
        budget_bytes=123, capacity=capacity, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    registry.authorize(
        token=current.token, infohash=current.infohash, destination=current.destination,
        metadata_sha256=current.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    assert discard(client, old).json() == {"state": "deleted"}
    assert not source.exists()
    next_capacity = CapacityEvidence(10_000, {}, pools=tuple(
        PoolCapacityEvidence(pool, f"{pool}-uuid", 10_000,
                             {current.infohash: 123} if pool == "ssd" else {},
                             paused_hashes=frozenset({current.infohash}))
        for pool in ("ssd", "hdd")
    ))
    next_metadata = inspect_torrent(TORRENT.replace(b"test.mp4", b"next.mp4"))
    replacement = registry.replace_confirmed(
        current.token, infohash=next_metadata.infohash,
        metadata_sha256=next_metadata.metadata_sha256, selected_files=("next.mp4",),
        budget_bytes=123, capacity=next_capacity,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert replacement.infohash == next_metadata.infohash
    assert registry.get(old.token).state == "superseded"


@pytest.mark.parametrize("state", ["probe_rejected", "confirmed"])
def test_database_owner_is_locked_through_the_native_delete_effect(tmp_path, state):
    client, registry, upstream, permit, source = setup(tmp_path, state=state)
    original_delete = upstream.delete_torrent

    def delete(infohash, *, delete_files):
        with sqlite3.connect(registry._db_path, timeout=0) as connection:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                connection.execute("UPDATE gateway_permits SET state='confirmed'")
        original_delete(infohash, delete_files=delete_files)

    upstream.delete_torrent = delete
    assert discard(client, permit).json() == {"state": "deleted"}
    assert not source.exists() and registry.source_cleanup_complete(permit)


@pytest.mark.parametrize("field", ["infohash", "metadata_sha256", "pool_id", "filesystem_id",
                                   "destination"])
def test_cleanup_proof_never_applies_to_a_changed_identity(tmp_path, field):
    client, registry, _, permit, _ = setup(tmp_path)
    assert discard(client, permit).json() == {"state": "deleted"}
    assert registry.source_cleanup_complete(permit)
    setattr(permit, field, "changed")
    assert not registry.source_cleanup_complete(permit)


def replacement_of(registry, permit):
    capacity = CapacityEvidence(10_000, {}, pools=tuple(
        PoolCapacityEvidence(pool, f"{pool}-uuid", 10_000,
                             {permit.infohash: 111} if pool == "ssd" else {},
                             paused_hashes=frozenset({permit.infohash}))
        for pool in ("ssd", "hdd")
    ))
    metadata = inspect_torrent(TORRENT.replace(b"test.mp4", b"new_.mp4"))
    return registry.replace_confirmed(
        permit.token, infohash=metadata.infohash, metadata_sha256=metadata.metadata_sha256,
        selected_files=("new_.mp4",), budget_bytes=123, capacity=capacity,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.mark.parametrize("cancel", ["request", "tombstone"])
def test_cancelled_media_never_authorizes_a_replacement(tmp_path, cancel):
    _, registry, _, permit, _ = setup(tmp_path, state="confirmed")
    with sqlite3.connect(registry._db_path) as connection:
        if cancel == "request":
            connection.execute("UPDATE requests SET state='cancel_requested'")
        else:
            connection.execute(
                "INSERT INTO tombstones VALUES ('movie:tmdb:1','now','fixture')",
            )
    with pytest.raises(PermissionError):
        replacement_of(registry, permit)
    assert registry.get(permit.token).state == "confirmed"


@pytest.mark.parametrize("cancel", ["request", "tombstone"])
def test_cancellation_after_replacement_admission_blocks_native_add(tmp_path, cancel):
    _, registry, _, permit, _ = setup(tmp_path, state="confirmed")
    replacement = replacement_of(registry, permit)
    with sqlite3.connect(registry._db_path) as connection:
        if cancel == "request":
            connection.execute("UPDATE requests SET state='cancel_requested'")
        else:
            connection.execute(
                "INSERT INTO tombstones VALUES ('movie:tmdb:1','now','fixture')",
            )
    effects = []
    with pytest.raises(PermissionError):
        registry.authorize(
            token=replacement.token, infohash=replacement.infohash,
            destination=replacement.destination, metadata_sha256=replacement.metadata_sha256,
            effect=lambda _: effects.append(True) or {"accepted": True},
        )
    assert effects == []
