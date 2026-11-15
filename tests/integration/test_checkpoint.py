"""Linux-only checkpoint fixtures: never use application or host data."""

import dataclasses
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

from homeserver_control import checkpoint

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires Linux GNU tar metadata")


@pytest.fixture
def fixture_root():
    with tempfile.TemporaryDirectory(prefix="checkpoint-test-", dir="/tmp") as name:
        yield Path(name)


def noop():
    pass


def source_tree(root):
    source = root / "source"
    source.mkdir(mode=0o700)
    (source / "directory").mkdir(mode=0o750)
    (source / "directory" / "binary").write_bytes(b"private-token\x00\xff")
    os.chmod(source / "directory" / "binary", 0o640)
    os.link(source / "directory" / "binary", source / "alias")
    (source / "link").symlink_to("directory/binary")
    os.setxattr(source / "directory" / "binary", "user.binary", b"\x00\xff\x01")
    for path in [source, *source.rglob("*")]:
        os.utime(path, ns=(1700000000123456789, 1700000000123456789), follow_symlinks=False)
    return source


def snapshot(source):
    result = {}
    for path in [source, *sorted(source.rglob("*"))]:
        info = path.lstat()
        result[str(path.relative_to(source))] = (
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
            info.st_mtime_ns,
            {
                key: os.getxattr(path, key, follow_symlinks=False)
                for key in os.listxattr(path, follow_symlinks=False)
            },
            path.read_bytes() if path.is_file() and not path.is_symlink() else None,
            os.readlink(path) if path.is_symlink() else None,
        )
    return result


def test_roundtrip_preserves_bytes_metadata_links_and_private_evidence(fixture_root):
    source = source_tree(fixture_root)
    before = snapshot(source)
    receipt = checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=noop)
    assert snapshot(source) == before
    opaque = fixture_root / "opaque"
    shutil.copytree(fixture_root / "bundle", opaque)
    result = checkpoint.restore_checkpoint(
        opaque, fixture_root / "restored", receipt=receipt, guard=noop
    )
    assert snapshot(fixture_root / "restored") == before
    assert (fixture_root / "restored/alias").stat().st_ino == (
        fixture_root / "restored/directory/binary"
    ).stat().st_ino
    assert result.bytes_verified and result.metadata_verified and result.sqlite_verified
    assert result.database_count == 0
    assert receipt.scope == "isolated_native_fixture"
    assert not receipt.production_ready and not receipt.partial_restart_safe
    assert "private-token" not in repr(receipt) + repr(result)
    assert "directory" not in repr(receipt) + repr(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        receipt.archive_sha256 = "0" * 64
    assert stat.S_IMODE((fixture_root / "bundle").stat().st_mode) == 0o700
    for path in (fixture_root / "bundle").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.geteuid() != 0, reason="distinct ownership/ACL requires root fixture run")
def test_root_preserves_distinct_ownership_and_real_posix_acl(fixture_root):
    source = source_tree(fixture_root)
    path = source / "directory/binary"
    os.chown(path, 12345, 23456)
    # Linux POSIX ACL xattr v2: named user 34567 with read, mask read.
    acl = struct.pack("<I", 2) + b"".join(
        struct.pack("<HHI", tag, permission, identifier)
        for tag, permission, identifier in [
            (1, 6, 0xFFFFFFFF),
            (2, 4, 34567),
            (4, 0, 0xFFFFFFFF),
            (16, 4, 0xFFFFFFFF),
            (32, 0, 0xFFFFFFFF),
        ]
    )
    os.setxattr(path, "system.posix_acl_access", acl)
    before = snapshot(source)
    receipt = checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=noop)
    checkpoint.restore_checkpoint(
        fixture_root / "bundle", fixture_root / "restored", receipt=receipt, guard=noop
    )
    assert snapshot(fixture_root / "restored") == before
    assert (fixture_root / "restored/alias").stat().st_uid == 12345
    assert os.getxattr(fixture_root / "restored/alias", "system.posix_acl_access") == acl


def test_committed_wal_is_preserved_and_checked_without_modifying_reference(fixture_root):
    source = source_tree(fixture_root)
    code = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('CREATE TABLE evidence(value TEXT)')
connection.execute("INSERT INTO evidence VALUES ('committed-wal-row')")
connection.commit()
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", code, str(source / "opaque")], check=True)
    assert (source / "opaque-wal").stat().st_size > 0
    before = snapshot(source)
    receipt = checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=noop)
    restored = fixture_root / "restored"
    report = checkpoint.restore_checkpoint(
        fixture_root / "bundle", restored, receipt=receipt, guard=noop
    )
    assert report.database_count == 1 and report.sqlite_verified
    assert snapshot(source) == snapshot(restored) == before
    shutil.copytree(restored, fixture_root / "disposable")
    with sqlite3.connect(fixture_root / "disposable/opaque") as database:
        assert database.execute("SELECT value FROM evidence").fetchall() == [("committed-wal-row",)]
    assert snapshot(restored) == before


@pytest.mark.parametrize("damaged_header", [False, True])
def test_expected_database_corruption_blocks_capture(fixture_root, damaged_header):
    source = source_tree(fixture_root)
    (source / "secret-database").write_bytes(
        (b"damaged header!!!" if damaged_header else b"SQLite format 3\x00") + b"\x00" * 100
    )
    with pytest.raises(checkpoint.CheckpointError) as error:
        checkpoint.capture_checkpoint(
            source, fixture_root / "bundle", guard=noop, expected_databases=("secret-database",)
        )
    assert "secret" not in str(error.value)
    assert source.exists()


@pytest.mark.parametrize("boundary", [2, 3, 4, 5])
def test_source_mutation_at_guard_boundary_fails_closed(fixture_root, boundary):
    source = source_tree(fixture_root)
    calls = 0

    def mutating_guard():
        nonlocal calls
        calls += 1
        if calls == boundary:
            (source / "alias").write_bytes(b"changed")

    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=mutating_guard)
    assert source.exists()
    assert not (fixture_root / "bundle/manifest.json").exists()


@pytest.mark.parametrize("operation", ["capture", "restore"])
def test_guard_exception_is_sanitized_and_retains_source(fixture_root, operation):
    source = source_tree(fixture_root)

    def rejected():
        raise RuntimeError("secret-token-and-private-path")

    if operation == "capture":

        def action():
            checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=rejected)
    else:
        receipt = checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=noop)

        def action():
            checkpoint.restore_checkpoint(
                fixture_root / "bundle", fixture_root / "restored", receipt=receipt, guard=rejected
            )

    with pytest.raises(checkpoint.CheckpointError) as error:
        action()
    assert "secret" not in str(error.value)
    assert source.exists()


def test_restore_rejects_existing_destination_without_touching_it(fixture_root):
    source = source_tree(fixture_root)
    receipt = checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=noop)
    target = fixture_root / "restored"
    target.mkdir()
    (target / "marker").write_text("keep")
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.restore_checkpoint(fixture_root / "bundle", target, receipt=receipt, guard=noop)
    assert (target / "marker").read_text() == "keep"


@pytest.mark.parametrize("kind", ["external_symlink", "unaccounted_hardlink", "fifo"])
def test_capture_rejects_open_scope(fixture_root, kind):
    source = source_tree(fixture_root)
    if kind == "external_symlink":
        (source / "bad").symlink_to("../../private-secret")
    elif kind == "unaccounted_hardlink":
        os.link(source / "alias", fixture_root / "outside")
    else:
        os.mkfifo(source / "bad")
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=noop)


@pytest.mark.parametrize("kind", ["archive_corrupt", "archive_truncated", "manifest"])
def test_expected_receipt_rejects_bundle_tampering(fixture_root, kind):
    source = source_tree(fixture_root)
    bundle = fixture_root / "bundle"
    receipt = checkpoint.capture_checkpoint(source, bundle, guard=noop)
    path = bundle / ("manifest.json" if kind == "manifest" else "archive.tar")
    if kind == "archive_truncated":
        path.write_bytes(path.read_bytes()[:512])
    else:
        content = bytearray(path.read_bytes())
        content[100] ^= 1
        path.write_bytes(content)
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.restore_checkpoint(
            bundle, fixture_root / "restored", receipt=receipt, guard=noop
        )
    assert not (fixture_root / "restored").exists()


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "extra",
        "missing",
        "traversal",
        "pivot",
        "external_hardlink",
        "special",
        "oversize",
    ],
)
def test_safe_preflight_rejects_malicious_archive_with_trusted_test_digest(fixture_root, kind):
    source = source_tree(fixture_root)
    bundle = fixture_root / "bundle"
    receipt = checkpoint.capture_checkpoint(source, bundle, guard=noop)
    archive = bundle / "archive.tar"
    with tarfile.open(archive, "r:") as stream:
        members = [
            (member, stream.extractfile(member).read() if member.isfile() else None)
            for member in stream.getmembers()
        ]
    if kind == "missing":
        members = members[:-1]
    elif kind == "duplicate":
        members.append(members[-1])
    else:
        member = tarfile.TarInfo("unexpected")
        payload = b""
        if kind in {"external_hardlink", "special", "oversize"}:
            index = next(i for i, (item, _) in enumerate(members) if item.isfile())
            member.name = members[index][0].name
            members.pop(index)
        if kind == "traversal":
            member.name = "../escape"
        elif kind == "pivot":
            member.name = "link/escape"
            # A trusted-test manifest reaches the ancestry check instead of
            # stopping at the extra-member check. Never accept external receipts.
            manifest_path = bundle / "manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            manifest["entries"][member.name] = {
                "kind": "file",
                "size": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
                "hardlink_group": member.name,
            }
            manifest_path.write_bytes(json.dumps(manifest).encode())
            receipt = dataclasses.replace(
                receipt, manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            )
        elif kind == "external_hardlink":
            member.type, member.linkname = tarfile.LNKTYPE, "../escape"
        elif kind == "special":
            member.type = tarfile.FIFOTYPE
        elif kind == "oversize":
            member.size = 9 * 1024**3
        members.append((member, payload))
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as stream:
        for member, payload in members:
            if kind == "oversize" and member.size == 9 * 1024**3:
                stream.fileobj.write(member.tobuf(format=tarfile.PAX_FORMAT))
                break
            stream.addfile(member, io.BytesIO(payload) if payload is not None else None)
    receipt = dataclasses.replace(
        receipt, archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.restore_checkpoint(
            bundle, fixture_root / "restored", receipt=receipt, guard=noop
        )
    assert not (fixture_root / "escape").exists()
    assert not (fixture_root / "restored").exists()


def test_manifest_is_canonical_and_private(fixture_root):
    source = source_tree(fixture_root)
    checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=noop)
    raw = (fixture_root / "bundle/manifest.json").read_bytes()
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()


def test_tar_uses_controlled_environment_and_handles_option_like_unicode_names(
    fixture_root,
    monkeypatch,
):
    source = source_tree(fixture_root)
    (source / "--checkpoint-action=exec=private\nmaçã").write_bytes(b"safe")
    tools = fixture_root / "tools"
    tools.mkdir()
    (tools / "tar").write_text("#!/bin/sh\nexit 91\n")
    (tools / "tar").chmod(0o700)
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setenv("TAR_OPTIONS", "--invalid-untrusted-option")
    monkeypatch.setenv("TAPE", "/nonexistent/private")
    receipt = checkpoint.capture_checkpoint(source, fixture_root / "bundle", guard=noop)
    checkpoint.restore_checkpoint(
        fixture_root / "bundle", fixture_root / "restored", receipt=receipt, guard=noop
    )
    assert snapshot(source) == snapshot(fixture_root / "restored")


@pytest.mark.parametrize("in_place", [False, True])
def test_archive_replaced_at_guard_boundary_is_rejected_before_extraction(fixture_root, in_place):
    source = source_tree(fixture_root)
    bundle = fixture_root / "bundle"
    receipt = checkpoint.capture_checkpoint(source, bundle, guard=noop)
    archive = bundle / "archive.tar"
    calls = 0

    def replacing_guard():
        nonlocal calls
        calls += 1
        if calls == 2:
            if in_place:
                with archive.open("r+b") as stream:
                    stream.seek(100)
                    stream.write(b"altered")
            else:
                archive.rename(bundle / "retained.tar")
                archive.write_bytes(b"changed")

    with pytest.raises(checkpoint.CheckpointError):
        checkpoint.restore_checkpoint(
            bundle, fixture_root / "restored", receipt=receipt, guard=replacing_guard
        )
    assert not (fixture_root / "restored").exists()
