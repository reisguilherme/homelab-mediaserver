"""Protected source exceptions remain intact at the authenticated gateway boundary."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.source_health import SourceHealthStore, TorrentHealth

HEADERS = {"X-Arr-Token": "fixture-worker"}


class TorrentServer:
    def __init__(self, source):
        self.entries = {}
        self.add_entry(source, state="stoppedDL")

    def add_entry(self, permit, *, state="downloading", left=8000):
        self.entries[permit.infohash] = {
            "hash": permit.infohash,
            "category": permit.category,
            "save_path": permit.destination,
            "state": state,
            "progress": 0.2,
            "downloaded": 2000,
            "amount_left": left,
            "num_seeds": 5,
            "dlspeed": 100,
            "force_start": False,
            "priority": 1,
        }

    def read(self, path, params=None):
        if path == "/api/v2/app/preferences":
            return {"queueing_enabled": True}
        assert path == "/api/v2/torrents/info"
        entries = list(self.entries.values())
        if params:
            entries = [entry for entry in entries if entry["hash"] == params["hashes"]]
        return [dict(entry) for entry in entries]

    def set_running(self, infohash, *, running):
        self.entries[infohash]["state"] = "downloading" if running else "stoppedDL"

    def add_torrent(self, payload):
        self.entries[payload["infohash"]] = {"state": "downloading"}
        return {"accepted": True, "infohash": payload["infohash"]}

    def top_priority(self, infohash):
        ordered = sorted(self.entries, key=lambda item: self.entries[item]["priority"])
        ordered.remove(infohash)
        ordered.insert(0, infohash)
        for priority, identifier in enumerate(ordered, start=1):
            self.entries[identifier]["priority"] = priority


def _fixture(tmp_path, *, protect=True):
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    reserved = repository.reserve(
        request_id="seerr:fixture:2",
        source_id="fixture:2",
        media_key="season:tmdb:97546:2",
        filesystem_id="fixture",
        budget_bytes=10000,
        free_bytes=100000,
        total_bytes=200000,
    )
    registry = PermitRegistry(database)
    source = registry.issue(
        infohash="a" * 40,
        metadata_sha256="1" * 64,
        destination="/data/torrents",
        category="sonarr",
        reservation_id=reserved.reservation_id,
        scope_key="S02E04",
        selected_files=("old/Fixture.S02E04.mkv",),
        budget_bytes=10000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    _confirm(registry, source)
    registry.set_quality(source.token, (1, 1080))
    health = SourceHealthStore(database)
    if protect:
        health.protect_source(source.infohash)
    return registry, source, health, TorrentServer(source)


def _confirm(registry, permit):
    registry.authorize(
        token=permit.token,
        infohash=permit.infohash,
        destination=permit.destination,
        metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True, "infohash": permit.infohash},
    )


def _client(registry, upstream, provider, *, torrent_store=None):
    return TestClient(
        create_app(
            permits=registry,
            upstream=upstream,
            arr_token="fixture-worker",
            capacity_provider=lambda: CapacityEvidence(100000, {}),
            protected_source_provider=provider,
            torrent_store=torrent_store,
        )
    )


@pytest.mark.parametrize("endpoint", ["source-state", "series-queue-state"])
@pytest.mark.parametrize("action,state", [("start", "stoppedDL"), ("stop", "downloading")])
def test_protected_episode_cannot_be_started_or_stopped(tmp_path, endpoint, action, state):
    registry, source, health, upstream = _fixture(tmp_path)
    upstream.entries[source.infohash]["state"] = state
    before = dict(upstream.entries[source.infohash])
    with _client(registry, upstream, health.is_protected) as client:
        response = client.post(
            "/internal/" + endpoint,
            headers=HEADERS,
            json={"permit_token": source.token, "action": action},
        )
    assert response.status_code == 403
    assert upstream.entries[source.infohash] == before
    assert registry.get(source.token).state == "confirmed"


@pytest.mark.parametrize("endpoint", ["source-state", "series-queue-state"])
def test_unprotected_episode_can_still_start(tmp_path, endpoint):
    registry, source, health, upstream = _fixture(tmp_path, protect=False)
    with _client(registry, upstream, health.is_protected) as client:
        response = client.post(
            "/internal/" + endpoint,
            headers=HEADERS,
            json={"permit_token": source.token, "action": "start"},
        )
    assert response.status_code == 200
    assert upstream.entries[source.infohash]["state"] == "downloading"


def test_protection_lookup_failure_blocks_without_exposing_exception(tmp_path):
    registry, source, _, upstream = _fixture(tmp_path)

    def unavailable(_infohash):
        raise RuntimeError("private-token=fixture-sensitive-value")

    with _client(registry, upstream, unavailable) as client:
        response = client.post(
            "/internal/source-state",
            headers=HEADERS,
            json={"permit_token": source.token, "action": "start"},
        )
    assert response.status_code == 503
    assert "fixture-sensitive-value" not in response.text
    assert upstream.entries[source.infohash]["state"] == "stoppedDL"


def _probe(registry, source, health, upstream):
    candidate = registry.issue_probe(
        source.token,
        infohash="b" * 40,
        metadata_sha256="2" * 64,
        selected_files=("new/Fixture.S02E04.mkv",),
        quality_rank=(1, 1080),
        budget_bytes=20000,
        capacity=CapacityEvidence(100000, {source.infohash: 8000}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    _confirm(registry, candidate)
    upstream.entries[source.infohash]["state"] = "downloading"
    upstream.add_entry(candidate, left=2000)
    now = time.time()

    def observation(permit, left):
        return TorrentHealth(permit.infohash, 30000 - left, left, 5, 100, "downloading", 0.2)

    health.probe_decision(
        source.permit_id,
        candidate.permit_id,
        observation(source, 8000),
        observation(candidate, 20000),
        now=now - 60,
    )
    assert health.probe_decision(
        source.permit_id,
        candidate.permit_id,
        observation(source, 7400),
        observation(candidate, 2000),
        now=now,
    ) == "promote"
    return candidate


@pytest.mark.parametrize("pending_handover", [False, True])
@pytest.mark.parametrize("protected", ["parent", "candidate"])
def test_protected_source_cannot_promote_or_retry_handover(tmp_path, pending_handover, protected):
    registry, source, health, upstream = _fixture(tmp_path, protect=False)
    candidate = _probe(registry, source, health, upstream)
    if pending_handover:
        registry.promote_probe(candidate.token)
    health.protect_source(source.infohash if protected == "parent" else candidate.infohash)
    before = {key: dict(value) for key, value in upstream.entries.items()}
    current_id = registry.get_for_reservation(source.reservation_id, scope_key="S02E04").permit_id
    with _client(registry, upstream, health.is_protected) as client:
        response = client.post(
            "/internal/probe-decision",
            headers=HEADERS,
            json={"permit_token": candidate.token, "decision": "promote"},
        )
    assert response.status_code == 403
    assert upstream.entries == before
    assert registry.get_for_reservation(
        source.reservation_id, scope_key="S02E04"
    ).permit_id == current_id
    assert (registry.pending_handover(candidate.token) is not None) == pending_handover


def test_protected_source_does_not_prevent_read_only_health(tmp_path):
    registry, source, health, upstream = _fixture(tmp_path)
    with _client(registry, upstream, health.is_protected) as client:
        response = client.get(
            "/internal/torrent-health",
            headers=HEADERS | {"X-Admission-Permit": source.token},
        )
    assert response.status_code == 200
    assert response.json()["hash"] == source.infohash
    assert response.json()["state"] == "stoppedDL"


def test_protected_source_cannot_be_readded(tmp_path):
    registry, source, health, upstream = _fixture(tmp_path)
    before = dict(upstream.entries[source.infohash])
    with _client(registry, upstream, health.is_protected) as client:
        response = client.post(
            "/api/v2/torrents/add",
            headers=HEADERS | {"X-Admission-Permit": source.token},
            json={
                "infohash": source.infohash,
                "metadata_sha256": source.metadata_sha256,
                "savepath": source.destination,
                "category": source.category,
            },
        )
    assert response.status_code == 403
    assert upstream.entries[source.infohash] == before


def test_probe_add_cannot_bypass_protected_parent(tmp_path):
    registry, source, health, upstream = _fixture(tmp_path)
    candidate = registry.issue_probe(
        source.token,
        infohash="b" * 40,
        metadata_sha256="2" * 64,
        selected_files=("new/Fixture.S02E04.mkv",),
        quality_rank=(1, 1080),
        budget_bytes=20000,
        capacity=CapacityEvidence(100000, {source.infohash: 8000}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with _client(registry, upstream, health.is_protected) as client:
        response = client.post(
            "/api/v2/torrents/add",
            headers=HEADERS | {"X-Admission-Permit": candidate.token},
            json={
                "infohash": candidate.infohash,
                "metadata_sha256": candidate.metadata_sha256,
                "savepath": candidate.destination,
                "category": candidate.category,
            },
        )
    assert response.status_code == 403
    assert candidate.infohash not in upstream.entries
    assert registry.get(candidate.token).state == "authorized"
    assert upstream.entries[source.infohash]["state"] == "stoppedDL"


def test_metadata_repair_cannot_reopen_protected_source(tmp_path):
    registry, source, health, upstream = _fixture(tmp_path)
    metadata = TorrentArtifactStore(tmp_path / "control.sqlite")
    # Repair must reject protection before it reads cached metadata or adds the source.
    upstream.entries[source.infohash].update(total_size=0, downloaded=0, progress=0)
    before = dict(upstream.entries[source.infohash])
    with _client(registry, upstream, health.is_protected, torrent_store=metadata) as client:
        response = client.post(
            "/internal/repair-metadata",
            headers=HEADERS,
            json={"permit_token": source.token},
        )
    assert response.status_code == 403
    assert upstream.entries[source.infohash] == before


def _movie(registry, identifier):
    repository = ReservationRepository(registry._db_path)
    reserved = repository.reserve(
        request_id=f"seerr:movie:{identifier}",
        source_id=f"movie:{identifier}",
        media_key=f"movie:tmdb:{identifier}",
        filesystem_id="fixture",
        budget_bytes=10000,
        free_bytes=100000,
        total_bytes=200000,
    )
    movie = registry.issue(
        infohash=str(identifier) * 40,
        metadata_sha256="1" * 64,
        destination="/data/torrents",
        category="radarr",
        reservation_id=reserved.reservation_id,
        selected_files=(f"movie-{identifier}/movie.mkv",),
        budget_bytes=10000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    _confirm(registry, movie)
    return movie


def test_priority_reorders_other_movies_without_moving_protected_episode_to_front(tmp_path):
    registry, source, health, upstream = _fixture(tmp_path)
    low = _movie(registry, 2)
    high = _movie(registry, 3)
    upstream.add_entry(low, state="queuedDL")
    upstream.add_entry(high, state="queuedDL")
    upstream.entries[low.infohash].update(priority=1, num_seeds=2)
    upstream.entries[high.infohash].update(priority=2, num_seeds=50)
    upstream.entries[source.infohash].update(priority=3, state="queuedDL")
    with _client(registry, upstream, health.is_protected) as client:
        response = client.post("/internal/prioritize-movies", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"state": "reordered", "count": 2}
    assert upstream.entries[high.infohash]["priority"] == 1
    assert upstream.entries[low.infohash]["priority"] == 2
    assert upstream.entries[source.infohash]["priority"] == 3
    assert upstream.entries[source.infohash]["state"] == "queuedDL"
