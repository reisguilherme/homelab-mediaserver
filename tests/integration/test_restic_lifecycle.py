import json
import os
import shutil
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

import pytest

from homeserver_common.backup import (
    _restic,
    _retention,
    copy_backups,
    create_backup,
    restore_snapshot,
    verify_backup,
)
from homeserver_common.env import load_settings

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("restic") is None,
    reason="real Restic lifecycle requires Linux/restic",
)


def fixture(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "HOMESERVER_ENVIRONMENT=dev",
                f"HOMESERVER_INSTALL_ROOT={tmp_path / 'install'}",
                f"HOMESERVER_BACKUP_REPOSITORY={tmp_path / 'source-repo'}",
                "HOMESERVER_BACKUP_PASSWORD=source-password",
                f"HOMESERVER_BACKUP_TARGET_REPOSITORY={tmp_path / 'destination-repo'}",
                "HOMESERVER_BACKUP_TARGET_PASSWORD=destination-password",
                "HOMESERVER_BACKUP_ENABLED=true",
            ]
        )
    )
    settings = load_settings(env, mode="dev")
    control = Path(settings.appdata_root) / "control"
    control.mkdir(parents=True)
    database = sqlite3.connect(control / "control.sqlite")
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("CREATE TABLE tombstones(id TEXT PRIMARY KEY)")
    database.execute("INSERT INTO tombstones VALUES ('cancelled-fixture')")
    database.commit()
    # Keep a WAL connection open: staging must use SQLite's backup API.
    (control / "config.json").write_text('{"fixture":true}\n')
    media = Path(settings.media_root)
    media.mkdir(parents=True)
    (media / "excluded-media.bin").write_bytes(b"do not back up media")
    return env, settings, database


def test_real_restic_preserves_wal_tombstones_and_excludes_media(tmp_path: Path) -> None:
    env, settings, database = fixture(tmp_path)
    try:
        result = create_backup(settings, env)
    finally:
        database.close()
    assert len(result["snapshot_id"]) == 64
    assert result["media_included"] is False
    assert isinstance(result["completed_at"], float)
    assert abs(result["completed_at"] - time.time()) < 30
    persistent = Path(settings.appdata_root) / "control/last-backup.json"
    mirror = Path(settings.run_root) / "last-backup.json"
    assert json.loads(persistent.read_text()) == result
    assert json.loads(mirror.read_text()) == result
    assert stat.S_IMODE(persistent.stat().st_mode) == 0o600
    assert stat.S_IMODE(mirror.stat().st_mode) == 0o600
    assert verify_backup(settings)["verified"] is True
    target = tmp_path / "restore"
    restored = restore_snapshot(settings, result["snapshot_id"], target, isolated=True)
    assert restored["admission_enabled"] is False
    with sqlite3.connect(target / "appdata/control/control.sqlite") as conn:
        assert conn.execute("SELECT id FROM tombstones").fetchall() == [("cancelled-fixture",)]
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (
        (target / "appdata/control/RECOVERY_MODE")
        .read_text()
        .startswith("admission_enabled=false\n")
    )
    assert not list(target.rglob("excluded-media.bin"))
    assert (
        json.loads((target / "restore-report.json").read_text())["media_reconciliation_required"]
        is True
    )
    assert not (Path(settings.run_root) / "maintenance").exists()


def test_generated_directories_are_excluded_before_following_log_symlinks(tmp_path: Path) -> None:
    env, settings, database = fixture(tmp_path)
    appdata = Path(settings.appdata_root)
    release = Path(settings.install_root) / "releases/fixture"
    release.mkdir(parents=True)
    (release / "manifest.json").write_text('{"fixture":true}\n')
    logs = appdata / "seerr/logs"
    logs.mkdir(parents=True)
    actual_log = logs / "jellyseerr-2026-09-28.log"
    actual_log.write_text("generated log\n")
    (logs / "jellyseerr.log").symlink_to(actual_log.name)
    (logs / ".machinelogs.json").symlink_to(".machinelogs-2026-09-28.json")
    for directory in (
        appdata / "control/cache", release / "logs", release / ".venv", release / "__pycache__"
    ):
        directory.mkdir()
        (directory / "generated.bin").write_bytes(b"unnecessary generated state")
    # Exclusions apply to directory names, not ordinary files with those names.
    (appdata / "control/logs").write_text("operator state, not a log directory\n")
    try:
        result = create_backup(settings, env)
    finally:
        database.close()
    target = tmp_path / "restore-without-generated-state"
    restore_snapshot(settings, result["snapshot_id"], target, isolated=True)
    assert (target / "releases/fixture/manifest.json").is_file()
    assert (target / "appdata/control/config.json").is_file()
    assert (target / "appdata/control/logs").read_text() == (
        "operator state, not a log directory\n"
    )
    assert not (target / "appdata/seerr/logs").exists()
    assert not (target / "appdata/control/cache").exists()
    assert not (target / "releases/fixture/logs").exists()
    assert not (target / "releases/fixture/.venv").exists()
    assert not (target / "releases/fixture/__pycache__").exists()
    assert actual_log.read_text() == "generated log\n"
    assert (logs / "jellyseerr.log").is_symlink()
    assert (logs / ".machinelogs.json").is_symlink()


def test_empty_exclusion_setting_keeps_ordinary_generated_directories(tmp_path: Path) -> None:
    env, _, database = fixture(tmp_path)
    env.write_text(env.read_text() + "\nHOMESERVER_BACKUP_EXCLUDE_DIRS=\n")
    settings = load_settings(env, mode="dev")
    appdata = Path(settings.appdata_root)
    release = Path(settings.install_root) / "releases/fixture"
    release.mkdir(parents=True)
    for root in (appdata / "control", release):
        for name in (".venv", "__pycache__", "logs", "cache"):
            directory = root / name
            directory.mkdir()
            (directory / "ordinary.txt").write_text(name + "\n")
    try:
        result = create_backup(settings, env)
    finally:
        database.close()
    target = tmp_path / "restore-with-generated-state"
    restore_snapshot(settings, result["snapshot_id"], target, isolated=True)
    for relative in ("appdata/control", "releases/fixture"):
        for name in (".venv", "__pycache__", "logs", "cache"):
            assert (target / relative / name / "ordinary.txt").read_text() == name + "\n"


def test_nonexcluded_state_symlink_still_rejects_capture(tmp_path: Path) -> None:
    env, settings, database = fixture(tmp_path)
    control = Path(settings.appdata_root) / "control"
    (control / "state-alias.json").symlink_to("config.json")
    try:
        with pytest.raises(ValueError, match="symlink"):
            create_backup(settings, env)
    finally:
        database.close()
    assert (control / "state-alias.json").is_symlink()
    assert (control / "config.json").read_text() == '{"fixture":true}\n'
    assert not (Path(settings.run_root) / "maintenance").exists()


def test_wrong_password_missing_snapshot_and_occupied_target_are_safe(tmp_path: Path) -> None:
    env, settings, database = fixture(tmp_path)
    create_backup(settings, env)
    database.close()
    bad_env = tmp_path / "bad.env"
    bad_env.write_text(env.read_text().replace("source-password", "wrong-password"))
    bad = load_settings(bad_env, mode="dev")
    with pytest.raises(RuntimeError):
        verify_backup(bad)
    missing = tmp_path / "missing-target"
    with pytest.raises(RuntimeError):
        restore_snapshot(settings, "0" * 64, missing, isolated=True)
    assert not missing.exists()
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep").write_text("keep")
    with pytest.raises(ValueError, match="empty"):
        restore_snapshot(settings, "latest", occupied, isolated=True)
    assert (occupied / "keep").read_text() == "keep"


def test_copy_is_independent_and_survives_source_repository_removal(tmp_path: Path) -> None:
    env, settings, database = fixture(tmp_path)
    create_backup(settings, env)
    database.close()
    first = copy_backups(settings)
    assert first["verified"] is True
    copy_backups(settings)
    destination = Path(settings.backup_target_repository)
    completed = subprocess.run(
        ["restic", "-r", str(destination), "snapshots", "--json"],
        env=os.environ | {"RESTIC_PASSWORD": "destination-password"},
        check=True,
        text=True,
        capture_output=True,
    )
    assert len(json.loads(completed.stdout)) == 1
    shutil.rmtree(settings.backup_repository)
    offsite_env = tmp_path / "offsite.env"
    offsite_env.write_text(
        env.read_text()
        .replace(str(settings.backup_repository), str(destination))
        .replace(
            "HOMESERVER_BACKUP_PASSWORD=source-password",
            "HOMESERVER_BACKUP_PASSWORD=destination-password",
        )
    )
    offsite = load_settings(offsite_env, mode="dev")
    restore_snapshot(offsite, "latest", tmp_path / "from-offsite", isolated=True)
    assert (tmp_path / "from-offsite/appdata/control/control.sqlite").exists()


def test_backup_failure_releases_maintenance_without_pruning_last_snapshot(tmp_path: Path) -> None:
    env, settings, database = fixture(tmp_path)
    create_backup(settings, env)
    database.close()
    bad_env = tmp_path / "bad.env"
    bad_env.write_text(env.read_text().replace("source-password", "wrong-password"))
    with pytest.raises(RuntimeError):
        create_backup(load_settings(bad_env, mode="dev"), bad_env)
    assert verify_backup(settings)["verified"] is True
    assert not (Path(settings.run_root) / "maintenance").exists()


def test_real_restic_lock_blocks_prune_and_preserves_verified_snapshot(tmp_path: Path) -> None:
    env, settings, database = fixture(tmp_path)
    snapshot = create_backup(settings, env)["snapshot_id"]
    database.close()
    process = subprocess.Popen(
        [
            "restic",
            "-r",
            settings.backup_repository,
            "backup",
            "--stdin",
            "--stdin-filename",
            "held",
        ],
        env=os.environ | {"RESTIC_PASSWORD": settings.backup_password},
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not _restic(settings, ["list", "locks"]).stdout.strip():
            assert process.poll() is None, "lock holder exited before acquiring repository lock"
            assert time.monotonic() < deadline, "Restic lock holder did not acquire a lock"
            time.sleep(0.05)
        with pytest.raises(RuntimeError, match="operation failed"):
            _retention(settings)
    finally:
        process.communicate(input=b"held-fixture", timeout=30)
    assert process.returncode == 0
    available = json.loads(_restic(settings, ["snapshots", "--json", "--tag", "homeserver"]).stdout)
    assert any(row["id"] == snapshot for row in available)
    restore_snapshot(settings, snapshot, tmp_path / "after-lock", isolated=True)
    assert (tmp_path / "after-lock/appdata/control/control.sqlite").exists()
