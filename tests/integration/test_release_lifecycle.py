import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest

from homeserver_common.env import load_settings
from homeserver_common.release import (
    deploy_release,
    refresh_runtime_configuration,
    rollback_release,
)
from homeserver_common.render import IMAGES


class DockerBoundary:
    def __init__(self, *, startup_failure=False, digest_failure=False, ready_failure=False):
        self.startup_failure = startup_failure
        self.digest_failure = digest_failure
        self.ready_failure = ready_failure
        self.stops = 0

    def pull_and_verify(self, references, release):
        if self.digest_failure:
            raise ValueError("effective image digest mismatch")

    def activate(self, compose):
        assert compose.is_file()
        services = json.loads(compose.read_text())["services"]
        assert all("@sha256:" in value["image"] for value in services.values())
        if self.startup_failure:
            raise RuntimeError("startup failed")

    def stop(self, compose):
        self.stops += 1

    def ready(self, settings, release):
        if self.ready_failure:
            raise RuntimeError("readiness failed")
        return True


def fixture(tmp_path: Path, *, requires_backup=False, schema=1):
    env = tmp_path / ".env"
    env.write_text(f"HOMESERVER_ENVIRONMENT=dev\nHOMESERVER_INSTALL_ROOT={tmp_path / 'install'}\n")
    settings = load_settings(env, mode="dev")
    release = "a" * 40
    payloads = {
        "deploy/compose.yaml": b"services: {}\n",
        "scripts/check-mount.sh": b"#!/bin/bash\nexit 0\n",
        "scripts/smoke.sh": b"#!/bin/bash\nexit 0\n",
        "services/control/src/homeserver_control/persistence/migrations/001_initial.sql": (
            b"CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT);\n"
            b"CREATE TABLE tombstones(id TEXT);\n"
        ),
    }
    artifact = tmp_path / "release.tar"
    with tarfile.open(artifact, "w") as archive:
        for path, data in payloads.items():
            member = tarfile.TarInfo(path)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    references = {
        name: image.split("@")[0].split(":")[0] + "@sha256:" + "b" * 64
        for name, image in IMAGES.items()
    }
    manifest = tmp_path / "release.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "git_commit": release,
                "database_schema": schema,
                "database_compatibility": {"minimum": 0, "maximum": schema},
                "config_version": 1,
                "tools": {"python": "3.12", "uv": "0.8.0"},
                "requires_backup": requires_backup,
                "images": {name: "sha256:" + "b" * 64 for name in references},
                "image_references": references,
                "config_checksums": {
                    "deploy/compose.yaml": hashlib.sha256(
                        payloads["deploy/compose.yaml"]
                    ).hexdigest()
                },
                "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
        )
    )
    return env, settings, release, artifact, manifest


def test_deploy_activates_images_migrates_and_is_idempotent(tmp_path: Path) -> None:
    env, settings, release, artifact, manifest = fixture(tmp_path)
    result = deploy_release(settings, env, release, artifact, manifest, backend=DockerBoundary())
    assert result["state"] == "active"
    current = Path(settings.install_root) / "current"
    assert (current / "COMMIT").read_text().strip() == release
    with sqlite3.connect(Path(settings.appdata_root) / "control/control.sqlite") as conn:
        assert conn.execute("SELECT version FROM schema_migrations").fetchall() == [(1,)]
    again = deploy_release(settings, env, release, artifact, manifest, backend=DockerBoundary())
    assert again["changed"] is False
    assert not (Path(settings.run_root) / "maintenance").exists()


def test_image_mismatch_and_required_backup_fail_before_activation(tmp_path: Path) -> None:
    env, settings, release, artifact, manifest = fixture(tmp_path)
    with pytest.raises(ValueError, match="digest"):
        deploy_release(
            settings, env, release, artifact, manifest, backend=DockerBoundary(digest_failure=True)
        )
    assert not (Path(settings.install_root) / "current").exists()
    data = json.loads(manifest.read_text())
    data["requires_backup"] = True
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="backup"):
        deploy_release(settings, env, release, artifact, manifest, backend=DockerBoundary())
    assert not (Path(settings.install_root) / "current").exists()


def test_failed_startup_enters_recovery_and_schema_downgrade_is_blocked(tmp_path: Path) -> None:
    env, settings, release, artifact, manifest = fixture(tmp_path)
    with pytest.raises(RuntimeError, match="startup"):
        deploy_release(
            settings, env, release, artifact, manifest, backend=DockerBoundary(startup_failure=True)
        )
    assert (Path(settings.appdata_root) / "control/RECOVERY_MODE").exists()
    current = Path(settings.install_root) / "current"
    assert not current.exists()
    target = Path(settings.install_root) / "releases" / release
    data = json.loads((target / "release.json").read_text())
    data["database_compatibility"]["maximum"] = 0
    (target / "release.json").write_text(json.dumps(data))
    with pytest.raises(ValueError, match="schema"):
        rollback_release(settings, env, release, backend=DockerBoundary())
    with sqlite3.connect(Path(settings.appdata_root) / "control/control.sqlite") as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1


def test_configuration_refresh_keeps_release_pins_and_is_idempotent(tmp_path: Path) -> None:
    env, settings, release, artifact, manifest = fixture(tmp_path)
    deploy_release(settings, env, release, artifact, manifest, backend=DockerBoundary())
    env.write_text(env.read_text() + "HOMESERVER_DOWNLOAD_MAX_ACTIVE=2\n")
    changed = load_settings(env, mode="dev")
    result = refresh_runtime_configuration(changed, env, backend=DockerBoundary())
    assert result["changed"] is True
    stack = json.loads((Path(settings.install_root) / "shared/compose.json").read_text())
    assert (
        stack["services"]["control-worker"]["environment"]["HOMESERVER_DOWNLOAD_MAX_ACTIVE"] == "2"
    )
    assert all(
        service["labels"]["homeserver.release"] == release for service in stack["services"].values()
    )
    assert refresh_runtime_configuration(changed, env, backend=DockerBoundary())["changed"] is False
    current = Path(settings.install_root) / "current"
    assert load_settings(current / "operator.env", mode="dev").download_max_active == 2
    saved = json.loads((current / "runtime-compose.json").read_text())
    assert (
        saved["services"]["control-worker"]["environment"]["HOMESERVER_DOWNLOAD_MAX_ACTIVE"] == "2"
    )


def test_compatible_rollback_preserves_database_and_restores_configuration(tmp_path: Path) -> None:
    env, settings, first, artifact, manifest = fixture(tmp_path)
    deploy_release(settings, env, first, artifact, manifest, backend=DockerBoundary())
    database = Path(settings.appdata_root) / "control/control.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO tombstones VALUES ('deleted-item')")
    second = "c" * 40
    data = json.loads(manifest.read_text())
    data["git_commit"] = second
    manifest.write_text(json.dumps(data))
    env.write_text(env.read_text() + "HOMESERVER_DOWNLOAD_MAX_ACTIVE=2\n")
    newer_settings = load_settings(env, mode="dev")
    deploy_release(newer_settings, env, second, artifact, manifest, backend=DockerBoundary())
    result = rollback_release(newer_settings, env, first, backend=DockerBoundary())
    assert result["database_schema"] == 1
    assert load_settings(env, mode="dev").download_max_active == settings.download_max_active
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM tombstones").fetchall() == [("deleted-item",)]


def test_release_retention_protects_current_previous_and_unknown_directories(
    tmp_path: Path,
) -> None:
    env, settings, release, artifact, manifest = fixture(tmp_path)
    env.write_text(env.read_text() + "HOMESERVER_RELEASE_KEEP_COUNT=2\n")
    settings = load_settings(env, mode="dev")
    deploy_release(settings, env, release, artifact, manifest, backend=DockerBoundary())
    unknown = Path(settings.install_root) / "releases/operator-owned"
    unknown.mkdir()
    (unknown / "keep").write_text("keep")
    data = json.loads(manifest.read_text())
    for letter in ("c", "d"):
        data["git_commit"] = letter * 40
        manifest.write_text(json.dumps(data))
        deploy_release(settings, env, letter * 40, artifact, manifest, backend=DockerBoundary())
    releases = Path(settings.install_root) / "releases"
    assert not (releases / release).exists()
    assert (releases / ("c" * 40)).exists()
    assert (releases / ("d" * 40)).exists()
    assert (unknown / "keep").read_text() == "keep"


def test_runtime_refresh_failure_stops_candidate_writers_and_gates_recovery(tmp_path):
    env, settings, release, artifact, manifest = fixture(tmp_path)
    deploy_release(settings, env, release, artifact, manifest, backend=DockerBoundary())
    env.write_text(env.read_text() + "HOMESERVER_DOWNLOAD_MAX_ACTIVE=2\n")
    changed = load_settings(env, mode="dev")
    backend = DockerBoundary(ready_failure=True)
    with pytest.raises(RuntimeError, match="readiness"):
        refresh_runtime_configuration(changed, env, backend=backend)
    assert backend.stops >= 1
    assert (Path(settings.appdata_root) / "control/RECOVERY_MODE").exists()
    assert (Path(settings.run_root) / "maintenance").exists()


def test_rollback_failure_stops_candidate_writers(tmp_path):
    env, settings, release, artifact, manifest = fixture(tmp_path)
    deploy_release(settings, env, release, artifact, manifest, backend=DockerBoundary())
    backend = DockerBoundary(ready_failure=True)
    with pytest.raises(RuntimeError, match="readiness"):
        rollback_release(settings, env, release, backend=backend)
    assert backend.stops >= 1
    assert (Path(settings.appdata_root) / "control/RECOVERY_MODE").exists()


def test_rollback_preserves_current_secret_file_after_password_rotation(tmp_path):
    env, settings, release, artifact, manifest = fixture(tmp_path)
    env.write_text(env.read_text() + "HOMESERVER_QBIT_PASSWORD=initial-password\n")
    settings = load_settings(env, mode="dev")
    deploy_release(settings, env, release, artifact, manifest, backend=DockerBoundary())
    secret = tmp_path / "rotated password"
    secret.write_text("rotated-password\n")
    env.write_text(
        env.read_text().replace(
            "HOMESERVER_QBIT_PASSWORD=initial-password", f'HOMESERVER_QBIT_PASSWORD_FILE="{secret}"'
        )
    )
    current = load_settings(env, mode="dev")
    rollback_release(current, env, release, backend=DockerBoundary())
    assert load_settings(env, mode="dev").qbit_password == "rotated-password"
    assert f'HOMESERVER_QBIT_PASSWORD_FILE="{secret}"' in env.read_text()
