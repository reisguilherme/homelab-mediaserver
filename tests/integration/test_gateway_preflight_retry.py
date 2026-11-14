"""Preflight failures must not claim that a qBit write might have happened."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Lock

import pytest
from fastapi.testclient import TestClient

from homeserver_common.storage import StorageUnavailable
from homeserver_control.adapters.http import EffectUncertain
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence


class RecordingUpstream:
    def __init__(self, *, uncertain=False):
        self.added = []
        self.uncertain = uncertain
        self.started = Event()
        self.finish = Event()
        self.finish.set()
        self.lock = Lock()

    def add_torrent(self, payload):
        assert payload["category"] == "sonarr"
        assert payload["savepath"] == "/data/torrents"
        assert isinstance(payload["torrent_bytes"], bytes)
        with self.lock:
            self.added.append(payload)
        self.started.set()
        assert self.finish.wait(timeout=5)
        if self.uncertain:
            raise EffectUncertain("qBit accepted but response was lost")
        return {"accepted": True, "infohash": payload["infohash"]}


def admission(tmp_path, scope, *, uncertain=False):
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    reservation = repository.reserve(
        request_id="request-fixture", source_id="fixture", media_key="season:tmdb:101:1",
        filesystem_id="fixture-uuid", budget_bytes=123,
        free_bytes=100_000_000_000, total_bytes=200_000_000_000,
    )
    assert reservation.accepted
    permits = PermitRegistry(database)
    name = b"Fixture.S01E01.mkv"
    torrent = (
        b"d4:infod6:lengthi123e4:name" + str(len(name)).encode() + b":" + name
        + b"12:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"ee"
    )
    inspected = inspect_torrent(torrent)
    permit = permits.issue(
        infohash=inspected.infohash, metadata_sha256=inspected.metadata_sha256,
        destination="/data/torrents", category="sonarr",
        reservation_id=reservation.reservation_id, scope_key=scope,
        selected_files=("Fixture.S01E01.mkv",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    if scope == "S01PACK":
        permits.bind_season_pack(
            permit.token, episode_files={"S01E01": ("Fixture.S01E01.mkv",)},
            episode_catalog=[{"seasonNumber": 1, "episodeNumber": 1}],
        )
    store = TorrentArtifactStore(database)
    store.put(permit, torrent)
    upstream = RecordingUpstream(uncertain=uncertain)
    evidence = {"error": None, "protected": False}

    def capacity():
        if evidence["error"] is not None:
            raise evidence["error"]
        return CapacityEvidence(free_bytes=10_000, remaining_by_hash={})

    client = TestClient(create_app(
        permits=permits, upstream=upstream, arr_token="fixture-token",
        torrent_store=store, capacity_provider=capacity,
        protected_source_provider=lambda _: evidence["protected"],
    ), raise_server_exceptions=False)

    def post():
        return client.post(
            "/api/v2/torrents/add", headers={
                "X-Arr-Token": "fixture-token", "X-Admission-Permit": permit.token,
            }, data={"category": "sonarr", "savepath": "/data/torrents", "stopped": "false"},
            files={"torrents": ("fixture.torrent", torrent, "application/x-bittorrent")},
        )

    return permits, permit, upstream, evidence, post


@pytest.mark.parametrize("scope", ["S01E01", "S01PACK"])
@pytest.mark.parametrize("failure", ["snapshot", "mounts", "space"])
def test_capacity_rejection_before_qbit_write_can_retry_same_permit(tmp_path, scope, failure):
    permits, permit, upstream, evidence, post = admission(tmp_path, scope)
    evidence["error"] = {
        "snapshot": ValueError("filesystem_snapshot_unavailable"),
        "mounts": StorageUnavailable("mount table changed during read snapshot"),
        "space": PermissionError("waiting_space"),
    }[failure]

    assert post().status_code >= 400
    assert upstream.added == []
    assert permits.get(permit.token).state == "authorized"

    evidence["error"] = None
    assert post().status_code == 200
    assert permits.get(permit.token).state == "confirmed"
    assert len(upstream.added) == 1
    assert post().status_code == 200
    assert len(upstream.added) == 1


def test_protection_change_inside_admission_cannot_make_unwritten_source_unknown(
    tmp_path, monkeypatch,
):
    permits, permit, upstream, evidence, post = admission(tmp_path, "S01E01")
    authorize = permits.authorize

    def protected_before_authorization(**kwargs):
        evidence["protected"] = True
        return authorize(**kwargs)

    monkeypatch.setattr(permits, "authorize", protected_before_authorization)
    assert post().status_code == 403
    assert upstream.added == []
    assert permits.get(permit.token).state == "authorized"


def test_placement_change_inside_admission_cannot_make_unwritten_source_unknown(
    tmp_path, monkeypatch,
):
    permits, permit, upstream, _evidence, post = admission(tmp_path, "S01E01")
    authorize = permits.authorize
    placement = {"valid": True}
    permits.storage_registry = object()
    monkeypatch.setattr(permits, "placement_valid", lambda _: placement["valid"])

    def disconnected_before_authorization(**kwargs):
        placement["valid"] = False
        return authorize(**kwargs)

    monkeypatch.setattr(permits, "authorize", disconnected_before_authorization)
    assert post().status_code == 403
    assert upstream.added == []
    assert permits.get(permit.token).state == "authorized"


@pytest.mark.parametrize("scope", ["S01E01", "S01PACK"])
def test_concurrent_retry_after_preflight_recovery_still_writes_once(tmp_path, scope):
    permits, permit, upstream, evidence, post = admission(tmp_path, scope)
    evidence["error"] = ValueError("filesystem_snapshot_unavailable")
    assert post().status_code >= 400
    assert permits.get(permit.token).state == "authorized"

    evidence["error"] = None
    upstream.finish.clear()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(post)
        assert upstream.started.wait(timeout=5)
        second = pool.submit(post)
        upstream.finish.set()
        statuses = [first.result(timeout=10).status_code, second.result(timeout=10).status_code]
    assert 200 in statuses and set(statuses) <= {200, 403}
    assert permits.get(permit.token).state == "confirmed"
    assert len(upstream.added) == 1


@pytest.mark.parametrize("scope", ["S01E01", "S01PACK"])
def test_error_after_qbit_write_stays_unknown_and_never_retries_blindly(tmp_path, scope):
    permits, permit, upstream, _evidence, post = admission(tmp_path, scope, uncertain=True)
    assert post().status_code == 500
    assert permits.get(permit.token).state == "unknown"
    assert len(upstream.added) == 1
    assert post().status_code == 403
    assert permits.get(permit.token).state == "unknown"
    assert len(upstream.added) == 1
