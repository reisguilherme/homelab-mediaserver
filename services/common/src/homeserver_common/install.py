"""Idempotent preparation of project-owned files on an already prepared host."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from .env import serialize_env
from .render import PROJECT_ROOT, atomic_write, literal_compose, render_stack, render_units
from .settings import Settings


class DependencyError(RuntimeError):
    """A required host tool is unavailable."""


def run_checked(
    command: list[str], *, timeout=120, cwd=None, env=None
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env
        )
    except FileNotFoundError as error:
        raise DependencyError(f"required command is unavailable: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"operation timed out: {command[0]}") from error
    if result.returncode:
        # Native output may contain credentials or private media. Keep it off public output.
        raise RuntimeError(f"operation failed: {command[0]} (exit {result.returncode})")
    return result


def preflight(settings: Settings, *, host_tools: bool = True) -> list[dict]:
    env = settings.as_environment()
    media = Path(env["HOMESERVER_MEDIA_ROOT"])
    checks = []
    if env["HOMESERVER_ENVIRONMENT"] == "prod":
        if platform.system() != "Linux":
            raise ValueError("production installation requires Linux")
        guard = PROJECT_ROOT / "scripts/check-mount.sh"
        run_checked(["bash", str(guard), str(media), env["HOMESERVER_MEDIA_UUID"]])
        checks.append({"name": "media_uuid", "status": "verified"})
        if host_tools:
            for command in ("docker", "uv", "systemctl", "findmnt"):
                if not shutil.which(command):
                    raise DependencyError(f"required command is unavailable: {command}")
            run_checked(["docker", "compose", "version"])
            if settings.backup_enabled and not shutil.which("restic"):
                raise DependencyError("restic is required when backups are enabled")
    else:
        checks.append({"name": "media_uuid", "status": "fixture"})
    if env["HOMESERVER_TRANSCODE_MODE"] == "intel":
        device = Path(env["HOMESERVER_INTEL_RENDER_DEVICE"])
        if not device.exists() or not os.access(device, os.R_OK | os.W_OK):
            raise ValueError("Intel render device is missing or inaccessible")
        checks.append({"name": "intel_render_device", "status": "verified"})
    return checks


def _desired(settings: Settings, env_file: Path, unit_root: Path | None):
    env = settings.as_environment()
    install = Path(env["HOMESERVER_INSTALL_ROOT"])
    shared = install / "shared"
    if unit_root is None:
        unit_root = (
            Path("/etc/systemd/system")
            if env["HOMESERVER_ENVIRONMENT"] == "prod"
            else shared / "systemd"
        )
    stack = render_stack(settings)
    current_manifest = install / "current/release.json"
    if current_manifest.is_file():
        manifest = json.loads(current_manifest.read_text())
        if manifest.get("schema_version") == 2:
            stack = render_stack(settings, images=manifest["image_references"])
            for service in stack["services"].values():
                service["labels"] = {"homeserver.release": manifest["git_commit"]}
    normalized = stack["services"]["control-api"]["environment"]
    files = {
        shared / "compose.json": json.dumps(literal_compose(stack), indent=2) + "\n",
        shared / "operator.env": serialize_env(normalized),
    }
    files.update(
        {unit_root / name: content for name, content in render_units(settings, env_file).items()}
    )
    dirs = {
        install,
        shared,
        Path(env["HOMESERVER_RUN_ROOT"]),
        Path(env["HOMESERVER_TRANSCODE_ROOT"]),
        Path(env["HOMESERVER_BACKUP_STAGING_ROOT"]),
        unit_root,
    }
    appdata, media = Path(env["HOMESERVER_APPDATA_ROOT"]), Path(env["HOMESERVER_MEDIA_ROOT"])
    dirs.update(
        appdata / name
        for name in (
            "jellyfin",
            "seerr",
            "sonarr",
            "radarr",
            "prowlarr",
            "qbittorrent",
            "bazarr",
            "control",
        )
    )
    dirs.update(media / name for name in ("torrents", "media", "media/movies", "media/tv"))
    return files, dirs, shared / "install.json"


def write_operator_env(settings: Settings) -> None:
    normalized = render_stack(settings)["services"]["control-api"]["environment"]
    path = Path(settings.install_root) / "shared/operator.env"
    atomic_write(path, serialize_env(normalized))
    record_generated_file(settings, path)


def record_generated_file(settings: Settings, path: Path) -> None:
    journal_path = Path(settings.install_root) / "shared/install.json"
    journal = (
        json.loads(journal_path.read_text())
        if journal_path.exists()
        else {"schema_version": 1, "files": {}}
    )
    journal["files"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_write(journal_path, json.dumps(journal, indent=2) + "\n")


def _native_seeds(settings: Settings) -> dict[Path, str]:
    """Only absent native files are seeded; upstream applications own later rewrites.

    Sources: upstream Arr ConfigFileProvider.cs, Bazarr app/config.py, and
    qBittorrent release-5.1.2 password.cpp/preferences.cpp (see infra report).
    """
    import base64
    import xml.etree.ElementTree as xml

    env = settings.as_environment()
    appdata = Path(settings.appdata_root)
    seeds = {}
    for name, port in (("sonarr", 8989), ("radarr", 7878), ("prowlarr", 9696)):
        path = appdata / name / "config.xml"
        key = env["HOMESERVER_" + name.upper() + "_API_KEY"]
        if not path.exists() and key:
            config = xml.Element("Config")
            for field, value in {
                "ApiKey": key,
                "BindAddress": "*",
                "Port": str(port),
                "EnableSsl": "False",
                "LaunchBrowser": "False",
                "AuthenticationMethod": "Forms",
                "AuthenticationRequired": "Enabled",
                "UpdateMechanism": "Docker",
                "LogLevel": "Info",
            }.items():
                xml.SubElement(config, field).text = value
            seeds[path] = xml.tostring(config, encoding="unicode") + "\n"
    bazarr = appdata / "bazarr/config/config.yaml"
    if not bazarr.exists() and env["HOMESERVER_BAZARR_API_KEY"]:
        # JSON is a valid YAML mapping. Bazarr uses this native MD5 representation.
        auth = {"apikey": env["HOMESERVER_BAZARR_API_KEY"]}
        if settings.admin_password:
            auth.update(
                type="form",
                username=settings.admin_username,
                password=hashlib.md5(
                    settings.admin_password.encode(), usedforsecurity=False
                ).hexdigest(),
            )
        seeds[bazarr] = json.dumps({"auth": auth}, indent=2) + "\n"
    qbit = appdata / "qbittorrent/qBittorrent/qBittorrent.conf"
    if not qbit.exists() and settings.qbit_password:
        salt = os.urandom(16)
        digest = hashlib.pbkdf2_hmac(
            "sha512", settings.qbit_password.encode(), salt, 100000, dklen=64
        )
        encoded = base64.b64encode(salt).decode() + ":" + base64.b64encode(digest).decode()
        username = (
            settings.qbit_username.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        seeds[qbit] = (
            "[LegalNotice]\nAccepted=true\n\n[Network]\nPortForwardingEnabled=false\n"
            "\n[Preferences]\nConnection\\UPnP=false\nWebUI\\UseUPnP=false\n"
            + f'WebUI\\Username="{username}"\n'
            + f'WebUI\\Password_PBKDF2="@ByteArray({encoded})"\nWebUI\\Port=8080\n'
        )
    return seeds


def plan_install(
    settings: Settings, env_file: Path, *, mode: str = "fresh", unit_root: Path | None = None
) -> dict:
    if mode not in ("fresh", "adopt"):
        raise ValueError("installation mode must be fresh or adopt")
    if not env_file.resolve().is_file():
        raise ValueError("operator env file is unavailable")
    checks = preflight(settings)
    files, dirs, journal_path = _desired(settings, env_file, unit_root)
    journal = json.loads(journal_path.read_text()) if journal_path.exists() else {"files": {}}
    changes = []
    for directory in sorted(dirs, key=str):
        if any(path.is_symlink() for path in (directory, *directory.parents)):
            raise ValueError("installation directory must not be a symlink")
        if directory.exists() and not directory.is_dir():
            raise ValueError("installation directory is occupied by a file")
        if not directory.exists():
            changes.append({"kind": "directory", "path": str(directory)})
    for path, contents in files.items():
        if path.is_symlink():
            raise ValueError("generated file must not be a symlink")
        if path.exists():
            prior = path.read_text()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if str(path) not in journal["files"] or journal["files"][str(path)] != digest:
                raise ValueError(
                    f"refusing unmanaged or operator-modified generated file: {path.name}"
                )
            if prior == contents:
                continue
        changes.append({"kind": "file", "path": str(path)})
    for path in _native_seeds(settings):
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("native configuration must not contain a symlink")
        changes.append({"kind": "native_seed", "path": str(path)})
    current = Path(settings.as_environment()["HOMESERVER_INSTALL_ROOT"]) / "current"
    if not current.exists():
        changes.append({"kind": "bootstrap_runtime", "path": str(current)})
    return {"mode": mode, "checks": checks, "changes": changes, "activation": "deploy required"}


def apply_install(
    settings: Settings, env_file: Path, *, mode: str = "fresh", unit_root: Path | None = None
) -> dict:
    # Preflight remains read-only; re-plan under the same lock as deploy/config/backup.
    plan_install(settings, env_file, mode=mode, unit_root=unit_root)
    from .backup import operation_lock

    with operation_lock(settings):
        return _apply_install_locked(settings, env_file, mode=mode, unit_root=unit_root)


def _apply_install_locked(
    settings: Settings, env_file: Path, *, mode: str, unit_root: Path | None
) -> dict:
    plan = plan_install(settings, env_file, mode=mode, unit_root=unit_root)
    if not plan["changes"]:
        return plan
    files, _, journal_path = _desired(settings, env_file, unit_root)
    seeds = _native_seeds(settings)
    env = settings.as_environment()
    for change in plan["changes"]:
        path = Path(change["path"])
        if change["kind"] == "directory":
            path.mkdir(parents=True, exist_ok=True, mode=0o750)
            if env["HOMESERVER_ENVIRONMENT"] == "prod":
                os.chown(
                    path, int(env["HOMESERVER_SERVICE_UID"]), int(env["HOMESERVER_SERVICE_GID"])
                )
        elif change["kind"] == "file":
            atomic_write(
                path, files[path], mode=0o644 if path.suffix in (".service", ".timer") else 0o600
            )
            # Journal each completed own write so an interrupted apply can resume.
            previous = (
                json.loads(journal_path.read_text())
                if journal_path.exists()
                else {"schema_version": 1, "files": {}}
            )
            previous["files"][str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            atomic_write(journal_path, json.dumps(previous, indent=2) + "\n")
        elif change["kind"] == "native_seed":
            if path.exists() or path.is_symlink():
                raise ValueError(
                    "native configuration appeared during installation; retry planning"
                )
            atomic_write(path, seeds[path])
            if env["HOMESERVER_ENVIRONMENT"] == "prod":
                os.chown(
                    path, int(env["HOMESERVER_SERVICE_UID"]), int(env["HOMESERVER_SERVICE_GID"])
                )
                os.chown(
                    path.parent,
                    int(env["HOMESERVER_SERVICE_UID"]),
                    int(env["HOMESERVER_SERVICE_GID"]),
                )
            previous = (
                json.loads(journal_path.read_text())
                if journal_path.exists()
                else {"schema_version": 1, "files": {}}
            )
            previous.setdefault("native_bootstrap", {})[str(path)] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "ownership": "native_after_first_start",
            }
            atomic_write(journal_path, json.dumps(previous, indent=2) + "\n")
        elif change["kind"] == "bootstrap_runtime":
            bootstrap = Path(env["HOMESERVER_INSTALL_ROOT"]) / "releases/bootstrap"
            bootstrap.mkdir(parents=True, exist_ok=True)
            for item in ("scripts", "services", "deploy", "config", "pyproject.toml", "uv.lock"):
                source, target = PROJECT_ROOT / item, bootstrap / item
                if source.is_dir():
                    shutil.copytree(
                        source,
                        target,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
                elif source.is_file():
                    shutil.copyfile(source, target)
            if env["HOMESERVER_ENVIRONMENT"] == "prod":
                run_checked(
                    ["uv", "sync", "--frozen", "--no-dev", "--project", str(bootstrap)], timeout=600
                )
            path.symlink_to(bootstrap, target_is_directory=True)
    if env["HOMESERVER_ENVIRONMENT"] == "prod":
        run_checked(["systemctl", "daemon-reload"])
        timers = [name for name in render_units(settings, env_file) if name.endswith(".timer")]
        run_checked(
            [
                "systemctl",
                "enable",
                "homeserver-stack.service",
                "homeserver-metrics.service",
                *timers,
            ]
        )
    return plan
