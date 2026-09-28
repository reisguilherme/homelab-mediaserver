"""Consistent Restic snapshots, independent copy, and isolated recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .install import DependencyError, preflight, run_checked
from .render import atomic_write
from .settings import Settings


@contextmanager
def operation_lock(settings: Settings):
    import fcntl

    run = Path(settings.as_environment()["HOMESERVER_RUN_ROOT"])
    existed = run.exists()
    run.mkdir(parents=True, exist_ok=True, mode=0o750)
    if not existed and settings.environment == "prod":
        os.chown(run, int(settings.service_uid), int(settings.service_gid))
    with (run / "operation.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(
                "another installation, backup or release operation is running"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _compose(settings: Settings, args: list[str]):
    path = Path(settings.install_root) / "shared/compose.json"
    if not path.is_file():
        return None  # First fresh preparation has no declared runtime to stop.
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("HOMESERVER_", "COMPOSE_"))
    }
    return run_checked(
        [
            "docker",
            "compose",
            "--env-file",
            os.devnull,
            "-p",
            settings.instance_name,
            "-f",
            str(path),
            *args,
        ],
        env=environment,
        timeout=180,
    )


def _running_services(settings: Settings) -> list[str]:
    result = _compose(settings, ["ps", "--all", "--format", "json"])
    if result is None or not result.stdout.strip():
        return []
    try:
        rows = json.loads(result.stdout)
    except ValueError:
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    if isinstance(rows, dict):
        rows = [rows]
    return [
        row["Service"] for row in rows if row.get("State") in ("running", "restarting", "paused")
    ]


def stop_current_stack(settings: Settings) -> None:
    """Stop and confirm all declared containers before snapshot/migration writes."""
    _compose(settings, ["stop", "--timeout", "60"])
    if _running_services(settings):
        raise RuntimeError("maintenance could not stop every declared container")


@contextmanager
def maintenance(settings: Settings, reason: str, *, keep_on_error: bool = False, stop_stack=True):
    """The supervisor honors this sentinel before starting any writer."""
    env = settings.as_environment()
    marker = Path(env["HOMESERVER_RUN_ROOT"]) / "maintenance"
    marker.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
    except FileExistsError as error:
        raise RuntimeError("maintenance is already active") from error
    with os.fdopen(descriptor, "w") as output:
        output.write(reason + "\n")
    production = env["HOMESERVER_ENVIRONMENT"] == "prod"
    was_active = False
    failed = False
    running_before = []
    try:
        if production:
            import subprocess

            was_active = (
                subprocess.run(
                    ["systemctl", "is-active", "--quiet", "homeserver-stack.service"],
                    capture_output=True,
                ).returncode
                == 0
            )
            run_checked(["systemctl", "stop", "homeserver-stack.service"], timeout=180)
            if stop_stack:
                running_before = _running_services(settings)
                stop_current_stack(settings)
        preflight(settings, host_tools=False)
        yield
    except BaseException:
        failed = True
        raise
    finally:
        if failed and keep_on_error:
            atomic_write(marker, reason + "_failed\n", mode=0o640)
            if production:
                stop_current_stack(settings)
        else:
            # Never restart onto a missing/wrong media mount, even after a failed capture.
            try:
                preflight(settings, host_tools=False)
            except Exception:
                atomic_write(marker, "storage_guard_failed\n", mode=0o640)
                if production:
                    stop_current_stack(settings)
                raise
            marker.unlink(missing_ok=True)
            if production and was_active:
                try:
                    run_checked(["systemctl", "start", "homeserver-stack.service"], timeout=180)
                except Exception:
                    atomic_write(marker, "restart_failed\n", mode=0o640)
                    raise
            elif production and stop_stack and running_before:
                _compose(settings, ["up", "-d", "--no-build", *running_before])


def _restic(settings: Settings, args: list[str], *, target=False, cwd=None):
    env = settings.as_environment()
    prefix = "HOMESERVER_BACKUP_TARGET_" if target else "HOMESERVER_BACKUP_"
    repository, password = _repository(settings, target=target), env[prefix + "PASSWORD"]
    if not repository or not password:
        raise ValueError(prefix + "REPOSITORY and PASSWORD are required")
    if not shutil.which("restic"):
        raise DependencyError("restic is required")
    # Clear inherited Restic configuration: the explicit .env owns both repositories.
    process_env = {key: val for key, val in os.environ.items() if not key.startswith("RESTIC_")}
    process_env.update(
        RESTIC_PASSWORD=password,
        RESTIC_CACHE_DIR=env["HOMESERVER_BACKUP_STAGING_ROOT"] + "/restic-cache",
    )
    if target:
        process_env.update(RESTIC_FROM_PASSWORD=env["HOMESERVER_BACKUP_PASSWORD"])
    command = ["restic", "-r", repository]
    if repository.startswith("sftp:") and env["HOMESERVER_BACKUP_SSH_KEY_FILE"]:
        import shlex

        ssh = [
            "ssh",
            "-i",
            env["HOMESERVER_BACKUP_SSH_KEY_FILE"],
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-s",
            "%r@%h",
            "sftp",
        ]
        command.extend(["-o", "sftp.command=" + shlex.join(ssh)])
    return run_checked(command + args, env=process_env, cwd=cwd, timeout=7200)


def _repository(settings: Settings, *, target=False) -> str:
    env = settings.as_environment()
    repository = env[
        "HOMESERVER_BACKUP_TARGET_REPOSITORY" if target else "HOMESERVER_BACKUP_REPOSITORY"
    ]
    host, user = env["HOMESERVER_BACKUP_SSH_HOST"], env["HOMESERVER_BACKUP_SSH_USER"]
    if target and host and not repository.startswith("sftp:"):
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host) or (
            user and not re.fullmatch(r"[A-Za-z0-9_.-]+", user)
        ):
            raise ValueError("backup SSH host/user is invalid")
        if not repository or ":" in repository:
            raise ValueError("SSH backup repository must be a remote filesystem path")
        return "sftp:" + (user + "@" if user else "") + host + ":" + repository
    return repository


def _initialize_local(settings: Settings, *, target=False, copy_parameters=False):
    key = "HOMESERVER_BACKUP_TARGET_REPOSITORY" if target else "HOMESERVER_BACKUP_REPOSITORY"
    repository = _repository(settings, target=target)
    if not repository:
        raise ValueError(key + " is required")
    if ":" in repository:
        return  # Remote repositories must be explicitly prepared by the operator.
    path = Path(repository)
    if (path / "config").is_file():
        return
    if path.exists() and any(path.iterdir()):
        raise ValueError("refusing to initialize a nonempty unknown backup repository")
    args = ["init"]
    if copy_parameters:
        args += ["--from-repo", _repository(settings), "--copy-chunker-params"]
    _restic(settings, args, target=target)


def _copy_state(source: Path, target: Path):
    """Use SQLite's online backup API; omit WAL/SHM sidecars after materializing."""
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_symlink():
            raise ValueError("backup state contains a symlink; resolve it before capture")
        if item.is_dir():
            if item.name not in (".venv", "__pycache__"):
                _copy_state(item, destination)
        elif item.is_file():
            if item.name.endswith(("-wal", "-shm")):
                continue
            with item.open("rb") as handle:
                sqlite = handle.read(16) == b"SQLite format 3\x00"
            if sqlite:
                with (
                    sqlite3.connect(f"file:{item}?mode=ro", uri=True) as src,
                    sqlite3.connect(destination) as dst,
                ):
                    src.backup(dst)
                    if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("SQLite backup integrity check failed")
            else:
                shutil.copy2(item, destination)


def _checksums(root: Path):
    result = {}
    for item in sorted(root.rglob("*")):
        if item.is_file() and item.name != "backup-manifest.json":
            result[item.relative_to(root).as_posix()] = hashlib.sha256(
                item.read_bytes()
            ).hexdigest()
    return result


def verify_backup(settings: Settings) -> dict:
    _restic(settings, ["check"])
    snapshots = json.loads(_restic(settings, ["snapshots", "--json", "--tag", "homeserver"]).stdout)
    return {
        "verified": True,
        "snapshot_count": len(snapshots),
        "latest_snapshot": snapshots[-1]["id"] if snapshots else None,
    }


def _retention(settings: Settings, *, target=False):
    env = settings.as_environment()
    args = [
        "forget",
        "--tag",
        "homeserver",
        "--keep-last",
        str(max(1, int(env["HOMESERVER_BACKUP_KEEP_LAST"]))),
    ]
    for field, option in (("DAILY", "daily"), ("WEEKLY", "weekly"), ("MONTHLY", "monthly")):
        if int(env["HOMESERVER_BACKUP_KEEP_" + field]):
            args.extend(["--keep-" + option, env["HOMESERVER_BACKUP_KEEP_" + field]])
    _restic(settings, args + ["--prune"], target=target)
    _restic(settings, ["check"], target=target)


def create_backup(settings: Settings, env_file: Path, *, _already_locked: bool = False) -> dict:
    preflight(settings)
    env = settings.as_environment()
    if env["HOMESERVER_BACKUP_ENABLED"] != "true":
        raise ValueError("HOMESERVER_BACKUP_ENABLED must be true")
    source = Path(env["HOMESERVER_APPDATA_ROOT"])
    if not source.is_dir() or source.is_symlink():
        raise ValueError("application state directory is unavailable")
    media, staging = (
        Path(env["HOMESERVER_MEDIA_ROOT"]).resolve(),
        Path(env["HOMESERVER_BACKUP_STAGING_ROOT"]).resolve(),
    )
    if (
        source.resolve() == media
        or media in source.resolve().parents
        or source.resolve() in media.parents
        or source.resolve() == staging
        or source.resolve() in staging.parents
    ):
        raise ValueError("backup appdata/staging must be separate from media")
    started = time.monotonic()
    with nullcontext() if _already_locked else operation_lock(settings):
        _initialize_local(settings)
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="capture-", dir=staging) as temp:
            capture = Path(temp)
            with maintenance(settings, "backup"):
                _copy_state(source, capture / "appdata")
                (capture / "config").mkdir()
                shutil.copy2(env_file, capture / "config/operator.env")
                releases = Path(env["HOMESERVER_INSTALL_ROOT"]) / "releases"
                if releases.is_dir():
                    _copy_state(releases, capture / "releases")
                checksums = _checksums(capture)
                byte_count = sum(
                    item.stat().st_size for item in capture.rglob("*") if item.is_file()
                )
                if byte_count > int(env["HOMESERVER_BACKUP_STAGING_MAX_GIB"]) * 1024**3:
                    raise RuntimeError("backup capture exceeds staging limit")
                manifest = {
                    "schema_version": 1,
                    "media_included": False,
                    "bytes": byte_count,
                    "captured_media_uuid": env["HOMESERVER_MEDIA_UUID"],
                    "checksums": checksums,
                }
                atomic_write(
                    capture / "backup-manifest.json", json.dumps(manifest, indent=2) + "\n"
                )
                result = _restic(
                    settings, ["backup", "--json", "--tag", "homeserver", "."], cwd=capture
                )
                summaries = [
                    json.loads(line) for line in result.stdout.splitlines() if line.strip()
                ]
                summary = next(
                    (row for row in summaries if row.get("message_type") == "summary"), {}
                )
                snapshot = summary.get("snapshot_id")
                if not isinstance(snapshot, str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot):
                    raise RuntimeError("Restic did not confirm a completed snapshot")
                # Do not prune until the new snapshot has been checked.
                _restic(settings, ["check"])
            _retention(settings)
        receipt = {
            "snapshot_id": snapshot,
            "verified": True,
            "media_included": False,
            "bytes": byte_count,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "completed_at": time.time(),
        }
        encoded = json.dumps(receipt) + "\n"
        atomic_write(Path(settings.appdata_root) / "control/last-backup.json", encoded)
        mirror = Path(settings.run_root) / "last-backup.json"
        atomic_write(mirror, encoded, mode=0o640 if settings.environment == "prod" else 0o600)
        if settings.environment == "prod":
            os.chown(mirror, 0, int(settings.service_gid))
    return receipt


def restore_snapshot(
    settings: Settings, snapshot: str, target: Path, *, isolated: bool = True
) -> dict:
    if not isolated or not re.fullmatch(r"(?:latest|[0-9a-f]{8,64})", snapshot):
        raise ValueError("restore requires --isolated and a valid snapshot ID")
    absolute = target.absolute()
    if any(path.is_symlink() for path in (absolute, *absolute.parents)):
        raise ValueError("restore target must not contain a symlink")
    target = absolute.resolve()
    env = settings.as_environment()
    protected = [
        Path(env["HOMESERVER_" + key]).resolve()
        for key in (
            "MEDIA_ROOT",
            "APPDATA_ROOT",
            "TRANSCODE_ROOT",
            "BACKUP_STAGING_ROOT",
            "INSTALL_ROOT",
            "RUN_ROOT",
        )
    ]
    if target == Path("/") or any(
        target == root or root in target.parents or target in root.parents for root in protected
    ):
        raise ValueError("production roots are never valid restore targets")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("restore target must be an empty directory")
    # `cat snapshot` requires an ID (unlike `restore`, it does not accept latest).
    if snapshot == "latest":
        available = json.loads(
            _restic(settings, ["snapshots", "--json", "--tag", "homeserver"]).stdout
        )
        if not available:
            raise ValueError("backup repository contains no HomeServer snapshot")
        snapshot = max(available, key=lambda row: row["time"])["id"]
    # Validate password and ID before creating the requested target.
    _restic(settings, ["cat", "snapshot", snapshot])
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=target.parent) as temp:
        temporary = Path(temp)
        _restic(settings, ["restore", snapshot, "--target", str(temporary), "--verify"])
        if any(item.is_symlink() for item in temporary.rglob("*")):
            raise ValueError("backup contains a symlink; isolated restore cannot publish it")
        manifest_path = temporary / "backup-manifest.json"
        if not manifest_path.is_file():
            raise ValueError("snapshot is not a HomeServer backup")
        manifest = json.loads(manifest_path.read_text())
        for relative, digest in manifest["checksums"].items():
            item = temporary / relative
            if item.is_symlink() or not item.is_file() or temporary not in item.resolve().parents:
                raise ValueError("backup contains an invalid file path")
            if hashlib.sha256(item.read_bytes()).hexdigest() != digest:
                raise RuntimeError("restored file checksum mismatch")
            with item.open("rb") as handle:
                is_sqlite = handle.read(16) == b"SQLite format 3\x00"
            if is_sqlite:
                with sqlite3.connect(f"file:{item}?mode=ro", uri=True) as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("restored SQLite integrity check failed")
        control = temporary / "appdata/control"
        if not control.is_dir():
            raise ValueError("snapshot does not contain control state")
        atomic_write(
            control / "RECOVERY_MODE", f"admission_enabled=false\nrecovery_snapshot={snapshot}\n"
        )
        report = {
            "snapshot_id": snapshot,
            "admission_enabled": False,
            "media_reconciliation_required": True,
            "new_host_media_uuid_verified": False,
            "old_uuid_must_not_be_adopted": True,
        }
        atomic_write(temporary / "restore-report.json", json.dumps(report, indent=2) + "\n")
        if target.exists():
            target.rmdir()  # It was verified empty. Publish only the fully verified recovery tree.
        temporary.rename(target)
    return report


def copy_backups(settings: Settings) -> dict:
    """Restic locks both repositories; destination has independent encryption/retention."""
    if _repository(settings) == _repository(settings, target=True):
        raise ValueError("source and target backup repositories must differ")
    with operation_lock(settings):
        _initialize_local(settings, target=True, copy_parameters=True)
        _restic(settings, ["copy", "--from-repo", _repository(settings)], target=True)
        _restic(settings, ["check"], target=True)
        _retention(settings, target=True)
    return {"verified": True, "transport": "restic-copy", "independent_repository": True}
