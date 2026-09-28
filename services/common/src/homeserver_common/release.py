"""Build and activate authenticated immutable runtimes without downgrading state."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import tarfile
import time
from contextlib import nullcontext
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from .backup import create_backup, maintenance, operation_lock
from .env import load_settings, parse_env, serialize_env
from .install import preflight, record_generated_file, run_checked, write_operator_env
from .render import IMAGES, PROJECT_ROOT, atomic_write, render_stack, write_stack
from .settings import FIELDS, Settings


def _validator():
    spec = importlib.util.spec_from_file_location(
        "homeserver_validate_release", PROJECT_ROOT / "scripts/validate-release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _schema(database: Path) -> int:
    if not database.exists():
        return 0
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        if not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='schema_migrations'"
        ).fetchone():
            raise ValueError("database schema is unknown; recovery is required")
        return connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]


def _compatible(manifest: dict, schema: int):
    bounds = manifest["database_compatibility"]
    if not bounds["minimum"] <= schema <= bounds["maximum"]:
        raise ValueError("database schema is incompatible; isolated recovery is required")


def _recovery(database: Path, reason: str):
    atomic_write(database.parent / "RECOVERY_MODE", f"admission_enabled=false\nreason={reason}\n")


def _save_runtime_configuration(target: Path, settings: Settings, compose: Path):
    atomic_write(target / "operator.env", serialize_env(settings.as_environment()))
    atomic_write(target / "runtime-compose.json", compose.read_bytes())


def _health_url(settings: Settings) -> str:
    address = (
        settings.tailscale_bind_ip
        if settings.access_mode in ("tailscale", "both")
        else settings.lan_bind_ip
    )
    if ":" in address:
        address = "[" + address + "]"
    return f"http://{address}:{settings.control_port}/health/ready"


def _rollback_configuration(settings: Settings, snapshot: Path, env_file: Path):
    historical = load_settings(snapshot, mode=settings.environment)
    values = dict(historical.values)
    for name, field in FIELDS.items():
        if field.secret:
            values[name] = settings.values[name]
    restored = Settings(values)
    output = restored.as_environment()
    current = parse_env(env_file.read_text())
    for name, field in FIELDS.items():
        if field.secret:
            key = "HOMESERVER_" + name.upper()
            if key + "_FILE" in current:
                output.pop(key, None)
                output[key + "_FILE"] = current[key + "_FILE"]
            elif key in current:
                output[key] = current[key]
    return restored, serialize_env(output)


def _migrate(database: Path, release_root: Path, target_schema: int):
    current = _schema(database)
    if current > target_schema:
        raise ValueError("database schema downgrade is prohibited")
    migrations = sorted(
        (release_root / "services/control/src/homeserver_control/persistence/migrations").glob(
            "*.sql"
        )
    )
    versions = {int(path.stem.split("_", 1)[0]): path for path in migrations}
    if any(version not in versions for version in range(current + 1, target_schema + 1)):
        raise ValueError("release is missing a required database migration")
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as connection:
        for version in range(current + 1, target_schema + 1):
            sql = versions[version].read_text()
            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + sql
                    + "\nINSERT INTO schema_migrations(version, applied_at) "
                    + f"VALUES ({version}, datetime('now'));\nCOMMIT;"
                )
            except sqlite3.Error:
                connection.rollback()
                raise RuntimeError("database migration failed; recovery is required") from None
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("database integrity check failed")


class DockerBackend:
    def __init__(self):
        self.expected_ids = {}
        self.references = {}
        self.compose = None

    def pull_and_verify(self, references: dict[str, str], release: str):
        self.references = references
        for service, reference in references.items():
            run_checked(["docker", "pull", reference], timeout=600)
            inspection = json.loads(run_checked(["docker", "image", "inspect", reference]).stdout)[
                0
            ]
            if reference not in inspection.get("RepoDigests", []):
                raise ValueError("effective image digest mismatch")
            if service in (
                "control-api",
                "control-worker",
                "download-gateway",
                "operator",
                "telemetry",
            ):
                if (
                    inspection.get("Config", {})
                    .get("Labels", {})
                    .get("org.opencontainers.image.revision")
                    != release
                ):
                    raise ValueError("project image revision does not match release")
            self.expected_ids[service] = inspection["Id"]

    def _compose(self, compose: Path, args: list[str]):
        environment = {
            key: val
            for key, val in os.environ.items()
            if not key.startswith(("HOMESERVER_", "COMPOSE_"))
        }
        return run_checked(
            ["docker", "compose", "--env-file", os.devnull, "-f", str(compose), *args],
            env=environment,
            timeout=600,
        )

    def activate(self, compose: Path):
        self.compose = compose
        self._compose(compose, ["config", "--quiet"])
        self._compose(compose, ["up", "-d", "--no-build"])

    def stop(self, compose: Path):
        self._compose(compose, ["stop"])

    def configure(self, settings: Settings, env_file: Path):
        from .cli import configure_native

        return configure_native(settings, env_file)

    def ready(self, settings: Settings, release: str):
        services = {
            key: val
            for key, val in render_stack(settings, images=self.references)["services"].items()
            if not val.get("profiles")
        }
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                ids = self._compose(self.compose, ["ps", "--quiet"]).stdout.split()
                inspections = (
                    json.loads(run_checked(["docker", "inspect", *ids]).stdout) if ids else []
                )
                running = {}
                for container in inspections:
                    labels = container["Config"]["Labels"]
                    service = labels.get("com.docker.compose.service")
                    if (
                        service in services
                        and container["State"]["Running"]
                        and container["State"].get("Health", {}).get("Status", "healthy")
                        == "healthy"
                    ):
                        if (
                            container["Image"] != self.expected_ids[service]
                            or labels.get("homeserver.release") != release
                        ):
                            raise ValueError("running image/label differs from manifest")
                        running[service] = True
                if set(running) == set(services):
                    url = _health_url(settings)
                    with urlopen(url, timeout=float(settings.http_timeout_seconds)) as response:
                        if response.status == 200:
                            return True
            except (URLError, OSError, RuntimeError):
                pass
            time.sleep(1)
        raise RuntimeError("release readiness/smoke checks failed")


def _set_current(root: Path, target: Path):
    temporary = root / ".current-next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, root / "current")


def _record(root: Path, release: str, phase: str, **extra):
    atomic_write(
        root / "shared/deploy-state.json",
        json.dumps({"release": release, "phase": phase, **extra}) + "\n",
    )


def _retain_releases(settings: Settings, previous: Path | None):
    """Delete only verified own release trees, always protecting current/previous."""
    root = Path(settings.install_root)
    releases = (root / "releases").resolve()
    protected = {(root / "current").resolve()}
    if previous is not None:
        protected.add(previous.resolve())
    candidates = []
    for path in releases.iterdir():
        if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"[0-9a-f]{40}", path.name):
            continue
        if path.resolve().parent != releases:
            continue
        try:
            manifest = json.loads((path / "release.json").read_text())
        except (ValueError, OSError):
            continue
        if manifest.get("schema_version") == 2 and manifest.get("git_commit") == path.name:
            candidates.append(path)
    candidates.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
    remaining = max(0, int(settings.release_keep_count) - len(protected))
    for path in candidates:
        if path.resolve() in protected:
            continue
        if remaining:
            remaining -= 1
            continue
        # Revalidate the final absolute target immediately before recursive removal.
        if not path.is_symlink() and path.resolve().parent == releases:
            shutil.rmtree(path)


def deploy_release(
    settings: Settings,
    env_file: Path,
    release: str,
    artifact: Path,
    manifest_path: Path,
    *,
    backend=None,
) -> dict:
    _validator().validate(manifest_path, artifact, release)
    manifest = json.loads(manifest_path.read_text())
    if manifest["schema_version"] != 2:
        raise ValueError("operational deploy requires a schema v2 release manifest")
    if manifest.get("config_version") != int(
        settings.as_environment()["HOMESERVER_CONFIG_VERSION"]
    ):
        raise ValueError("configuration version is incompatible with release")
    preflight(settings)
    references = manifest["image_references"]
    wanted = set(render_stack(settings)["services"])
    if not wanted <= set(references):
        raise ValueError("release does not pin every enabled service image")
    backend = backend or DockerBackend()
    env = settings.as_environment()
    root = Path(env["HOMESERVER_INSTALL_ROOT"])
    database = Path(env["HOMESERVER_APPDATA_ROOT"]) / "control/control.sqlite"
    _compatible(manifest, _schema(database))
    if manifest["requires_backup"] and env["HOMESERVER_BACKUP_ENABLED"] != "true":
        raise ValueError("release requires an enabled verified backup")
    with operation_lock(settings):
        backend.pull_and_verify(references, release)
        current = root / "current"
        target = root / "releases" / release
        if current.exists() and current.resolve() == target.resolve():
            backend.activate(root / "shared/compose.json")
            backend.ready(settings, release)
            return {"release": release, "state": "active", "changed": False}
        backup = (
            create_backup(settings, env_file, _already_locked=True)
            if manifest["requires_backup"]
            else None
        )
        root.mkdir(parents=True, exist_ok=True)
        root.joinpath("releases").mkdir(exist_ok=True)
        if target.exists():
            if (
                not (target / "release.json").is_file()
                or json.loads((target / "release.json").read_text()) != manifest
            ):
                raise ValueError("existing release directory differs from requested manifest")
        else:
            incoming = root / "releases" / (".incoming-" + release)
            if incoming.exists():
                raise ValueError("unfinished release extraction requires operator inspection")
            _validator().extract_artifact(artifact, incoming)
            atomic_write(incoming / "release.json", json.dumps(manifest, indent=2) + "\n")
            atomic_write(incoming / "COMMIT", release + "\n")
            atomic_write(incoming / "operator.env", serialize_env(settings.as_environment()))
            incoming.rename(target)
        prior = current.resolve() if current.exists() else None
        compose = root / "shared/compose.json"
        old_compose = compose.read_bytes() if compose.exists() else None
        _record(
            root, release, "prepared", backup_snapshot=backup["snapshot_id"] if backup else None
        )
        with maintenance(settings, "deploy", keep_on_error=True):
            try:
                _migrate(database, target, manifest["database_schema"])
                if env["HOMESERVER_ENVIRONMENT"] == "prod":
                    os.chown(database, int(settings.service_uid), int(settings.service_gid))
                    os.chown(database.parent, int(settings.service_uid), int(settings.service_gid))
                    run_checked(
                        ["uv", "sync", "--frozen", "--no-dev", "--project", str(target)],
                        timeout=600,
                    )
                _set_current(root, target)
                write_stack(settings, compose, images=references)
                stack = json.loads(compose.read_text())
                for service in stack["services"].values():
                    service["labels"] = {"homeserver.release": release}
                atomic_write(compose, json.dumps(stack, indent=2) + "\n")
                record_generated_file(settings, compose)
                write_operator_env(settings)
                atomic_write(target / "runtime-compose.json", compose.read_bytes())
                _record(root, release, "activating")
                if env["HOMESERVER_ENVIRONMENT"] == "prod":
                    run_checked(["systemctl", "start", "homeserver-metrics.service"])
                backend.activate(compose)
                if hasattr(backend, "configure"):
                    configured = backend.configure(settings, env_file)
                    if configured.as_environment() != settings.as_environment():
                        settings = configured
                        _write_runtime_stack(settings, compose, references, release)
                        atomic_write(
                            target / "operator.env", serialize_env(settings.as_environment())
                        )
                        atomic_write(target / "runtime-compose.json", compose.read_bytes())
                        backend.activate(compose)
                preflight(settings, host_tools=False)
                (Path(settings.run_root) / "maintenance").unlink(missing_ok=True)
                backend.ready(settings, release)
                _record(root, release, "active")
            except Exception:
                try:
                    backend.stop(compose)
                finally:
                    if prior is not None:
                        _set_current(root, prior)
                    else:
                        current.unlink(missing_ok=True)
                    if old_compose is not None:
                        atomic_write(compose, old_compose)
                    atomic_write(
                        database.parent / "RECOVERY_MODE",
                        "admission_enabled=false\nreason=release_activation_failed\n",
                    )
                    _record(root, release, "recovery_required")
                raise
        if settings.environment == "prod":
            preflight(settings, host_tools=False)
            run_checked(["systemctl", "start", "homeserver-stack.service"])
        _retain_releases(settings, prior)
    return {"release": release, "state": "active", "changed": True}


def rollback_release(settings: Settings, env_file: Path, release: str, *, backend=None) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise ValueError("release must be a lowercase SHA")
    root = Path(settings.as_environment()["HOMESERVER_INSTALL_ROOT"])
    target = root / "releases" / release
    manifest = json.loads((target / "release.json").read_text())
    if manifest.get("schema_version") != 2 or manifest.get("git_commit") != release:
        raise ValueError("rollback target is not a verified release")
    database = Path(settings.as_environment()["HOMESERVER_APPDATA_ROOT"]) / "control/control.sqlite"
    _compatible(manifest, _schema(database))
    restored, operator_env = _rollback_configuration(settings, target / "operator.env", env_file)
    if (
        restored.install_root != settings.install_root
        or restored.appdata_root != settings.appdata_root
    ):
        raise ValueError(
            "rollback configuration changes state roots; isolated recovery is required"
        )
    preflight(restored)
    backend = backend or DockerBackend()
    with operation_lock(restored):
        previous = (root / "current").resolve() if (root / "current").exists() else None
        marker = Path(restored.run_root) / "maintenance"
        if marker.is_file() and marker.read_text() in (
            "deploy_failed\n",
            "rollback_failed\n",
            "configure_failed\n",
        ):
            marker.unlink()
        backend.pull_and_verify(manifest["image_references"], release)
        with maintenance(restored, "rollback", keep_on_error=True):
            compose = root / "shared/compose.json"
            try:
                _set_current(root, target)
                atomic_write(env_file, operator_env)
                _write_runtime_stack(restored, compose, manifest["image_references"], release)
                backend.activate(compose)
                if hasattr(backend, "configure"):
                    configured = backend.configure(restored, env_file)
                    if configured.as_environment() != restored.as_environment():
                        restored = configured
                        _write_runtime_stack(
                            restored, compose, manifest["image_references"], release
                        )
                        backend.activate(compose)
                recovery = database.parent / "RECOVERY_MODE"
                own_reasons = (
                    "release_activation_failed",
                    "runtime_configuration_failed",
                    "rollback_failed",
                )
                if recovery.is_file() and any(
                    recovery.read_text() == f"admission_enabled=false\nreason={reason}\n"
                    for reason in own_reasons
                ):
                    recovery.unlink()
                preflight(restored, host_tools=False)
                (Path(restored.run_root) / "maintenance").unlink(missing_ok=True)
                backend.ready(restored, release)
                _save_runtime_configuration(target, restored, compose)
                _record(root, release, "active", rollback=True)
            except Exception:
                try:
                    backend.stop(compose)
                finally:
                    _recovery(database, "rollback_failed")
                    _record(root, release, "recovery_required", rollback=True)
                raise
        if restored.environment == "prod":
            preflight(restored, host_tools=False)
            run_checked(["systemctl", "start", "homeserver-stack.service"])
        _retain_releases(restored, previous)
    return {
        "release": release,
        "state": "active",
        "database_schema": _schema(database),
        "changed": True,
    }


def _write_runtime_stack(settings: Settings, path: Path, references: dict, release: str):
    write_stack(settings, path, images=references)
    stack = json.loads(path.read_text())
    for service in stack["services"].values():
        service["labels"] = {"homeserver.release": release}
    atomic_write(path, json.dumps(stack, indent=2) + "\n")
    record_generated_file(settings, path)
    write_operator_env(settings)


def refresh_runtime_configuration(
    settings: Settings,
    env_file: Path,
    *,
    backend=None,
    _already_locked=False,
    _already_maintenance=False,
) -> dict:
    """Apply runtime env changes while preserving the active manifest's image pins."""
    root = Path(settings.install_root)
    current_manifest = root / "current/release.json"
    if not current_manifest.is_file():
        write_operator_env(settings)
        return {"changed": False, "activation": "deploy required"}
    manifest = json.loads(current_manifest.read_text())
    if manifest.get("schema_version") != 2:
        raise ValueError("runtime refresh requires a verified schema v2 release")
    preflight(settings)
    compose = root / "shared/compose.json"
    desired = render_stack(settings, images=manifest["image_references"])
    from .render import literal_compose

    desired = literal_compose(desired)
    for service in desired["services"].values():
        service["labels"] = {"homeserver.release": manifest["git_commit"]}
    if compose.is_file() and json.loads(compose.read_text()) == desired:
        write_operator_env(settings)
        return {"changed": False, "release": manifest["git_commit"]}
    backend = backend or DockerBackend()
    with nullcontext() if _already_locked else operation_lock(settings):
        backend.pull_and_verify(manifest["image_references"], manifest["git_commit"])
        context = (
            nullcontext()
            if _already_maintenance
            else maintenance(settings, "configure", keep_on_error=True)
        )
        with context:
            try:
                backend.stop(compose)
                _write_runtime_stack(
                    settings, compose, manifest["image_references"], manifest["git_commit"]
                )
                backend.activate(compose)
                preflight(settings, host_tools=False)
                (Path(settings.run_root) / "maintenance").unlink(missing_ok=True)
                backend.ready(settings, manifest["git_commit"])
                _save_runtime_configuration(root / "current", settings, compose)
            except Exception:
                try:
                    backend.stop(compose)
                finally:
                    database = Path(settings.appdata_root) / "control/control.sqlite"
                    _recovery(database, "runtime_configuration_failed")
                raise
    return {"changed": True, "release": manifest["git_commit"]}


def build_release(output: Path, image_prefix: str, *, project_root: Path = PROJECT_ROOT) -> dict:
    """Publish built project images, pin upstream digests, archive the clean Git SHA."""
    if not image_prefix or any(char.isspace() for char in image_prefix):
        raise ValueError("a registry image prefix is required")
    if run_checked(["git", "status", "--porcelain"], cwd=project_root).stdout.strip():
        raise ValueError("release build requires a clean Git checkout")
    release = run_checked(["git", "rev-parse", "HEAD"], cwd=project_root).stdout.strip()
    references = {}
    for role in ("control", "telemetry"):
        tag = image_prefix.rstrip("/") + f"-{role}:{release}"
        run_checked(
            [
                "docker",
                "build",
                "--label",
                f"org.opencontainers.image.revision={release}",
                "-f",
                f"deploy/Dockerfile.{role}",
                "-t",
                tag,
                ".",
            ],
            cwd=project_root,
            timeout=1800,
        )
        run_checked(["docker", "push", tag], timeout=1800)
        digests = json.loads(run_checked(["docker", "image", "inspect", tag]).stdout)[0][
            "RepoDigests"
        ]
        reference = next(
            (ref for ref in digests if ref.startswith(image_prefix.rstrip("/") + f"-{role}@")), None
        )
        if not reference:
            raise ValueError("built image has no published registry digest")
        for name in (
            ("control-api", "control-worker", "download-gateway", "operator")
            if role == "control"
            else ("telemetry",)
        ):
            references[name] = reference
    for name, image in IMAGES.items():
        if name in references:
            continue
        run_checked(["docker", "pull", image], timeout=600)
        digests = json.loads(run_checked(["docker", "image", "inspect", image]).stdout)[0][
            "RepoDigests"
        ]
        if not digests:
            raise ValueError("upstream image has no immutable registry digest")
        repository = image.split("@", 1)[0].rsplit(":", 1)[0]
        reference = next((ref for ref in digests if ref.startswith(repository + "@")), None)
        if not reference:
            raise ValueError("upstream registry digest does not match its image repository")
        references[name] = reference
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f"homeserver-{release}.tar"
    run_checked(
        ["git", "archive", "--format=tar", "--output", str(artifact.resolve()), release],
        cwd=project_root,
    )
    with tarfile.open(artifact) as archive:
        checksums = {}
        for item in archive.getmembers():
            if item.isfile() and item.name.startswith(("deploy/", "config/")):
                checksums[item.name] = hashlib.sha256(archive.extractfile(item).read()).hexdigest()
    migrations = list(
        (project_root / "services/control/src/homeserver_control/persistence/migrations").glob(
            "*.sql"
        )
    )
    schema = max(int(path.stem.split("_", 1)[0]) for path in migrations)
    manifest = {
        "schema_version": 2,
        "git_commit": release,
        "database_schema": schema,
        "database_compatibility": {"minimum": 0, "maximum": schema},
        "config_version": 1,
        "requires_backup": True,
        "images": {key: ref.split("@", 1)[1] for key, ref in references.items()},
        "image_references": references,
        "config_checksums": checksums,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "tools": {"python": "3.12", "uv": "0.8.0"},
    }
    manifest_path = output / f"homeserver-{release}.json"
    atomic_write(manifest_path, json.dumps(manifest, indent=2) + "\n", mode=0o644)
    _validator().validate(manifest_path, artifact, release)
    return {"release": release, "artifact": str(artifact), "manifest": str(manifest_path)}
