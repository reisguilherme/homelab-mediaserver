"""Inspect stopped core copies without exposing settings or changing SQLite state."""

import gc
import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/audit-core-state.py"


@pytest.fixture
def audit():
    assert SCRIPT.is_file(), "offline core state auditor is missing"
    spec = importlib.util.spec_from_file_location("core_state_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_state(tmp_path):
    root = tmp_path / "restored"
    app = root / "sonarr"
    app.mkdir(parents=True)
    db = app / "sonarr.db"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE ScheduledTasks (Id INTEGER, TypeName TEXT, Interval INTEGER);
        INSERT INTO ScheduledTasks VALUES (1, 'NzbDrone.Core.Indexers.RssSyncCommand', 15);
        CREATE TABLE DownloadClients (Id INTEGER, Enable INTEGER, Name TEXT, Settings TEXT);
        INSERT INTO DownloadClients VALUES (1, 1, 'PRIVATE-NAME', 'PRIVATE-TOKEN');
        CREATE TABLE Notifications (Id INTEGER, OnImport INTEGER, OnUpgrade INTEGER, Settings TEXT);
        INSERT INTO Notifications VALUES (1, 1, 0, 'PRIVATE-URL');
        CREATE TABLE RootFolders (Id INTEGER, Path TEXT);
        INSERT INTO RootFolders VALUES (1, '/data/media/PRIVATE-SERIES');
        INSERT INTO RootFolders VALUES (2, '/unreviewed/PRIVATE-MEDIA');
        CREATE TABLE VersionInfo (Version INTEGER);
        INSERT INTO VersionInfo VALUES (192);
    """)
    connection.commit()
    connection.close()
    (app / "config.xml").write_text("<Config><ApiKey>PRIVATE-API-KEY</ApiKey></Config>")
    return root, db


def fingerprints(root):
    return {
        str(p.relative_to(root)): (
            p.stat().st_mode,
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in root.rglob("*")
        if p.is_file()
    }


def test_schema_and_flags_are_evidence_not_startup_permission(audit, tmp_path):
    root, _ = make_state(tmp_path)
    before = fingerprints(root)
    report = audit.inspect(root, ["sonarr"])
    assert report["startup_allowed"] is False
    assert report["native_containment_verified"] is False
    assert report["restore_provenance_verified"] is False
    database = report["applications"]["sonarr"]["database"]
    assert database["integrity"] == "ok"
    assert database["schema"]["DownloadClients"] == ["Id", "Enable", "Name", "Settings"]
    observations = database["observations"]
    assert observations["DownloadClients"]["flags"]["Enable"] == {"zero": 0, "one": 1, "other": 0}
    assert observations["Notifications"]["flags"]["OnImport"]["one"] == 1
    assert observations["RootFolders"]["path_classes"] == {"container_media": 1, "other": 1}
    assert observations["ScheduledTasks"]["task_types"] == ["NzbDrone.Core.Indexers.RssSyncCommand"]
    assert observations["ScheduledTasks"]["positive_interval_rows"] == 1
    assert observations["VersionInfo"]["numeric_versions"] == [192]
    assert "PRIVATE-" not in json.dumps(report)
    assert fingerprints(root) == before


def test_sqlite_wal_is_included_without_checkpointing_source(audit, tmp_path):
    root, db = make_state(tmp_path)
    live = sqlite3.connect(db)
    try:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("INSERT INTO DownloadClients VALUES (2, 0, 'PRIVATE-NEW', 'PRIVATE-SECRET')")
        live.commit()
        before = fingerprints(root)
        report = audit.inspect(root, ["sonarr"])
        inspected = report["applications"]["sonarr"]["database"]
        assert inspected["observations"]["DownloadClients"]["rows"] == 2
        assert "-wal" in inspected["sidecars"]
        assert fingerprints(root) == before
    finally:
        live.close()


@pytest.mark.parametrize("target", ["application", "database", "wal", "config"])
def test_selected_symlinks_are_refused(audit, tmp_path, target):
    root, db = make_state(tmp_path)
    if target == "application":
        (root / "sonarr").rename(root / "actual")
        (root / "sonarr").symlink_to(root / "actual", target_is_directory=True)
    else:
        path = {
            "database": db,
            "wal": Path(str(db) + "-wal"),
            "config": root / "sonarr/config.xml",
        }[target]
        if path.exists():
            path.rename(path.with_name("outside"))
        path.symlink_to(tmp_path / "secret")
    with pytest.raises(audit.AuditError):
        audit.inspect(root, ["sonarr"])


def test_unselected_private_trees_and_log_symlinks_are_not_read(audit, tmp_path):
    root, _ = make_state(tmp_path)
    (root / "sonarr/logs").symlink_to(tmp_path / "missing", target_is_directory=True)
    (root / "qbittorrent").symlink_to(tmp_path / "missing", target_is_directory=True)
    assert "qbittorrent" not in audit.inspect(root, ["sonarr"])["applications"]


@pytest.mark.parametrize("kind", ["corrupt", "hardlink", "fifo", "missing", "orphan_wal"])
def test_invalid_database_cannot_yield_a_success_report(audit, tmp_path, kind):
    root, db = make_state(tmp_path)
    if kind in {"corrupt", "fifo", "missing", "orphan_wal"}:
        db.unlink()
    if kind == "corrupt":
        db.write_bytes(b"PRIVATE invalid database")
    elif kind == "hardlink":
        os.link(db, tmp_path / "alias.db")
    elif kind == "fifo":
        os.mkfifo(db)
    elif kind == "orphan_wal":
        Path(str(db) + "-wal").write_bytes(b"PRIVATE WAL")
    with pytest.raises(audit.AuditError) as caught:
        audit.inspect(root, ["sonarr"])
    assert "PRIVATE" not in str(caught.value)


def test_unknown_application_and_missing_application_are_refused(audit, tmp_path):
    root, _ = make_state(tmp_path)
    for selection in (["../sonarr"], ["jellyfin"], ["radarr"], [], ["sonarr", "sonarr"]):
        with pytest.raises(audit.AuditError):
            audit.inspect(root, selection)


def test_sensitive_unexpected_task_values_are_counted_not_printed(audit, tmp_path):
    root, db = make_state(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO ScheduledTasks VALUES (2, 'PRIVATE-TASK', 0)")
        connection.execute("INSERT INTO DownloadClients VALUES (2, 'PRIVATE-FLAG', 'a', 'b')")
    report = audit.inspect(root, ["sonarr"])
    tables = report["applications"]["sonarr"]["database"]["observations"]
    assert tables["ScheduledTasks"]["unrecognized_task_types"] == 1
    assert tables["DownloadClients"]["flags"]["Enable"]["other"] == 1
    assert "PRIVATE-" not in json.dumps(report)


def test_namespace_shaped_private_task_is_not_disclosed(audit, tmp_path):
    root, db = make_state(tmp_path)
    secret = "NzbDrone.Core.HELD_PRIVATE_TOKEN_123"
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO ScheduledTasks VALUES (2, ?, 0)", (secret,))
    report = audit.inspect(root, ["sonarr"])
    assert secret not in json.dumps(report)
    tasks = report["applications"]["sonarr"]["database"]["observations"]["ScheduledTasks"]
    assert tasks["unrecognized_task_types"] == 1


def test_sqlite_copy_descriptors_are_closed_before_temp_cleanup(audit, tmp_path):
    root, _ = make_state(tmp_path)
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        audit.inspect(root, ["sonarr"])
        retained = []
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                target = descriptor.readlink()
            except FileNotFoundError:
                continue
            if "homeserver-core-audit-" in str(target):
                retained.append(str(target))
        assert retained == []
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


@pytest.mark.parametrize("path", ["/data/media2/title", "/data/media/../private/title"])
def test_noncanonical_root_cannot_be_classified_as_media(audit, tmp_path, path):
    root, db = make_state(tmp_path)
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM RootFolders")
        connection.execute("INSERT INTO RootFolders VALUES (1, ?)", (path,))
    report = audit.inspect(root, ["sonarr"])
    roots = report["applications"]["sonarr"]["database"]["observations"]["RootFolders"]
    assert roots["path_classes"] == {"other": 1}


def test_qbit_configuration_is_fingerprinted_without_loading_private_values(audit, tmp_path):
    root = tmp_path / "restored"
    config = root / "qbittorrent/qBittorrent/qBittorrent.conf"
    config.parent.mkdir(parents=True)
    config.write_text("[Preferences]\nWebUI\\Password_PBKDF2=PRIVATE-HASH\n")
    report = audit.inspect(root, ["qbittorrent"])
    app = report["applications"]["qbittorrent"]
    assert app["database"] is None
    assert app["configs"]["qBittorrent/qBittorrent.conf"]["present"] is True
    assert app["native_config_review"] == "pending"
    assert "PRIVATE" not in json.dumps(report)


def test_cli_creates_private_new_report_and_never_overwrites(audit, tmp_path):
    root, _ = make_state(tmp_path)
    output = tmp_path / "report.json"
    command = [
        sys.executable,
        str(SCRIPT),
        "--state-root",
        str(root),
        "--application",
        "sonarr",
        "--output",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    original = output.read_bytes()
    assert json.loads(original)["startup_allowed"] is False
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert output.read_bytes() == original
    assert str(root) not in result.stderr


def test_failure_never_creates_output_or_leaks_database_contents(audit, tmp_path):
    root, db = make_state(tmp_path)
    db.write_text("PRIVATE-CORRUPT")
    output = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--state-root",
            str(root),
            "--application",
            "sonarr",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not output.exists()
    assert "PRIVATE" not in result.stdout + result.stderr
    assert str(root) not in result.stdout + result.stderr


def test_output_inside_state_and_symlink_parent_are_refused(audit, tmp_path):
    root, _ = make_state(tmp_path)
    report = audit.inspect(root, ["sonarr"])
    with pytest.raises(audit.AuditError):
        audit.write_report(root / "report.json", report, root)
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(audit.AuditError):
        audit.write_report(tmp_path / "link/report.json", report, root)
    assert not (tmp_path / "report.json").exists()
