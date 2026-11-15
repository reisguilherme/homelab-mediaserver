"""Manual checkpoint/restore for trusted, quiescent Linux fixture trees.

No runtime integration or operational permission is provided. The caller excludes
writers and retains the expected receipt separately from the opaque bundle.
Failed artifacts are deliberately retained for private inspection.

Precondition: bundle and destination live beneath caller-owned private 0700
parents. Restore preserves the source root mode, which may be more permissive;
the private parent must protect the complete restored tree throughout its life.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .checkpoint_manifest import (
    MAX_ARCHIVE_BYTES,
    MAX_MANIFEST_BYTES,
    CheckpointError,
    digest,
    inventory,
    member_name,
    preflight,
)

__all__ = [
    "CheckpointError",
    "CheckpointReceipt",
    "RestoreReport",
    "capture_checkpoint",
    "restore_checkpoint",
]


@dataclass(frozen=True)
class CheckpointReceipt:
    archive_sha256: str = field(repr=False)
    manifest_sha256: str = field(repr=False)
    scope: str = "isolated_native_fixture"
    evidence_kind: str = "quiescent_tree_checkpoint"
    partial_restart_safe: bool = False
    production_ready: bool = False


@dataclass(frozen=True)
class RestoreReport:
    bytes_verified: bool
    metadata_verified: bool
    sqlite_verified: bool
    database_count: int


def _guard(guard):
    try:
        guard()
    except Exception:
        raise CheckpointError("guard_failed") from None


def _tar(arguments, *, descriptor=None):
    environment = {"PATH": os.defpath, "LC_ALL": "C"}
    executable = shutil.which("tar", path=os.defpath)
    if executable is None:
        raise CheckpointError("gnu_tar_required")
    version = subprocess.run(
        [executable, "--version"], env=environment, capture_output=True, check=True, timeout=30
    )
    if not version.stdout.startswith(b"tar (GNU tar)"):
        raise CheckpointError("gnu_tar_required")
    result = subprocess.run(
        [
            executable,
            "--format=pax",
            "--acls",
            "--xattrs",
            "--xattrs-include=*",
            "--numeric-owner",
            *arguments,
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        pass_fds=() if descriptor is None else (descriptor,),
        timeout=300,
    )
    if result.returncode:
        raise CheckpointError("archive_command_failed")


def _open_regular(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    stream = os.fdopen(descriptor, "rb")
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        stream.close()
        raise CheckpointError("invalid_bundle_file")
    return stream


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _archive_unchanged(archive, path, identity):
    if (
        _file_identity(os.fstat(archive.fileno())) != identity
        or _file_identity(path.lstat()) != identity
    ):
        raise CheckpointError("archive_changed")


def _databases(root, entries, expected):
    names = set(expected)
    for name in names:
        if member_name(name) != name or name not in entries or entries[name]["kind"] != "file":
            raise CheckpointError("expected_database_invalid")
    for name, entry in entries.items():
        if entry["kind"] == "file":
            with (root / name).open("rb") as stream:
                if stream.read(16) == b"SQLite format 3\x00":
                    names.add(name)
    return tuple(sorted(names))


def _check_sqlite(root, names):
    if not names:
        return
    # SQLite can recover WAL and remove journals during integrity checks.
    # Only this disposable copy is ever opened by SQLite.
    with tempfile.TemporaryDirectory(prefix="checkpoint-sqlite-", dir="/tmp") as temporary:
        copied = Path(temporary) / "tree"
        shutil.copytree(root, copied, symlinks=True)
        for name in names:
            database = copied / name
            with database.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\x00":
                    raise CheckpointError("sqlite_invalid")
            connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=1)
            try:
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise CheckpointError("sqlite_invalid")
                connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
            finally:
                connection.close()


def _unchanged(root, before, identities):
    after, after_identities = inventory(root)
    if before != after or identities != after_identities:
        raise CheckpointError("source_changed")


def capture_checkpoint(
    source: Path,
    bundle: Path,
    *,
    guard: Callable[[], None],
    expected_databases: tuple[str, ...] = (),
) -> CheckpointReceipt:
    """Capture a closed tree under a private 0700 parent, excluding all writers."""
    try:
        _guard(guard)
        source, bundle = Path(source), Path(bundle)
        if source.is_symlink() or not source.is_dir():
            raise CheckpointError("invalid_source")
        source = source.resolve()
        resolved_bundle = bundle.resolve()
        if resolved_bundle.is_relative_to(source) or source.is_relative_to(resolved_bundle):
            raise CheckpointError("overlapping_paths")
        bundle.mkdir(mode=0o700)
        before, identities = inventory(source)
        databases = _databases(source, before, expected_databases)
        _guard(guard)
        _unchanged(source, before, identities)
        partial_archive = bundle / "archive.tar.partial"
        descriptor = os.open(partial_archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        _tar(["--create", "--file", str(partial_archive), "--directory", str(source), "."])
        _guard(guard)
        _unchanged(source, before, identities)
        _check_sqlite(source, databases)
        _guard(guard)
        _unchanged(source, before, identities)
        with _open_regular(partial_archive, MAX_ARCHIVE_BYTES) as archive:
            preflight(archive, before)
            archive_digest = digest(archive)
            os.fsync(archive.fileno())
        manifest = json.dumps(
            {"schema": 1, "entries": before, "databases": databases},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(manifest) > MAX_MANIFEST_BYTES:
            raise CheckpointError("size_limit")
        descriptor = os.open(
            bundle / "manifest.json.partial", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(manifest)
            stream.flush()
            os.fsync(stream.fileno())
        _guard(guard)
        _unchanged(source, before, identities)
        partial_archive.rename(bundle / "archive.tar")
        (bundle / "manifest.json.partial").rename(bundle / "manifest.json")
        _fsync_directory(bundle)
        return CheckpointReceipt(archive_digest, hashlib.sha256(manifest).hexdigest())
    except CheckpointError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        tarfile.TarError,
        subprocess.SubprocessError,
    ):
        raise CheckpointError("capture_failed") from None


def restore_checkpoint(
    bundle: Path, destination: Path, *, receipt: CheckpointReceipt, guard: Callable[[], None]
) -> RestoreReport:
    """Restore under a private 0700 parent after verifying trusted expected digests.

    Destination must not exist. Its final root mode matches the source, so its
    caller-owned private parent must remain in place to protect restored details.
    """
    try:
        _guard(guard)
        bundle, destination = Path(bundle), Path(destination)
        if destination.exists() or destination.is_symlink():
            raise CheckpointError("destination_exists")
        with _open_regular(bundle / "manifest.json", MAX_MANIFEST_BYTES) as stream:
            raw = stream.read()
        if hashlib.sha256(raw).hexdigest() != receipt.manifest_sha256:
            raise CheckpointError("manifest_digest_mismatch")
        manifest = json.loads(raw)
        if manifest["schema"] != 1:
            raise CheckpointError("manifest_schema")
        entries, databases = manifest["entries"], manifest["databases"]
        with _open_regular(bundle / "archive.tar", MAX_ARCHIVE_BYTES) as archive:
            archive_identity = _file_identity(os.fstat(archive.fileno()))
            if digest(archive) != receipt.archive_sha256:
                raise CheckpointError("archive_digest_mismatch")
            preflight(archive, entries)
            _guard(guard)
            _archive_unchanged(archive, bundle / "archive.tar", archive_identity)
            destination.mkdir(mode=0o700)
            # Use the same inode already hashed and preflighted even if someone
            # replaces the bundle pathname. In-place writes are checked after.
            _tar(
                [
                    "--extract",
                    "--same-owner",
                    "--same-permissions",
                    "--delay-directory-restore",
                    "--file",
                    f"/proc/self/fd/{archive.fileno()}",
                    "--directory",
                    str(destination),
                ],
                descriptor=archive.fileno(),
            )
            _guard(guard)
            _archive_unchanged(archive, bundle / "archive.tar", archive_identity)
            if digest(archive) != receipt.archive_sha256:
                raise CheckpointError("archive_changed")
        restored, identities = inventory(destination)
        if restored != entries:
            raise CheckpointError("restored_tree_mismatch")
        names = _databases(destination, restored, tuple(databases))
        if names != tuple(databases):
            raise CheckpointError("database_set_mismatch")
        _check_sqlite(destination, names)
        _guard(guard)
        _unchanged(destination, restored, identities)
        return RestoreReport(True, True, True, len(names))
    except CheckpointError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        tarfile.TarError,
        subprocess.SubprocessError,
    ):
        raise CheckpointError("restore_failed") from None
