"""Multipart targets reserve real same-pool bytes under the admission lock."""

import copy
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.audited_imports import AuditedImportStore, validate_manifest
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import CapacityEvidence


def multipart_job(permit, *, job_id="multipart"):
    return {
        "job_id": job_id,
        "candidate_id": "a" * 64,
        "entry_id": "series-office",
        "permit_id": permit.permit_id,
        "reservation_id": permit.reservation_id,
        "category": "sonarr",
        "tmdb_id": 2316,
        "tvdb_id": 73244,
        "metadata_sha256": "b" * 64,
        "release_title": "The Office Season 5",
        "expected_payload_bytes": 100,
        "padding_bytes": 0,
        "payload_files": [
            {"path": "Show/part1.mp4", "size_bytes": 40},
            {"path": "Show/part2.mp4", "size_bytes": 60},
        ],
        "files": [
            {
                "path": "Show/part1.mp4",
                "size_bytes": 40,
                "kind": "episode",
                "native_series_id": 30,
                "native_episode_ids": [40],
                "canonical_episode_ids": [389875],
                "scopes": ["S05E01"],
                "source_parts": [
                    {"path": "Show/part1.mp4", "size_bytes": 40},
                    {"path": "Show/part2.mp4", "size_bytes": 60},
                ],
                "derivation": {"recipe": "concat_mp4_streamcopy_v1", "max_output_bytes": 200},
            }
        ],
        "excluded_files": [],
    }


def wrap(jobs):
    return {
        "schema_version": 1,
        "mode": "audited_manifest",
        "manifest_id": "test",
        "manifest_sha256": "c" * 64,
        "jobs": jobs,
    }


def fixture(tmp_path, *, suffix="", free=1000):
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    reserved = repo.reserve(
        request_id="operator:audited:" + suffix,
        source_id="uindex:" + suffix,
        media_key="collection:audited:" + "a" * 64,
        filesystem_id="fixture",
        budget_bytes=0,
        free_bytes=free,
        total_bytes=free * 2,
    )
    assert reserved.accepted
    permits = PermitRegistry(db)
    p = permits.issue(
        infohash=hashlib.sha1(suffix.encode()).hexdigest(),
        metadata_sha256="b" * 64,
        destination="/data/torrents",
        category="sonarr",
        reservation_id=reserved.reservation_id,
        scope_key=None,
        selected_files=("Show/part1.mp4", "Show/part2.mp4"),
        budget_bytes=100,
        reported_seeders=1,
        capacity=CapacityEvidence(free, {}),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    job = multipart_job(p, job_id="multipart" + suffix)
    store = AuditedImportStore(db)
    store.install_manifest(wrap([job]))
    file_id = store.pending_files()[0]["file_id"]
    return db, repo, permits, p, store, file_id


def test_two_parts_are_one_target_and_partition_all_payload(tmp_path):
    *_, store, file_id = fixture(tmp_path)
    target = store.pending_files()[0]["file"]
    assert len(target["source_parts"]) == 2
    assert target["canonical_episode_ids"] == [389875]
    assert file_id


@pytest.mark.parametrize(
    "change", ["duplicate", "size", "missing", "recipe", "bound", "movie", "multi_id"]
)
def test_multipart_manifest_refuses_ambiguous_or_unbounded_inputs(tmp_path, change):
    *_, p, store, _fid = fixture(tmp_path)
    job = multipart_job(p)
    t = job["files"][0]
    if change == "duplicate":
        t["source_parts"][1] = copy.deepcopy(t["source_parts"][0])
    elif change == "size":
        t["source_parts"][1]["size_bytes"] += 1
    elif change == "missing":
        t["source_parts"].pop()
    elif change == "recipe":
        t["derivation"]["recipe"] = "encode"
    elif change == "bound":
        t["derivation"]["max_output_bytes"] = 201
    elif change == "movie":
        t["kind"] = "movie"
    else:
        t["native_episode_ids"] += [41]
        t["canonical_episode_ids"] += [389876]
        t["scopes"] += ["S05E02"]
    with pytest.raises(ValueError):
        validate_manifest(wrap([job]))


def test_reservation_is_idempotent_persistent_and_counted_by_normal_admission(tmp_path):
    db, repo, registry, _p, store, fid = fixture(tmp_path)
    cap = CapacityEvidence(500, {})
    result = registry.reserve_audited_derivation(fid, cap)
    assert result["budget_bytes"] == 200
    assert registry.pending_bytes(cap) == 300
    repo.normalize_verified_budgets()
    other = PermitRegistry(db)
    assert other.reserve_audited_derivation(fid, cap) == result
    assert other.pending_bytes(cap) == 300
    new = repo.reserve(
        request_id="new",
        source_id="new",
        media_key="movie:42",
        filesystem_id="fixture",
        budget_bytes=0,
        free_bytes=500,
        total_bytes=1000,
    )
    assert new.accepted
    with pytest.raises(PermissionError, match="waiting_space"):
        other.issue(
            infohash="d" * 40,
            destination="/data/torrents",
            category="radarr",
            metadata_sha256="e" * 64,
            reservation_id=new.reservation_id,
            budget_bytes=201,
            selected_files=("Movie.mp4",),
            capacity=cap,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM gateway_permits").fetchone()[0] == 1


def test_parallel_ledgers_do_not_overbook_and_never_choose_a_different_pool(tmp_path):
    db, _repo, registry, p, store, fid = fixture(tmp_path)
    # A second immutable canonical target and parent, both consume 100 before extras.
    repo = ReservationRepository(db)
    r = repo.reserve(
        request_id="second",
        source_id="second",
        media_key="collection:audited:" + "d" * 64,
        filesystem_id="fixture",
        budget_bytes=0,
        free_bytes=1000,
        total_bytes=2000,
    )
    p2 = registry.issue(
        infohash="d" * 40,
        metadata_sha256="b" * 64,
        destination="/data/torrents",
        category="sonarr",
        reservation_id=r.reservation_id,
        scope_key=None,
        selected_files=p.selected_files,
        budget_bytes=100,
        capacity=CapacityEvidence(1000, {}),
        expires_at=p.expires_at,
    )
    j2 = multipart_job(p2, job_id="second")
    j2["files"][0]["native_episode_ids"] = [41]
    j2["files"][0]["canonical_episode_ids"] = [422063]
    j2["files"][0]["scopes"] = ["S05E13"]
    manifest = wrap([multipart_job(p), j2])
    manifest["manifest_id"] = "next"
    store.install_manifest(manifest)
    fid2 = next(row["file_id"] for row in store.pending_files() if row["job"]["job_id"] == "second")

    def reserve(which):
        try:
            return PermitRegistry(db).reserve_audited_derivation(which, CapacityEvidence(550, {}))[
                "budget_bytes"
            ]
        except PermissionError:
            return "waiting"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, [fid, fid2]), key=str) == [200, "waiting"]
    assert registry.pending_bytes(CapacityEvidence(550, {})) == 400


def test_unsealed_or_cancelled_parent_cannot_reserve(tmp_path):
    db, _repo, registry, _p, _store, fid = fixture(tmp_path)
    with pytest.raises(PermissionError):
        registry.reserve_audited_derivation("unknown", CapacityEvidence(1000, {}))
    with sqlite3.connect(db) as c:
        c.execute("UPDATE reservations SET state='cancelled'")
    with pytest.raises(PermissionError):
        registry.reserve_audited_derivation(fid, CapacityEvidence(1000, {}))


def test_extra_reservation_and_normal_admission_share_one_sqlite_capacity_lock(tmp_path):
    from threading import Barrier

    db, repo, registry, parent, store, fid = fixture(tmp_path)
    new = repo.reserve(
        request_id="new-parent",
        source_id="new-parent",
        media_key="movie:99",
        filesystem_id="fixture",
        budget_bytes=0,
        free_bytes=1000,
        total_bytes=2000,
    )
    barrier = Barrier(2)

    def extra():
        barrier.wait()
        try:
            PermitRegistry(db).reserve_audited_derivation(fid, CapacityEvidence(400, {}))
            return "accepted"
        except PermissionError:
            return "waiting"

    def download():
        barrier.wait()
        try:
            PermitRegistry(db).issue(
                infohash="f" * 40,
                metadata_sha256="e" * 64,
                destination="/data/torrents",
                category="radarr",
                reservation_id=new.reservation_id,
                scope_key=None,
                selected_files=("Movie.mp4",),
                budget_bytes=201,
                capacity=CapacityEvidence(400, {}),
                expires_at=parent.expires_at,
            )
            return "accepted"
        except PermissionError:
            return "waiting"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(extra), pool.submit(download)]
        assert sorted(r.result() for r in results) == ["accepted", "waiting"]
    assert registry.pending_bytes(CapacityEvidence(400, {})) <= 400


def test_derivation_cannot_migrate_to_a_different_pool_with_more_free_space(tmp_path):
    from types import SimpleNamespace

    from homeserver_control.worker.capacity_evidence import PoolCapacityEvidence

    db, repo, registry, parent, store, fid = fixture(tmp_path)
    destination = "/data/torrents/.placements/" + parent.permit_id
    with sqlite3.connect(db) as c:
        c.execute(
            "UPDATE gateway_permits SET pool_id='hdd',filesystem_id='disk1',destination=?",
            (destination,),
        )

    class Storage:
        def inspect(self, pool, writable=False):
            return SimpleNamespace(filesystem_id="disk1", free_bytes=1000)

        def validate_destination(self, pool, permit_id):
            return destination

    registry.storage_registry = Storage()
    cap = CapacityEvidence(
        10000,
        {},
        pools=(
            PoolCapacityEvidence("ssd", "ssd-uuid", 10000),
            PoolCapacityEvidence("hdd", "disk1", 150),
        ),
    )
    with pytest.raises(PermissionError, match="waiting_space"):
        registry.reserve_audited_derivation(fid, cap)
    assert store.derivation(fid) is None
