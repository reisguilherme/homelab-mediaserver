import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from homeserver_control.migration_rehearsal import RehearsalScenario, _hash, run_rehearsal

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/rehearse-media-batch.py"


def test_success_copies_real_fixture_and_releases_only_exact_simulated_scan():
    report = run_rehearsal(RehearsalScenario.SUCCESS)
    assert report.scope == "synthetic_fixture"
    assert report.containment_kind == "simulated"
    assert not report.production_ready
    assert report.completed
    assert report.blocked_reasons == ()
    assert (
        report.staging_verified and report.publication_verified and report.fixture_readback_verified
    )
    assert report.selected_groups == 3
    assert report.selected_paths == report.published_paths == 4
    assert report.sources_preserved
    assert report.copied_inode_separation and report.alias_topology_verified
    assert report.equal_content_inode_separation and report.unselected_absent
    assert report.restricted_scan_simulated
    assert report.rejected_scan_requests == 4
    assert not any(
        (
            report.execution_allowed,
            report.source_cleanup_allowed,
            report.global_scan_allowed,
            report.deletion_allowed,
        )
    )


@pytest.mark.parametrize(
    "scenario,reason,staging,published",
    [
        (RehearsalScenario.STAGING_CORRUPTION, "fixture_content_mismatch", False, 0),
        (RehearsalScenario.PUBLICATION_CORRUPTION, "fixture_content_mismatch", True, 4),
        (RehearsalScenario.DESTINATION_COLLISION, "destination_collision", True, 0),
        (RehearsalScenario.SOURCE_MUTATION, "source_changed", False, 0),
        (RehearsalScenario.UNACCOUNTED_HARDLINK, "unaccounted_hardlinks:", False, 0),
        (RehearsalScenario.SYMLINK, "special_entries", False, 0),
        (RehearsalScenario.INTERRUPTION, "interrupted_publication", True, 1),
        (RehearsalScenario.CONTAINMENT_LOSS, "simulated_containment_lost", True, 1),
    ],
)
def test_named_failures_hold_consumer_and_keep_sources(scenario, reason, staging, published):
    report = run_rehearsal(scenario)
    assert not report.completed
    if reason.endswith(":"):
        assert any(item.startswith(reason) for item in report.blocked_reasons)
    else:
        assert reason in report.blocked_reasons
    assert report.staging_verified == staging
    assert report.published_paths == published
    assert not report.publication_verified and not report.fixture_readback_verified
    assert report.partial_publication_retained == (published == 1)
    assert not report.restricted_scan_simulated
    assert not report.production_ready
    assert not any(
        (
            report.execution_allowed,
            report.source_cleanup_allowed,
            report.global_scan_allowed,
            report.deletion_allowed,
        )
    )
    assert report.sources_preserved == (scenario != RehearsalScenario.SOURCE_MUTATION)
    if scenario == RehearsalScenario.SOURCE_MUTATION:
        assert "source_not_preserved" in report.blocked_reasons


def test_fixture_hash_detects_mutation_even_with_same_size_and_restored_mtime(monkeypatch):
    real_digest = hashlib.file_digest
    with TemporaryDirectory(dir="/tmp") as directory:
        path = Path(directory) / "synthetic"
        path.write_bytes(b"before")
        info = path.lstat()

        def mutate_after_read(stream, algorithm):
            digest = real_digest(stream, algorithm)
            # ext4 ctime updates may share a kernel clock tick on very small files.
            time.sleep(0.02)
            path.write_bytes(b"after!")
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
            return digest

        monkeypatch.setattr(hashlib, "file_digest", mutate_after_read)
        with pytest.raises(RuntimeError, match="changed_during_hash"):
            _hash(path)


def test_programmatic_rehearsal_rejects_paths_and_unnamed_scenarios():
    with pytest.raises(ValueError):
        run_rehearsal("/srv/data")
    with pytest.raises(TypeError):
        run_rehearsal(RehearsalScenario.SUCCESS, source="/srv/data")


@pytest.mark.parametrize("option", ["--source", "--destination", "--apply", "--resume"])
def test_cli_rejects_any_path_or_apply_options(option):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), option, "/srv/data"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""


def test_cli_default_json_and_held_exit_code():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["completed"] and report["scope"] == "synthetic_fixture"
    assert "/tmp/" not in result.stdout and "/srv/" not in result.stdout
    held = subprocess.run(
        [sys.executable, str(SCRIPT), "--scenario", "interruption"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert held.returncode == 1
    assert not json.loads(held.stdout)["completed"]
