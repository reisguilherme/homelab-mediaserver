"""Immutable audited imports, partition validation and crash-safe request intent."""

import copy
import importlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from homeserver_control.persistence.db import ReservationRepository


def api():
    name = "homeserver_control.persistence.audited_imports"
    assert importlib.util.find_spec(name) is not None, "audited import implementation missing"
    return importlib.import_module(name)


def manifest():
    return {
        "schema_version": 1,
        "mode": "audited_manifest",
        "manifest_id": "first-library",
        "manifest_sha256": "a" * 64,
        "jobs": [
            {
                "job_id": "series-job",
                "candidate_id": "b" * 64,
                "permit_id": "permit-series",
                "reservation_id": "reserve-series",
                "category": "sonarr",
                "metadata_sha256": "c" * 64,
                "entry_id": "series:123",
                "tmdb_id": 123,
                "release_title": "Fixture.S01.1080p.WEB-DL",
                "expected_payload_bytes": 307,
                "padding_bytes": 7,
                "quality": {"resolution": 1080, "source": "webdl"},
                "payload_files": [
                    {"path": "Show/Show.S01E01E02.mkv", "size_bytes": 100},
                    {"path": "Show/Show.S01E03.mkv", "size_bytes": 150},
                    {"path": "Show/sample.mkv", "size_bytes": 50},
                ],
                "files": [
                    {
                        "path": "Show/Show.S01E01E02.mkv",
                        "size_bytes": 100,
                        "kind": "episode",
                        "native_series_id": 5,
                        "native_episode_ids": [11, 12],
                        "canonical_episode_ids": [21, 22],
                        "scopes": ["S01E01", "S01E02"],
                        "subtitle_release_title": "Fixture.S01E01E02.1080p.WEB-DL",
                    },
                    {
                        "path": "Show/Show.S01E03.mkv",
                        "size_bytes": 150,
                        "kind": "episode",
                        "native_series_id": 5,
                        "native_episode_ids": [13],
                        "canonical_episode_ids": [23],
                        "scopes": ["S01E03"],
                    },
                ],
                "excluded_files": [
                    {"path": "Show/sample.mkv", "size_bytes": 50, "reason": "sample"},
                ],
            }
        ],
    }


def test_manifest_preserves_annotations_and_multi_episode_file():
    data = manifest()
    result = api().validate_manifest(data)
    assert result == data
    assert result["jobs"][0]["files"][0]["native_episode_ids"] == [11, 12]


@pytest.mark.parametrize("path", ["../a", "/a", "a/../b", "a\\b", "a//b", "./a", "C:/a", "a\x00b"])
def test_unsafe_payload_paths_are_rejected(path):
    data = manifest()
    data["jobs"][0]["payload_files"][0]["path"] = path
    with pytest.raises(ValueError):
        api().validate_manifest(data)


@pytest.mark.parametrize("change", ["missing", "overlap", "size", "duplicate", "total", "empty"])
def test_payload_is_an_exact_unique_partition_with_padding(change):
    data = manifest()
    job = data["jobs"][0]
    if change == "missing":
        job["excluded_files"] = []
    elif change == "overlap":
        job["excluded_files"].append(job["files"][0] | {"reason": "sample"})
    elif change == "size":
        job["files"][0]["size_bytes"] += 1
    elif change == "duplicate":
        job["payload_files"].append(copy.deepcopy(job["payload_files"][0]))
    elif change == "total":
        job["expected_payload_bytes"] += 1
    else:
        data["jobs"] = []
    with pytest.raises(ValueError):
        api().validate_manifest(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("tmdb_id", True),
        ("tmdb_id", 0),
        ("padding_bytes", -1),
        ("expected_payload_bytes", 0),
        ("candidate_id", "invalid"),
        ("metadata_sha256", "x" * 64),
        ("category", "other"),
    ],
)
def test_invalid_job_identity_and_byte_counts_are_rejected(field, value):
    data = manifest()
    data["jobs"][0][field] = value
    with pytest.raises(ValueError):
        api().validate_manifest(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("native_episode_ids", [11]),
        ("canonical_episode_ids", [21, 21]),
        ("scopes", ["S01E01", "unsafe"]),
        ("native_series_id", 0),
        ("kind", "movie"),
        ("size_bytes", 0),
    ],
)
def test_episode_identity_and_lengths_are_validated(field, value):
    data = manifest()
    data["jobs"][0]["files"][0][field] = value
    with pytest.raises(ValueError):
        api().validate_manifest(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("native_episode_ids", [11]),
        ("canonical_episode_ids", [21]),
        ("scopes", ["S01E01"]),
    ],
)
def test_same_episode_cannot_be_imported_from_two_files(field, value):
    data = manifest()
    data["jobs"][0]["files"][1][field] = value
    with pytest.raises(ValueError):
        api().validate_manifest(data)


def test_same_episode_cannot_be_imported_from_two_jobs():
    data = manifest()
    duplicate = copy.deepcopy(data["jobs"][0])
    duplicate.update(job_id="different-job", permit_id="different-permit", reservation_id="other")
    data["jobs"].append(duplicate)
    with pytest.raises(ValueError):
        api().validate_manifest(data)


def test_movie_targets_validate_without_episode_fields():
    data = manifest()
    job = data["jobs"][0]
    job.update(
        category="radarr",
        expected_payload_bytes=100,
        padding_bytes=0,
        payload_files=[{"path": "Film.mkv", "size_bytes": 100}],
        excluded_files=[],
        files=[{"path": "Film.mkv", "size_bytes": 100, "kind": "movie", "native_movie_id": 9}],
    )
    assert api().validate_manifest(data)["jobs"][0]["files"][0]["native_movie_id"] == 9


def write_manifest(path, data=None):
    path.write_text(json.dumps(data or manifest()), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_load_requires_absolute_fixed_private_regular_file(tmp_path):
    module = api()
    database = tmp_path / "control.sqlite"
    path = write_manifest(tmp_path / "audited-imports.json")
    assert module.load_manifest(path, database) == manifest()
    wrong = write_manifest(tmp_path / "other.json")
    with pytest.raises(ValueError):
        module.load_manifest(wrong, database)
    with pytest.raises(ValueError):
        module.load_manifest(path.name, database)
    if os.name == "posix":
        path.chmod(0o644)
        with pytest.raises(ValueError):
            module.load_manifest(path, database)


@pytest.mark.skipif(os.name != "posix", reason="Linux filesystem contract")
def test_load_rejects_symlink_file_and_parent(tmp_path):
    module = api()
    real = write_manifest(tmp_path / "real.json")
    path = tmp_path / "audited-imports.json"
    path.symlink_to(real)
    with pytest.raises(ValueError):
        module.load_manifest(path, tmp_path / "control.sqlite")
    directory = tmp_path / "real-dir"
    directory.mkdir()
    write_manifest(directory / "audited-imports.json")
    linked = tmp_path / "linked-dir"
    linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(ValueError):
        module.load_manifest(linked / "audited-imports.json", linked / "control.sqlite")


def test_load_bounds_bytes_and_rejects_duplicate_json_keys(tmp_path):
    module = api()
    path = tmp_path / "audited-imports.json"
    path.touch(mode=0o600)
    with path.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    with pytest.raises(ValueError):
        module.load_manifest(path, tmp_path / "control.sqlite")


def test_load_rejects_duplicate_keys_in_otherwise_valid_json(tmp_path):
    module = api()
    path = write_manifest(tmp_path / "audited-imports.json")
    text = path.read_text(encoding="utf-8").replace(
        '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
    )
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_manifest(path, tmp_path / "control.sqlite")


@pytest.mark.skipif(os.name != "posix", reason="Linux filesystem contract")
def test_load_rejects_fifo_without_blocking(tmp_path):
    api()
    path = tmp_path / "audited-imports.json"
    os.mkfifo(path, 0o600)
    program = (
        "from pathlib import Path\n"
        "from homeserver_control.persistence.audited_imports import load_manifest\n"
        "import sys\n"
        "try:\n"
        " load_manifest(Path(sys.argv[1]), Path(sys.argv[2]))\n"
        "except ValueError:\n"
        " sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", program, str(path), str(tmp_path / "control.sqlite")],
        env={"PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        timeout=3,
        check=False,
    )
    assert child.returncode == 0


@pytest.mark.skipif(os.name != "posix", reason="Linux filesystem contract")
def test_parent_symlink_swap_during_open_is_rejected(tmp_path, monkeypatch):
    module = api()
    directory = tmp_path / "control"
    directory.mkdir()
    path = write_manifest(directory / "audited-imports.json")
    outside = tmp_path / "outside"
    outside.mkdir()
    write_manifest(outside / "audited-imports.json")
    original_open = os.open
    swapped = False

    def swap_then_open(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            directory.rename(tmp_path / "old-control")
            directory.symlink_to(outside, target_is_directory=True)
        return original_open(*args, **kwargs)

    monkeypatch.setattr(module.os, "open", swap_then_open)
    with pytest.raises(ValueError):
        module.load_manifest(path, directory / "control.sqlite")


def store(tmp_path):
    result = api().AuditedImportStore(tmp_path / "control.sqlite")
    result.install_manifest(manifest())
    return result


def test_store_install_is_idempotent_and_sealed_even_after_restart(tmp_path):
    result = store(tmp_path)
    initial = result.pending_files()
    result.install_manifest(copy.deepcopy(manifest()))
    assert result.pending_files() == initial
    assert len(initial) == 2
    assert initial[0]["state"] == "planned"
    assert initial[0]["job"] == manifest()["jobs"][0]
    assert initial[0]["file"] == manifest()["jobs"][0]["files"][0]
    restarted = api().AuditedImportStore(tmp_path / "control.sqlite")
    for change in ("id", "sha", "jobs"):
        data = manifest()
        if change == "id":
            data["manifest_id"] = "new-library"
        elif change == "sha":
            data["manifest_sha256"] = "d" * 64
        else:
            data["jobs"][0]["release_title"] = "Other.Release"
        with pytest.raises(ValueError):
            restarted.install_manifest(data)
    assert restarted.pending_files() == initial


def test_request_intent_survives_restart_and_concurrent_claim(tmp_path):
    result = store(tmp_path)
    file_id = result.pending_files()[0]["file_id"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(result.claim_import, [file_id, file_id]))
    assert sorted(claimed) == [False, True]
    result = api().AuditedImportStore(tmp_path / "control.sqlite")
    row = next(item for item in result.pending_files() if item["file_id"] == file_id)
    assert row["state"] == "import_unknown"
    assert row["command_id"] is None
    assert not result.claim_import(file_id)
    result.record_requested(file_id, 42)
    row = next(item for item in result.pending_files() if item["file_id"] == file_id)
    assert row["state"] == "import_requested"
    assert row["command_id"] == 42


def test_import_subtitle_retry_and_job_completion_are_persistent(tmp_path):
    result = store(tmp_path)
    file_ids = [item["file_id"] for item in result.pending_files()]
    first = file_ids[0]
    result.claim_import(first)
    result.record_requested(first, 42)
    result.mark_imported(first, "/library/Show/S01E01E02.mkv", {"duration": 20.0})
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nFixture subtitle\n"
    result.cache_subtitle(first, "BR_PT", srt)
    result.subtitle_pending(first, retry_after=300)
    assert all(item["file_id"] != first for item in result.pending_files())
    row = next(item for item in result.pending_files(now=10**12) if item["file_id"] == first)
    assert row["state"] == "subtitle_pending"
    assert row["imported_path"] == "/library/Show/S01E01E02.mkv"
    assert row["probe"] == {"duration": 20.0}
    assert row["subtitle_content"] == srt
    assert row["subtitle_language"] == "BR_PT"
    result.complete(first)
    assert len(result.pending_files(now=10**12)) == 1
    result.mark_imported(file_ids[1], "/library/Show/S01E03.mkv")
    result.complete(file_ids[1])
    assert api().AuditedImportStore(tmp_path / "control.sqlite").pending_files() == []
    with sqlite3.connect(tmp_path / "control.sqlite") as connection:
        assert (
            connection.execute("SELECT state FROM audited_import_jobs").fetchone()[0] == "complete"
        )


def test_defer_preserves_unknown_state_and_respects_retry_before_recovery(tmp_path):
    result = store(tmp_path)
    first, second = result.pending_files()
    result.claim_import(first["file_id"])
    assert result.pending_files(limit=1)[0]["file_id"] == first["file_id"]
    result.defer(first["file_id"], "native_unavailable", retry_after=30)
    assert [item["file_id"] for item in result.pending_files()] == [second["file_id"]]
    rows = result.pending_files(now=10**12)
    assert rows[0]["file_id"] == first["file_id"] and rows[0]["state"] == "import_unknown"
    with pytest.raises(ValueError):
        result.defer(first["file_id"], "https://private/token=secret")


@pytest.mark.parametrize("language,content", [("pt", b"invalid"), ("BR_PT", b"invalid")])
def test_only_supported_valid_srt_can_be_cached(tmp_path, language, content):
    result = store(tmp_path)
    with pytest.raises(ValueError):
        result.cache_subtitle(result.pending_files()[0]["file_id"], language, content)


def test_requested_transition_requires_preposted_intent(tmp_path):
    result = store(tmp_path)
    first = result.pending_files()[0]["file_id"]
    with pytest.raises(ValueError):
        result.record_requested(first, 42)
    assert result.pending_files()[0]["state"] == "planned"


@pytest.mark.parametrize("reservation_state", ["reserved", "downloading", "waiting_episodes"])
@pytest.mark.parametrize("request_state", ["reserved", "cancel_requested", "cancelled"])
def test_last_completed_target_finishes_only_real_parent(
    tmp_path, reservation_state, request_state
):
    result = store(tmp_path)
    database = tmp_path / "control.sqlite"
    ReservationRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        for request, source, media, state in (
            ("request-own", "source-own", "series:tmdb:123", request_state),
            ("request-other", "source-other", "series:tmdb:999", "downloading"),
        ):
            connection.execute(
                "INSERT INTO requests (id, source_id, media_key, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'fixture-created', 'fixture-updated')",
                (request, source, media, state),
            )
        for reservation, request, media, state in (
            ("reserve-series", "request-own", "series:tmdb:123", reservation_state),
            ("reserve-other", "request-other", "series:tmdb:999", "downloading"),
        ):
            connection.execute(
                "INSERT INTO reservations (id, request_id, media_key, filesystem_id, "
                "budget_bytes, state) VALUES (?, ?, ?, 'fixture-filesystem', 307, ?)",
                (reservation, request, media, state),
            )
    first, last = result.pending_files()
    result.mark_imported(first["file_id"], "/library/Show/S01E01E02.mkv")
    result.complete(first["file_id"])
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT state FROM reservations WHERE id='reserve-series'"
            ).fetchone()[0]
            == reservation_state
        )
    result.mark_imported(last["file_id"], "/library/Show/S01E03.mkv")
    result.complete(last["file_id"])
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT state FROM reservations WHERE id='reserve-series'"
            ).fetchone()[0]
            == "imported"
        )
        expected = "available" if request_state == "reserved" else request_state
        assert (
            connection.execute("SELECT state FROM requests WHERE id='request-own'").fetchone()[0]
            == expected
        )
        assert (
            connection.execute(
                "SELECT state FROM reservations WHERE id='reserve-other'"
            ).fetchone()[0]
            == "downloading"
        )
        assert (
            connection.execute("SELECT state FROM requests WHERE id='request-other'").fetchone()[0]
            == "downloading"
        )
        assert connection.execute("SELECT COUNT(*) FROM movie_imports").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 2


@pytest.mark.parametrize("reservation_state", ["cancelled", "released", "validating"])
def test_completed_targets_do_not_change_inactive_parent(tmp_path, reservation_state):
    result = store(tmp_path)
    database = tmp_path / "control.sqlite"
    ReservationRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO requests (id, source_id, media_key, state, created_at, updated_at) "
            "VALUES ('request-own', 'source-own', 'series:tmdb:123', 'reserved', 'old', 'old')"
        )
        connection.execute(
            "INSERT INTO reservations (id, request_id, media_key, filesystem_id, "
            "budget_bytes, state) VALUES ('reserve-series', 'request-own', 'series:tmdb:123', "
            "'fixture-filesystem', 307, ?)",
            (reservation_state,),
        )
    for item in result.pending_files():
        result.mark_imported(
            item["file_id"], "/library/Show/" + item["file"]["path"].split("/")[-1]
        )
        result.complete(item["file_id"])
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT state FROM reservations WHERE id='reserve-series'"
            ).fetchone()[0]
            == reservation_state
        )
        assert (
            connection.execute("SELECT state FROM requests WHERE id='request-own'").fetchone()[0]
            == "reserved"
        )


def appended_manifest():
    data = manifest()
    data.update(manifest_id="increment-two", manifest_sha256="d" * 64)
    job = copy.deepcopy(data["jobs"][0])
    job.update(
        job_id="new-job",
        permit_id="new-permit",
        reservation_id="new-reservation",
        tmdb_id=999,
        entry_id="series:999",
    )
    for target in job["files"]:
        target["native_series_id"] = 6
    data["jobs"].append(job)
    return data


@pytest.mark.parametrize("existing_state", ["complete", "import_unknown"])
def test_new_manifest_appends_jobs_without_resetting_existing_work(tmp_path, existing_state):
    result = store(tmp_path)
    original = result.pending_files()
    if existing_state == "complete":
        for row in original:
            result.mark_imported(
                row["file_id"], "/library/Show/" + row["file"]["path"].split("/")[-1]
            )
            result.complete(row["file_id"])
    else:
        result.claim_import(original[0]["file_id"])
        result.defer(original[0]["file_id"], "native_import_pending", retry_after=30)
    before = result.pending_files(limit=100, now=10**12)
    result.install_manifest(appended_manifest())
    restarted = api().AuditedImportStore(tmp_path / "control.sqlite")
    after = restarted.pending_files(limit=100, now=10**12)
    old_rows = [row for row in after if row["job_id"] == "series-job"]
    assert old_rows == before
    assert [row["job_id"] for row in after if row["job_id"] == "new-job"] == ["new-job", "new-job"]
    if existing_state == "complete":
        with sqlite3.connect(tmp_path / "control.sqlite") as connection:
            assert (
                connection.execute(
                    "SELECT state FROM audited_import_jobs WHERE job_id='series-job'"
                ).fetchone()[0]
                == "complete"
            )
    restarted.install_manifest(copy.deepcopy(appended_manifest()))
    assert restarted.pending_files(limit=100, now=10**12) == after


@pytest.mark.parametrize("change", ["remove", "modify", "reuse_id", "no_new_jobs", "old_rollback"])
def test_incremental_manifest_rejects_mutation_removal_and_historical_id_reuse(tmp_path, change):
    result = store(tmp_path)
    result.install_manifest(appended_manifest())
    data = appended_manifest()
    data.update(manifest_id="increment-three", manifest_sha256="e" * 64)
    if change == "remove":
        data["jobs"] = data["jobs"][1:]
    elif change == "modify":
        data["jobs"][0]["release_title"] = "Modified.Release"
    elif change == "reuse_id":
        data["manifest_id"] = "first-library"
    elif change == "old_rollback":
        data = manifest()
    before = result.pending_files(limit=100, now=10**12)
    restarted = api().AuditedImportStore(tmp_path / "control.sqlite")
    with pytest.raises(ValueError):
        restarted.install_manifest(data)
    assert restarted.pending_files(limit=100, now=10**12) == before


def test_jobs_keep_their_original_manifest_binding_across_increments(tmp_path):
    result = store(tmp_path)
    assert {row["manifest_id"] for row in result.pending_files()} == {"first-library"}
    increment = appended_manifest()
    increment["manifest_sha256"] = manifest()["manifest_sha256"]
    result.install_manifest(increment)
    bindings = {row["job_id"]: row["manifest_id"] for row in result.pending_files()}
    assert bindings == {"series-job": "first-library", "new-job": "increment-two"}
    result.install_manifest(copy.deepcopy(increment))
    assert {row["job_id"]: row["manifest_id"] for row in result.pending_files()} == bindings


def test_upgrade_backfills_each_jobs_first_historical_manifest(tmp_path):
    result = store(tmp_path)
    result.install_manifest(appended_manifest())
    database = tmp_path / "control.sqlite"
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(audited_import_jobs)")}
        if "manifest_id" in columns:
            connection.execute("ALTER TABLE audited_import_jobs DROP COLUMN manifest_id")
    restarted = api().AuditedImportStore(database)
    bindings = {row["job_id"]: row["manifest_id"] for row in restarted.pending_files()}
    assert bindings == {"series-job": "first-library", "new-job": "increment-two"}
