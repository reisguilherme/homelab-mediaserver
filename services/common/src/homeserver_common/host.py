"""Prepare only the folders and absent native settings used by Docker Compose."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import stat
import subprocess
import xml.etree.ElementTree as xml
from pathlib import Path

from .env import load_settings, parse_env
from .render import PROJECT_ROOT, atomic_write
from .settings import Settings
from .storage import load_storage_registry
from .storage_startup import verify_storage_startup


class DependencyError(RuntimeError):
    """A required host tool is unavailable."""


def container_settings(settings: Settings, *, media_root="/data"):
    return Settings(
        dict(settings.values)
        | {
            "appdata_root": "/srv/appdata",
            "media_root": media_root,
            "transcode_root": "/srv/transcode",
            "run_root": "/run/homeserver",
        }
    )


def run_checked(command: list[str], *, timeout=120, cwd=None, env=None):
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env
        )
    except FileNotFoundError:
        raise DependencyError(f"required command is unavailable: {command[0]}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"operation timed out: {command[0]}") from None
    if result.returncode:
        raise RuntimeError(f"operation failed: {command[0]} (exit {result.returncode})")
    return result


def preflight(settings: Settings, *, host_tools=True, check_device=True):
    checks = []
    if settings.environment == "prod":
        if platform.system() != "Linux":
            raise ValueError("production runtime requires Linux")
        if load_storage_registry(Path(settings.run_root) / 'storage.json') is not None:
            checks.append({"name": "media_uuid", "status": "registered_pools"})
        elif settings.media_uuid:
            run_checked(
                [
                    "bash",
                    str(PROJECT_ROOT / "scripts/check-mount.sh"),
                    settings.media_root,
                    settings.media_uuid,
                ]
            )
            checks.append({"name": "media_uuid", "status": "verified"})
        else:
            checks.append({"name": "media_uuid", "status": "unknown"})
        if host_tools:
            if not shutil.which("docker"):
                raise DependencyError("required command is unavailable: docker")
            run_checked(["docker", "compose", "version"])
    else:
        checks.append({"name": "media_uuid", "status": "fixture"})
    if check_device and settings.transcode_mode == "intel":
        device = Path(settings.intel_render_device)
        if not device.exists() or not os.access(device, os.R_OK | os.W_OK):
            raise ValueError("Intel render device is missing or inaccessible")
        checks.append({"name": "intel_render_device", "status": "verified"})
    return checks


def _native_seeds(settings):
    appdata = Path(settings.appdata_root)
    seeds = {}
    for name, port in (("sonarr", 8989), ("radarr", 7878), ("prowlarr", 9696)):
        path = appdata / name / "config.xml"
        key = getattr(settings, name + "_api_key")
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
    if not bazarr.exists() and settings.bazarr_api_key:
        auth = {"apikey": settings.bazarr_api_key}
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
            f'WebUI\\Username="{username}"\nWebUI\\Password_PBKDF2="@ByteArray({encoded})"\nWebUI\\Port=8080\n'
        )
    return seeds


def prepare_controller_database(path: Path, *, uid: int, gid: int) -> None:
    """Prevent root worker's first-create race; never repair existing state silently."""
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError('controller database path must not contain a symlink')
    if os.geteuid() != 0 and (uid, gid) != (os.geteuid(), os.getegid()):
        raise ValueError('controller database preparation requires the service identity or root')
    parent = path.parent.stat()
    if (parent.st_uid != uid or parent.st_gid != gid
            or not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o700 != 0o700):
        raise ValueError('controller database directory owner/access mismatch')
    for candidate in (path, *(Path(str(path) + suffix)
                              for suffix in ('-wal', '-shm', '-journal'))):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('controller database/sidecar must not contain a symlink')
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_gid != gid
                or info.st_mode & 0o600 != 0o600):
            raise ValueError('controller database/sidecar owner/access mismatch')
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return  # Preserve existing database bytes, journal mode and permissions.
    try:
        if os.geteuid() == 0:
            os.fchown(descriptor, uid, gid)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(path)
    try:
        connection.execute('PRAGMA journal_mode = WAL')
    finally:
        connection.close()


def prepare_runtime(settings: Settings):
    """Never replace native configs, remove data, or manage the host's services."""
    registered_storage = verify_storage_startup(
        Path(settings.run_root) / 'storage.json', Path(settings.media_root))
    preflight(settings, host_tools=False, check_device=False)
    appdata, media = Path(settings.appdata_root), Path(settings.media_root)
    directories = [Path(settings.run_root), Path(settings.transcode_root)]
    directories += [
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
    ]
    directories += [media / name for name in ("torrents", "media", "media/movies", "media/tv")]
    for path in directories:
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("runtime directories must not contain a symlink")
        if not path.exists():
            path.mkdir(parents=True, mode=0o750)
            if os.geteuid() == 0:
                os.chown(path, int(settings.service_uid), int(settings.service_gid))
    if registered_storage:
        prepare_controller_database(appdata / 'control/control.sqlite',
                                    uid=int(settings.service_uid), gid=int(settings.service_gid))
    seeded = 0
    for path in (appdata / name / "config.xml" for name in ("sonarr", "radarr", "prowlarr")):
        if path.is_symlink():
            raise ValueError("native configuration must not contain a symlink")
    for path, content in _native_seeds(settings).items():
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("native configuration must not contain a symlink")
        if path.exists():
            continue
        atomic_write(path, content)
        if os.geteuid() == 0:
            os.chown(path, int(settings.service_uid), int(settings.service_gid))
            os.chown(path.parent, int(settings.service_uid), int(settings.service_gid))
        seeded += 1
    return {"prepared": True, "seeded": seeded}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--mode", choices=("dev", "prod"))
    args = parser.parse_args(argv)
    try:
        mode = args.mode or parse_env(args.env_file.read_text()).get(
            "HOMESERVER_ENVIRONMENT", "prod"
        )
        settings = load_settings(args.env_file, mode=mode)
        if os.environ.get("HOMESERVER_NATIVE_CONTEXT") == "container":
            settings = container_settings(settings, media_root="/srv/data")
        print(json.dumps(prepare_runtime(settings)))
        return 0
    except (OSError, ValueError, RuntimeError):
        print("runtime preparation failed; inspect configuration and folder access")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
