"""Explicit audited replacements preserve history and retire only proven sources."""

import copy
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.audited_imports import AuditedImportStore
from homeserver_control.persistence.db import ReservationRepository


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    return b"d" + b"".join(bencode(key) + bencode(value[key]) for key in sorted(value)) + b"e"


def sealed(jobs, name):
    digest = hashlib.sha256(
        json.dumps(jobs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"schema_version": 1, "mode": "audited_manifest", "manifest_id": name,
            "manifest_sha256": digest, "jobs": copy.deepcopy(jobs)}


def add_job(database, name, *, kind="movie", episodes=(1, 2), tmdb=100, state="confirmed"):
    paths = [f"Film-{name}.mkv"] if kind == "movie" else [
        f"Show-{name}.S01E{number:02}.mkv" for number in episodes
    ]
    descriptor = bencode({b"info": {
        b"files": [{b"length": 100 + index, b"path": [path.encode()]}
                   for index, path in enumerate(paths)],
        b"name": name.encode(), b"piece length": 16384, b"pieces": b"x" * 20,
    }})
    parsed = inspect_torrent(descriptor)
    candidate = hashlib.sha256(("torrent:" + parsed.infohash).encode()).hexdigest()
    payload = [{"path": file.path, "size_bytes": file.length} for file in parsed.files]
    files = []
    for index, file in enumerate(payload):
        identity = {"kind": "movie", "native_movie_id": tmdb + 1000} if kind == "movie" else {
            "kind": "episode", "native_series_id": tmdb + 1000,
            "native_episode_ids": [tmdb * 1000 + episodes[index]],
            "canonical_episode_ids": [tmdb * 100 + episodes[index]],
            "scopes": [f"S01E{episodes[index]:02}"],
        }
        files.append(file | identity)
    job = {"job_id": "job-" + name, "candidate_id": candidate,
           "permit_id": "permit-" + name, "reservation_id": "reserve-" + name,
           "category": "radarr" if kind == "movie" else "sonarr",
           "entry_id": f"{kind}-{tmdb}", "tmdb_id": tmdb,
           "tvdb_id": None if kind == "movie" else tmdb + 5000,
           "metadata_sha256": parsed.metadata_sha256, "release_title": name,
           "expected_payload_bytes": parsed.total_bytes, "padding_bytes": 0,
           "payload_files": payload, "files": files, "excluded_files": [],
           "subtitle_policy": {"profile_name": "Standard", "original_language": "en"}}
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO requests VALUES (?,?,?,?,?,?)", (
            "request-" + name, "source-" + name, "collection:audited:" + candidate,
            "reserved", now, now))
        connection.execute(
            "INSERT INTO reservations(id,request_id,media_key,filesystem_id,budget_bytes,state) "
            "VALUES (?,?,?,?,?,?)", (job["reservation_id"], "request-" + name,
                                    "collection:audited:" + candidate, "uuid-fixture",
                                    parsed.total_bytes, "downloading"))
        connection.execute(
            "INSERT INTO gateway_permits(permit_id,token,operation_id,reservation_id,infohash,"
            "metadata_sha256,destination,category,selected_files_json,budget_bytes,expires_at,"
            "state,pool_id,filesystem_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                job["permit_id"], "token-" + name, "operation-" + name, job["reservation_id"],
                parsed.infohash, parsed.metadata_sha256,
                "/data/torrents/.placements/" + job["permit_id"], job["category"],
                json.dumps([file.path for file in parsed.files]), parsed.total_bytes,
                (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                state, "hdd3", "uuid-fixture"))
        connection.execute("CREATE TABLE IF NOT EXISTS torrent_artifacts ("
                           "permit_id TEXT PRIMARY KEY,infohash TEXT,"
                           "metadata_sha256 TEXT,content BLOB)")
        connection.execute("INSERT INTO torrent_artifacts VALUES (?,?,?,?)", (
            job["permit_id"], parsed.infohash, parsed.metadata_sha256, descriptor))
    return job


@pytest.fixture
def replacement(tmp_path):
    database = tmp_path / "control.sqlite"
    ReservationRepository(database).initialize()
    PermitRegistry(database)
    store = AuditedImportStore(database)
    old = add_job(database, "old-series", kind="episode", tmdb=101)
    keep = add_job(database, "keep", tmdb=200)
    parent = sealed([old, keep], "parent-manifest")
    store.install_manifest(parent)
    first = add_job(database, "new-first", kind="episode", episodes=(1,), tmdb=101,
                    state="authorized")
    second = add_job(database, "new-second", kind="episode", episodes=(2,), tmdb=101,
                     state="authorized")
    manifest = sealed([keep, first, second], "replacement-manifest")

    def stopped(binding):
        return dict(binding, native_id=binding["infohash"], state="stoppedDL", progress=0.5,
                    amount_left=100, total_size=binding["budget_bytes"], checked_at=time.time(),
                    payload_retained=True)

    return SimpleNamespace(database=database, store=store, old=old, keep=keep,
                           new=[first, second], parent=parent, manifest=manifest, stopped=stopped,
                           replacements={old["job_id"]: [first["job_id"], second["job_id"]]})


def install(fixture, **kwargs):
    return fixture.store.install_replacement_manifest(
        fixture.manifest, expected_parent_manifest_id=fixture.parent["manifest_id"],
        replacements=fixture.replacements, stopped_source_validator=fixture.stopped, **kwargs)


def snapshot(database):
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        return {name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
                if name in tables else []
                for name in ("audited_import_jobs", "audited_import_files", "gateway_permits",
                             "reservations", "audited_import_manifests",
                             "audited_import_manifest_history", "audited_import_supersessions")}


def test_replacement_retires_one_pack_without_changing_history_or_untouched_work(replacement):
    fixture = replacement
    before = snapshot(fixture.database)
    receipts = install(fixture)
    assert len(receipts) == 1 and receipts[0]["payload_retained"] is True
    after = snapshot(fixture.database)
    assert after["audited_import_jobs"][:2] == before["audited_import_jobs"]
    assert after["audited_import_files"][:3] == before["audited_import_files"]
    assert after["audited_import_manifest_history"][:1] == before[
        "audited_import_manifest_history"]
    with sqlite3.connect(fixture.database) as connection:
        assert connection.execute("SELECT state FROM gateway_permits WHERE permit_id=?", (
            fixture.old["permit_id"],)).fetchone() == ("superseded",)
        assert connection.execute("SELECT state FROM reservations WHERE id=?", (
            fixture.old["reservation_id"],)).fetchone() == ("superseded",)
    assert {row["job_id"] for row in fixture.store.pending_files(limit=20)} == {
        fixture.keep["job_id"], *(job["job_id"] for job in fixture.new)}


def test_identical_retry_never_calls_native_validator_or_resets_new_work(replacement):
    first = install(replacement)
    replacement.store.defer(next(row["file_id"] for row in replacement.store.pending_files()
                                if row["job_id"] == replacement.new[0]["job_id"]), "waiting")
    before = snapshot(replacement.database)
    replacement.stopped = lambda _: pytest.fail("idempotent retry repeated native validation")
    assert install(replacement) == first
    assert snapshot(replacement.database) == before
    replacement.store.install_manifest(replacement.manifest)


def test_ordinary_manifest_append_still_preserves_history_after_supersession(replacement):
    install(replacement)
    extra = add_job(replacement.database, "additional", tmdb=300)
    expanded = sealed([*replacement.manifest["jobs"], extra], "later-append")
    replacement.store.install_manifest(expanded)
    assert {row["job_id"] for row in replacement.store.pending_files(limit=20)} == {
        *(job["job_id"] for job in replacement.manifest["jobs"]), extra["job_id"]}


@pytest.mark.parametrize("field,value", [
    ("state", "downloading"), ("progress", 1), ("progress", float("nan")),
    ("amount_left", 0), ("amount_left", True), ("total_size", 999),
    ("payload_retained", False), ("filesystem_id", "wrong-device"),
    ("destination", "/data/torrents/untrusted"), ("infohash", "a" * 40),
    ("checked_at", 1), ("checked_at", float("inf")), ("native_id", "invalid"),
])
def test_native_evidence_must_be_fresh_exact_stopped_and_incomplete(replacement, field, value):
    original = replacement.stopped
    replacement.stopped = lambda binding: original(binding) | {field: value}
    before = snapshot(replacement.database)
    with pytest.raises(ValueError):
        install(replacement)
    assert snapshot(replacement.database) == before


@pytest.mark.parametrize("fault", ["drop-keep", "changed-keep", "missing-episode", "identity",
                                    "policy", "foreign-new", "unmapped-new", "parent-seal"])
def test_replacement_scope_and_canonical_coverage_are_exact(replacement, fault):
    if fault == "drop-keep":
        replacement.manifest["jobs"].pop(0)
    elif fault == "changed-keep":
        replacement.manifest["jobs"][0]["release_title"] = "changed"
    elif fault == "missing-episode":
        replacement.manifest["jobs"].pop()
        replacement.replacements[replacement.old["job_id"]].pop()
    elif fault == "identity":
        replacement.manifest["jobs"][1]["tmdb_id"] = 555
    elif fault == "policy":
        replacement.manifest["jobs"][1]["subtitle_policy"]["profile_name"] = "Dublado"
    elif fault == "foreign-new":
        replacement.replacements[replacement.old["job_id"]] = [replacement.keep["job_id"]]
    elif fault == "unmapped-new":
        replacement.replacements[replacement.old["job_id"]].pop()
    else:
        replacement.parent["manifest_id"] = "unexpected-parent"
    replacement.manifest = sealed(replacement.manifest["jobs"], "replacement-manifest")
    before = snapshot(replacement.database)
    with pytest.raises(ValueError):
        install(replacement)
    assert snapshot(replacement.database) == before


@pytest.mark.parametrize("sql", [
    "UPDATE audited_import_files SET state='import_unknown' WHERE job_id='job-old-series'",
    "UPDATE audited_import_files SET command_id=42 WHERE job_id='job-old-series'",
    "UPDATE audited_import_files SET imported_path='/data/media/file' "
    "WHERE job_id='job-old-series'",
    "UPDATE audited_import_files SET probe_json='{}' WHERE job_id='job-old-series'",
    "UPDATE gateway_permits SET state='unknown' WHERE permit_id='permit-old-series'",
    "UPDATE requests SET state='cancelled' WHERE id='request-old-series'",
    "UPDATE reservations SET state='imported' WHERE id='reserve-old-series'",
    "UPDATE torrent_artifacts SET content=X'00' WHERE permit_id='permit-new-first'",
    "UPDATE gateway_permits SET budget_bytes=999 WHERE permit_id='permit-new-first'",
    "UPDATE gateway_permits SET selected_files_json='[]' WHERE permit_id='permit-new-first'",
    "UPDATE reservations SET media_key='movie:tmdb:101' WHERE id='reserve-new-first'",
    "INSERT INTO tombstones VALUES ('episode:tmdb:101:S01E02','now','manual')",
])
def test_import_effects_identity_drift_and_cancellation_fail_before_any_write(replacement, sql):
    with sqlite3.connect(replacement.database) as connection:
        connection.execute(sql)
    before = snapshot(replacement.database)
    with pytest.raises(ValueError):
        install(replacement)
    assert snapshot(replacement.database) == before


def test_callback_failure_rolls_back_entire_replacement(replacement):
    def fail(_):
        raise RuntimeError("native read failed")
    replacement.stopped = fail
    before = snapshot(replacement.database)
    with pytest.raises(RuntimeError, match="native read failed"):
        install(replacement)
    assert snapshot(replacement.database) == before


def test_manifest_duplicates_remain_prohibited(replacement):
    from homeserver_control.persistence.audited_imports import validate_manifest

    duplicate = copy.deepcopy(replacement.old)
    duplicate.update(job_id="duplicate", permit_id="duplicate", reservation_id="duplicate")
    with pytest.raises(ValueError):
        validate_manifest(sealed([replacement.old, duplicate], "duplicates"))


def test_movie_replacement_preserves_original_canonical_reservation_and_payload(tmp_path):
    database = tmp_path / "control.sqlite"
    ReservationRepository(database).initialize()
    PermitRegistry(database)
    store = AuditedImportStore(database)
    old = add_job(database, "old-movie")
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE reservations SET media_key='movie:tmdb:100'")
        connection.execute("UPDATE requests SET media_key='movie:tmdb:100'")
    parent = sealed([old], "movie-parent")
    store.install_manifest(parent)
    new = add_job(database, "new-movie", state="authorized")
    manifest = sealed([new], "movie-replacement")
    receipts = store.install_replacement_manifest(
        manifest, expected_parent_manifest_id="movie-parent",
        replacements={old["job_id"]: [new["job_id"]]},
        stopped_source_validator=lambda binding: dict(
            binding, native_id=binding["infohash"], state="pausedDL", progress=0,
            amount_left=binding["budget_bytes"], total_size=binding["budget_bytes"],
            payload_retained=True, checked_at=time.time(),
        ),
    )
    assert receipts[0]["payload_retained"] is True
    assert [row["job_id"] for row in store.pending_files()] == [new["job_id"]]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT media_key FROM reservations WHERE id=?", (
            old["reservation_id"],)).fetchone() == ("movie:tmdb:100",)
        assert connection.execute("SELECT COUNT(*) FROM torrent_artifacts").fetchone() == (2,)


def test_stale_worker_cannot_claim_retired_import_intent(replacement):
    old_file_id = next(row["file_id"] for row in replacement.store.pending_files()
                       if row["job_id"] == replacement.old["job_id"])
    install(replacement)
    assert replacement.store.claim_import(old_file_id) is False


def test_replacement_rejects_corrupted_old_file_binding(replacement):
    with sqlite3.connect(replacement.database) as connection:
        connection.execute("UPDATE audited_import_files SET file_json='{}' "
                           "WHERE job_id=?", (replacement.old["job_id"],))
    before = snapshot(replacement.database)
    with pytest.raises(ValueError):
        install(replacement)
    assert snapshot(replacement.database) == before


def test_retry_with_different_scope_is_not_idempotent(replacement):
    install(replacement)
    replacement.replacements[replacement.old["job_id"]].pop()
    before = snapshot(replacement.database)
    with pytest.raises(ValueError):
        install(replacement)
    assert snapshot(replacement.database) == before


def test_existing_asc_candidate_id_keeps_its_historical_identity(replacement):
    old = replacement.old
    old["candidate_id"] = "a" * 64
    replacement.parent = sealed([old, replacement.keep], "parent-manifest")
    encoded_job = json.dumps(old, sort_keys=True, separators=(",", ":"))
    encoded_manifest = json.dumps(replacement.parent, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(replacement.database) as connection:
        connection.execute("UPDATE audited_import_jobs SET job_json=? WHERE job_id=?", (
            encoded_job, old["job_id"]))
        values = (encoded_manifest, replacement.parent["manifest_sha256"],
                  replacement.parent["manifest_sha256"])
        connection.execute("UPDATE audited_import_manifests SET manifest_json=?,"
                           "manifest_sha256=?,jobs_sha256=?", values)
        connection.execute("UPDATE audited_import_manifest_history SET manifest_json=?,"
                           "manifest_sha256=?,jobs_sha256=?", values)
        connection.execute("UPDATE reservations SET media_key=? WHERE id=?", (
            "collection:audited:" + old["candidate_id"], old["reservation_id"]))
        connection.execute("UPDATE requests SET media_key=? WHERE id=?", (
            "collection:audited:" + old["candidate_id"], "request-old-series"))
    install(replacement)
    with sqlite3.connect(replacement.database) as connection:
        assert connection.execute("SELECT job_json FROM audited_import_jobs WHERE job_id=?", (
            old["job_id"],)).fetchone() == (encoded_job,)


def test_unrepresented_active_database_job_prevents_replacement(replacement):
    unexpected = copy.deepcopy(replacement.keep)
    unexpected["job_id"] = "orphan-active-job"
    with sqlite3.connect(replacement.database) as connection:
        connection.execute(
            "INSERT INTO audited_import_jobs(job_id,ordinal,manifest_id,job_json) VALUES (?,?,?,?)",
            (unexpected["job_id"], 999, replacement.parent["manifest_id"],
             json.dumps(unexpected, sort_keys=True, separators=(",", ":"))),
        )
    before = snapshot(replacement.database)
    with pytest.raises(ValueError):
        install(replacement)
    assert snapshot(replacement.database) == before
