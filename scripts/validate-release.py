#!/usr/bin/env python3
"""Validate a release manifest before the supervisor is touched."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path, PurePosixPath


def extract_artifact(artifact_path: Path, destination: Path) -> None:
    """Extract a release tarball without following archive-controlled paths."""

    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(artifact_path, "r:*") as archive:
        members = archive.getmembers()
        for member in members:
            name = member.name
            path = PurePosixPath(name)
            if (
                not name
                or "\\" in name
                or path.is_absolute()
                or ":" in path.parts[0]
                or ".." in path.parts
            ):
                raise ValueError(f"unsafe archive path: {name}")
            target = (destination / Path(*path.parts)).resolve()
            if target != destination and destination not in target.parents:
                raise ValueError(f"unsafe archive path: {name}")
            if member.issym() or member.islnk():
                raise ValueError(f"links are not allowed in release archive: {name}")
            if not member.isdir() and not member.isfile():
                raise ValueError(f"special files are not allowed in release archive: {name}")

        for member in members:
            target = (destination / Path(*PurePosixPath(member.name).parts)).resolve()
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError(f"archive member has no data: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.name.startswith("scripts/") else member.mode & 0o777)


def validate(manifest_path: Path, artifact_path: Path, release: str) -> None:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("release manifest is not valid JSON") from error
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("unsupported release schema")
    if not isinstance(manifest.get("git_commit"), str) or manifest["git_commit"] != release:
        raise ValueError("manifest commit mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise ValueError("release must be a lowercase 40-character SHA")
    images = manifest.get("images")
    if not isinstance(images, dict) or not images:
        raise ValueError("release manifest must contain image digests")
    for name, digest in images.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            raise ValueError("release image map is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"image {name} is not pinned by digest")
    if not isinstance(manifest.get("database_schema"), int) or manifest["database_schema"] < 1:
        raise ValueError("database schema is invalid")
    if not isinstance(manifest.get("requires_backup"), bool):
        raise ValueError("requires_backup must be boolean")
    if not isinstance(manifest.get("config_checksums"), dict):
        raise ValueError("config_checksums must be an object")
    expected = manifest.get("artifact_sha256")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("artifact checksum is invalid")
    digest = hashlib.sha256()
    with artifact_path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError("artifact checksum mismatch")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--release", required=True)
    parser.add_argument("--extract-to", type=Path)
    args = parser.parse_args()
    try:
        validate(args.manifest, args.artifact, args.release)
        if args.extract_to is not None:
            extract_artifact(args.artifact, args.extract_to)
    except (OSError, tarfile.TarError, ValueError) as error:
        print(str(error), flush=True)
        return 1
    print("release manifest and artifact validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
