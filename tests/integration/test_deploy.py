import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from uuid import uuid4

import pytest


def _bash() -> str:
    candidates = [r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    pytest.skip("bash is required")


def _bash_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if len(value) > 1 and value[1] == ":" else value


@pytest.fixture
def local_tmp():
    path = Path(".runtime") / f"test-deploy-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_deploy_rejects_manifest_without_image_digests(local_tmp: Path) -> None:
    tmp_path = local_tmp
    bash = _bash()
    artifact = tmp_path / "release.tar"
    artifact.write_bytes(b"release")
    manifest = tmp_path / "release.json"
    manifest.write_text(
        '{"schema_version":1,"git_commit":"'
        + "1" * 40
        + (
            '","database_schema":3,"requires_backup":false,"images":{},'
            '"config_checksums":{},"artifact_sha256":"'
        )
        + "0" * 64
        + '"}',
        encoding="utf-8",
    )
    config = tmp_path / "deploy.env"

    config.write_text(f"HOMESERVER_ROOT={_bash_path(tmp_path / 'homeserver')}\n", encoding="utf-8")
    result = subprocess.run(
        [
            bash,
            _bash_path(Path("scripts/deploy.sh")),
            "--release",
            "1" * 40,
            "--artifact",
            _bash_path(artifact),
            "--config",
            _bash_path(config),
            "--manifest",
            _bash_path(manifest),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "homeserver" / "current").exists()


@pytest.mark.skipif(os.name == "nt", reason="release extraction test runs in Linux")
def test_deploy_extracts_release_before_switching_current(local_tmp: Path) -> None:
    tmp_path = local_tmp
    artifact = tmp_path / "release.tar"
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "deploy" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (source / "scripts" / "check-mount.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (source / "scripts" / "smoke.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    with tarfile.open(artifact, "w") as archive:
        archive.add(source / "deploy", arcname="deploy")
        archive.add(source / "scripts", arcname="scripts")

    release = "2" * 40
    manifest = tmp_path / "release.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "git_commit": release,
                "database_schema": 3,
                "requires_backup": False,
                "images": {"control": "sha256:" + "a" * 64},
                "config_checksums": {},
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "deploy.env"
    config.write_text(f"HOMESERVER_ROOT={_bash_path(tmp_path / 'homeserver')}\n", encoding="utf-8")

    result = subprocess.run(
        [
            _bash(),
            _bash_path(Path("scripts/deploy.sh")),
            "--release",
            release,
            "--artifact",
            _bash_path(artifact),
            "--config",
            _bash_path(config),
            "--manifest",
            _bash_path(manifest),
        ],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    current = tmp_path / "homeserver" / "current"
    assert current.is_symlink()
    assert (current / "deploy" / "compose.yaml").exists()
    assert (current / "COMMIT").read_text(encoding="utf-8").strip() == release
