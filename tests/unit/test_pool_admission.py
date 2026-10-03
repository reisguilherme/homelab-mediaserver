import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import (
    CapacityEvidence,
    PoolCapacityEvidence,
    capacity_from_queue,
)


class Storage:
    pool_ids = ("ssd", "hdd")
    offline = frozenset()

    def inspect(self, pool_id, *, writable=False):
        if pool_id in self.offline:
            raise RuntimeError("physical pool unavailable")
        return SimpleNamespace(filesystem_id=f"{pool_id}-uuid", free_bytes=10_000)

    def prepare_destination(self, pool_id, permit_id):
        return f"/data/torrents/.placements/{permit_id}"

    def validate_destination(self, pool_id, permit_id):
        return f"/data/torrents/.placements/{permit_id}"


def evidence(ssd, hdd, *, remaining=None, paused=frozenset()):
    return CapacityEvidence(
        ssd,
        {},
        pools=tuple(
            PoolCapacityEvidence(
                pool_id,
                f"{pool_id}-uuid",
                free,
                (remaining or {}) if pool_id == "ssd" else {},
                0,
                paused,
            )
            for pool_id, free in [("ssd", ssd), ("hdd", hdd)]
        ),
    )


def issue(registry, repo, digit, size, capacity):
    reservation = repo.reserve(
        request_id=f"seerr:{digit}",
        source_id=digit,
        media_key=f"movie:tmdb:{digit}",
        filesystem_id="fixture",
        budget_bytes=1,
        free_bytes=10_000,
        total_bytes=20_000,
    )
    return registry.issue(
        infohash=digit * 40,
        destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        category="radarr",
        reservation_id=reservation.reservation_id,
        budget_bytes=size,
        capacity=capacity,
    )


@pytest.mark.parametrize(
    ("ssd", "hdd", "size", "expected"),
    [
        (1000, 2000, 900, "ssd"),
        (500, 2000, 900, "hdd"),
        (500, 600, 900, None),
    ],
)
def test_exact_bytes_choose_one_physical_pool(tmp_path, ssd, hdd, size, expected):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    registry = PermitRegistry(db, storage_registry=Storage())
    if expected is None:
        with pytest.raises(PermissionError, match="waiting_space"):
            issue(registry, repo, "a", size, evidence(ssd, hdd))
        return
    permit = issue(registry, repo, "a", size, evidence(ssd, hdd))
    persisted = PermitRegistry(db, storage_registry=Storage()).get(permit.token)
    assert persisted.pool_id == expected
    assert persisted.filesystem_id == f"{expected}-uuid"
    assert persisted.destination == f"/data/torrents/.placements/{permit.permit_id}"


def test_paused_partial_download_keeps_only_remaining_bytes_reserved(tmp_path):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    registry = PermitRegistry(db, storage_registry=Storage())
    old = issue(registry, repo, "a", 900, evidence(1000, 2000))
    registry.authorize(
        token=old.token,
        infohash=old.infohash,
        destination=old.destination,
        effect=lambda _: {"accepted": True},
    )
    capacity = evidence(500, 2000, remaining={old.infohash: 400}, paused=frozenset({old.infohash}))
    assert registry.pending_bytes(capacity, pool_id="ssd") == 400
    assert issue(registry, repo, "b", 200, capacity).pool_id == "hdd"


def test_unavailable_hdd_does_not_receive_admission(tmp_path):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    storage = Storage()
    storage.offline = frozenset({"hdd"})
    registry = PermitRegistry(db, storage_registry=storage)
    with pytest.raises(PermissionError, match="waiting_space"):
        issue(registry, repo, "a", 900, evidence(500, 2000))


def test_concurrent_admissions_select_only_remaining_pool_headroom(tmp_path):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    registries = [PermitRegistry(db, storage_registry=Storage()) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as threads:
        futures = [
            threads.submit(issue, registries[index], repo, digit, 900, evidence(1000, 1000))
            for index, digit in enumerate(("a", "b"))
        ]
        permits = [future.result(timeout=10) for future in futures]
    assert {permit.pool_id for permit in permits} == {"ssd", "hdd"}
    with pytest.raises(PermissionError, match="waiting_space"):
        issue(registries[0], repo, "c", 900, evidence(1000, 1000))


def test_capacity_evidence_charges_paused_progress_per_verified_pool(tmp_path):
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(
        json.dumps(
            {
                "pools": [
                    {
                        "pool_id": pool,
                        "filesystem_id": f"{pool}-uuid",
                        "state": "ready",
                        "measured_at": time.time(),
                    }
                    for pool in ("ssd", "hdd")
                ]
            }
        )
    )
    entries = [
        {
            "hash": digit * 40,
            "pool_id": pool,
            "filesystem_id": f"{pool}-uuid",
            "save_path": "/data/torrents/.placements/fixture",
            "placement_verified": True,
            "total_size": 1000,
            "amount_left": left,
            "admitted": admitted,
            "state": "pausedDL",
        }
        for digit, pool, left, admitted in [("a", "ssd", 400, True), ("b", "hdd", 700, False)]
    ]
    capacity = capacity_from_queue(
        snapshot_path=snapshot, data_root=tmp_path, payload=entries, storage_registry=Storage()
    )
    assert capacity.pool("ssd").remaining_by_hash == {"a" * 40: 400}
    assert capacity.pool("ssd").other_pending_bytes == 0
    assert capacity.pool("hdd").remaining_by_hash == {}
    assert capacity.pool("hdd").other_pending_bytes == 700
    entries[0].update(total_size=0, amount_left=0, reserved_bytes=1000)
    stalled = capacity_from_queue(
        snapshot_path=snapshot, data_root=tmp_path, payload=entries, storage_registry=Storage()
    )
    assert stalled.pool("ssd").remaining_by_hash == {"a" * 40: 1000}
    entries[1]["placement_verified"] = False
    healthy = capacity_from_queue(
        snapshot_path=snapshot, data_root=tmp_path, payload=entries, storage_registry=Storage()
    )
    assert [pool.pool_id for pool in healthy.pools] == ["ssd"]


def test_registered_arr_add_uses_authorized_destination_and_rejects_lost_pool(tmp_path):
    torrent = (
        b"d4:infod6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:"
        b"pieces20:aaaaaaaaaaaaaaaaaaaaee"
    )
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    storage = Storage()
    registry = PermitRegistry(db, storage_registry=storage)
    reservation = repo.reserve(
        request_id="seerr:route",
        source_id="1",
        media_key="movie:tmdb:1",
        filesystem_id="fixture",
        budget_bytes=1,
        free_bytes=10_000,
        total_bytes=20_000,
    )
    permit = registry.issue(
        infohash=inspect_torrent(torrent).infohash,
        metadata_sha256=sha256(torrent).hexdigest(),
        selected_files=("test.mp4",),
        destination="/data/torrents",
        category="radarr",
        budget_bytes=123,
        capacity=evidence(100, 1000),
        reservation_id=reservation.reservation_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    class Upstream:
        added = []

        def add_torrent(self, payload):
            self.added.append(payload)
            return {"accepted": True}

    upstream = Upstream()
    client = TestClient(create_app(permits=registry, upstream=upstream, arr_token="private"))
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "private"})
    data = {"category": "radarr", "savepath": "/data/torrents"}
    files = {"torrents": ("fixture.torrent", torrent, "application/x-bittorrent")}
    storage.offline = frozenset({"hdd"})
    assert client.post("/api/v2/torrents/add", data=data, files=files).status_code == 503
    assert upstream.added == []
    storage.offline = frozenset()
    response = client.post("/api/v2/torrents/add", data=data, files=files)
    assert response.status_code == 200, response.text
    assert upstream.added[0]["savepath"] == permit.destination
    assert upstream.added[0]["autoTMM"] is False
    assert upstream.added[0]["downloadPath"] == permit.destination


def test_season_pack_episode_views_inherit_the_parent_pool(tmp_path):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    registry = PermitRegistry(db, storage_registry=Storage())
    reservation = repo.reserve(
        request_id="seerr:pack",
        source_id="1",
        media_key="season:tmdb:1:1",
        filesystem_id="fixture",
        budget_bytes=1,
        free_bytes=10_000,
        total_bytes=20_000,
    )
    files = ("Fixture.S01E01.mkv", "Fixture.S01E02.mkv")
    parent = registry.issue(
        infohash="a" * 40,
        destination="/data/torrents",
        category="sonarr",
        metadata_sha256="b" * 64,
        selected_files=files,
        scope_key="S01PACK",
        reservation_id=reservation.reservation_id,
        budget_bytes=900,
        capacity=evidence(500, 1000),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    registry.bind_season_pack(
        parent.token, episode_files={"S01E01": (files[0],), "S01E02": (files[1],)}
    )
    for scope in ("S01E01", "S01E02"):
        episode = registry.get_for_reservation(reservation.reservation_id, scope_key=scope)
        assert episode.pool_id == "hdd"
        assert episode.filesystem_id == "hdd-uuid"
        assert episode.destination == parent.destination
        assert registry.placement_valid(episode)
    assert registry.pending_bytes(evidence(500, 1000), pool_id="hdd") == 900


def test_probe_can_use_a_different_pool_from_the_original(tmp_path):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    registry = PermitRegistry(db, storage_registry=Storage())
    old = issue(registry, repo, "a", 500, evidence(600, 1000))
    registry.authorize(
        token=old.token,
        infohash=old.infohash,
        destination=old.destination,
        effect=lambda _: {"accepted": True},
    )
    registry.set_quality(old.token, (1, 1, 1))
    probe = registry.issue_probe(
        old.token,
        infohash="b" * 40,
        metadata_sha256="c" * 64,
        selected_files=("probe.mkv",),
        budget_bytes=700,
        quality_rank=(1, 1, 1),
        capacity=evidence(600, 1000, remaining={old.infohash: 400}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    assert old.pool_id == "ssd"
    assert probe.pool_id == "hdd"
    assert probe.destination != old.destination
    persisted = registry.get(probe.token)
    assert persisted.pool_id == "hdd"
    assert persisted.filesystem_id == "hdd-uuid"


@pytest.mark.parametrize(
    "bad_fields",
    [
        {"placement_verified": False},
        {"filesystem_id": "replacement-disk"},
        {"total_size": 0, "amount_left": 0, "admitted": False},
        {"total_size": 0, "amount_left": 0},
        {"hash": "invalid"},
        {"admitted": "true"},
    ],
)
def test_bad_attributed_queue_quarantines_only_its_pool(tmp_path, bad_fields):
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(
        json.dumps(
            {
                "pools": [
                    {
                        "pool_id": pool,
                        "filesystem_id": f"{pool}-uuid",
                        "state": "ready",
                        "measured_at": time.time(),
                    }
                    for pool in ("ssd", "hdd")
                ]
            }
        )
    )
    entry = {
        "pool_id": "hdd",
        "filesystem_id": "hdd-uuid",
        "placement_verified": True,
        "hash": "a" * 40,
        "admitted": True,
        "total_size": 1000,
        "amount_left": 700,
        **bad_fields,
    }
    capacity = capacity_from_queue(
        snapshot_path=snapshot, data_root=tmp_path, payload=[entry], storage_registry=Storage()
    )
    assert [pool.pool_id for pool in capacity.pools] == ["ssd"]
    assert capacity.pool("ssd").free_bytes == 10_000
    entry["pool_id"] = None
    with pytest.raises(ValueError, match="unverified gateway queue placement"):
        capacity_from_queue(
            snapshot_path=snapshot, data_root=tmp_path, payload=[entry], storage_registry=Storage()
        )


@pytest.mark.parametrize(
    "failure", ["offline", "path", "category", "disconnect_during_read", "capacity_changed", None]
)
def test_metadata_repair_rechecks_destination_and_capacity_before_add(tmp_path, failure):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    storage = Storage()
    registry = PermitRegistry(db, storage_registry=storage)
    permit = issue(registry, repo, "a", 900, evidence(500, 2000))
    registry.authorize(
        token=permit.token,
        infohash=permit.infohash,
        destination=permit.destination,
        effect=lambda _: {"accepted": True},
    )
    if failure == "offline":
        storage.offline = frozenset({"hdd"})

    class Upstream:
        def __init__(self):
            self.added = []

        def read(self, path, params=None):
            if failure == "disconnect_during_read":
                storage.offline = frozenset({"hdd"})
            return [
                {
                    "hash": permit.infohash,
                    "total_size": 123 if self.added else 0,
                    "downloaded": 0,
                    "progress": 0,
                    "category": "sonarr" if failure == "category" else permit.category,
                    "save_path": "/data/torrents/changed"
                    if failure == "path"
                    else permit.destination,
                }
            ]

        def add_torrent(self, payload):
            self.added.append(payload)
            return {"accepted": True}

    capacity_reads = []

    def current_capacity():
        capacity_reads.append(True)
        return evidence(
            500, 500 if failure == "capacity_changed" and len(capacity_reads) > 1 else 2000
        )

    upstream = Upstream()
    client = TestClient(
        create_app(
            permits=registry,
            upstream=upstream,
            arr_token="private",
            capacity_provider=current_capacity,
            torrent_store=SimpleNamespace(get=lambda _: b"verified-fixture"),
        )
    )
    response = client.post(
        "/internal/repair-metadata",
        headers={"X-Arr-Token": "private"},
        json={"permit_token": permit.token},
    )
    if failure is None:
        assert response.status_code == 200, response.text
        assert len(upstream.added) == 1
        assert len(capacity_reads) == 2
    else:
        assert response.status_code in {409, 503}, response.text
        assert upstream.added == []
