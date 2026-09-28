#!/usr/bin/env python3
"""Exercise seven real native services in a new, isolated CPU Docker fixture."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "services/common/src"))
from homeserver_common.cli import initialize_env  # noqa: E402
from homeserver_common.env import load_settings, parse_env, serialize_env  # noqa: E402
from homeserver_common.install import DependencyError, apply_install  # noqa: E402
from homeserver_common.render import atomic_write  # noqa: E402

SERVICES = {"qbittorrent", "radarr", "sonarr", "prowlarr", "bazarr", "jellyfin", "seerr"}


def require_docker():
    if not shutil.which("docker"):
        raise DependencyError("Docker unavailable")
    for args in (["docker", "compose", "version"], ["docker", "info"]):
        try:
            result = subprocess.run(args, capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            raise DependencyError("Docker unavailable") from None
        if result.returncode:
            raise DependencyError("Docker unavailable")


def free_ports(count):
    # Hold both protocols while selecting distinct ports, then release before Compose.
    sockets = []
    ports = []
    try:
        while len(ports) < count:
            tcp = socket.socket()
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            udp = socket.socket(type=socket.SOCK_DGRAM)
            try:
                udp.bind(("127.0.0.1", port))
            except OSError:
                tcp.close()
                udp.close()
                continue
            sockets.extend([tcp, udp])
            ports.append(port)
        return ports
    finally:
        for sock in sockets:
            sock.close()


def prepare_fixture(root):
    root = root.absolute()
    if root.exists() or any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("fixture root must be a new directory without symlinks")
    root.mkdir(parents=True, mode=0o700)
    env_file = root / ".env"
    initialize_env(env_file, "dev")
    values = parse_env(env_file.read_text())
    for key in ("INSTALL", "APPDATA", "MEDIA", "TRANSCODE", "BACKUP_STAGING", "RUN"):
        values[f"HOMESERVER_{key}_ROOT"] = str(root / key.lower())
    names = (
        "JELLYFIN",
        "SEERR",
        "SONARR",
        "RADARR",
        "PROWLARR",
        "BAZARR",
        "CONTROL",
        "STATUS",
        "QBIT_MONITOR",
        "QBIT_PEER",
    )
    for name, port in zip(names, free_ports(len(names)), strict=True):
        values[f"HOMESERVER_{name}_PORT"] = str(port)
        if name != "QBIT_PEER":
            values[f"HOMESERVER_{name}_PUBLIC_URL"] = f"http://127.0.0.1:{port}"
    values.update(
        HOMESERVER_INSTANCE_NAME="homeserver-fresh-" + uuid.uuid4().hex[:12],
        HOMESERVER_SERVICE_UID=str(os.getuid()),
        HOMESERVER_SERVICE_GID=str(os.getgid()),
        HOMESERVER_ADMIN_USERNAME="fixture-admin",
        HOMESERVER_TRANSCODE_MODE="cpu",
        HOMESERVER_MEDIA_UUID="",
        HOMESERVER_LAN_BIND_IP="127.0.0.1",
        HOMESERVER_TAILSCALE_BIND_IP="127.0.0.1",
        HOMESERVER_QBIT_PEER_BIND_IP="127.0.0.1",
        HOMESERVER_BYPARR_ENABLED="false",
        HOMESERVER_PROWLARR_INDEXERS="[]",
        HOMESERVER_BAZARR_PROVIDERS="",
        HOMESERVER_SOURCE_PROBE_ENABLED="false",
        HOMESERVER_BACKUP_ENABLED="false",
    )
    atomic_write(env_file, serialize_env(values))
    return env_file, load_settings(env_file, mode="dev")


def check_outcomes(rows, *, unchanged=False):
    if (
        not isinstance(rows, list)
        or len(rows) != len(SERVICES)
        or {row.get("service") for row in rows if isinstance(row, dict)} != SERVICES
        or any(row.get("status") != "verified" for row in rows)
        or any(not isinstance(row.get("changes"), list) for row in rows)
        or (unchanged and any(row["changes"] for row in rows))
    ):
        raise RuntimeError("native configuration did not verify all seven services")


class Runner:
    def __init__(self, settings, env_file, timeout):
        self.settings, self.env_file = settings, env_file
        self.timeout = timeout
        self.compose_file = Path(settings.install_root) / "shared/compose.json"
        self.project = settings.instance_name
        self.started = False
        self.metrics = None

    def compose(self, *args, timeout=120):
        command = ["docker", "compose", "-p", self.project, "-f", str(self.compose_file), *args]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError("isolated Compose command unavailable or timed out") from None
        return result

    def checked_compose(self, *args):
        if self.compose(*args).returncode:
            raise RuntimeError("isolated Compose operation failed")

    def operator(self, action):
        result = self.compose(
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "operator",
            "config",
            action,
            "--env-file",
            "/run/homeserver/operator.env",
            "--mode",
            "dev",
            "--in-container",
        )
        try:
            rows = json.loads(result.stdout)
        except ValueError:
            raise RuntimeError("operator did not return structured outcomes") from None
        if result.returncode not in (0, 4):
            raise RuntimeError("operator failed")
        atomic_write(
            Path(self.settings.run_root) / f"operator-{action}-outcomes.json",
            json.dumps(rows, indent=2),
        )
        return rows

    def snapshots(self):
        if self.metrics is None:
            spec = importlib.util.spec_from_file_location(
                "fresh_host_metrics", PROJECT / "scripts/host-metrics.py"
            )
            self.metrics = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.metrics)
        media = Path(self.settings.media_root)
        stat = os.statvfs(media)
        # This is an honestly measured dev filesystem identity, never a claimed UUID.
        capacity = {
            "filesystem_id": f"dev-device:{media.stat().st_dev}",
            "measured_at": time.time(),
            "free_bytes": stat.f_bavail * stat.f_frsize,
            "total_bytes": stat.f_blocks * stat.f_frsize,
        }
        atomic_write(Path(self.settings.run_root) / "capacity.json", json.dumps(capacity))
        atomic_write(
            Path(self.settings.run_root) / "host.json",
            json.dumps(self.metrics.collect(media_path=media)),
        )

    def wait_native(self):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.snapshots()
            rows = self.operator("plan")
            if isinstance(rows, list) and {row.get("service") for row in rows} == SERVICES:
                if any(row.get("status") == "unsupported" for row in rows):
                    raise RuntimeError("native capability unsupported")
                if all(row.get("status") in ("planned", "drift", "verified") for row in rows):
                    return
            time.sleep(3)
        raise RuntimeError("native API bootstrap exceeded bounded wait")

    def adopt_credentials(self):
        path = Path(self.settings.run_root) / "native-credentials.json"
        if not path.is_file() or path.stat().st_mode & 0o077:
            raise RuntimeError("private credential adoption record unavailable")
        updates = json.loads(path.read_text())
        allowed = {"HOMESERVER_JELLYFIN_API_KEY", "HOMESERVER_SEERR_API_KEY"}
        if not isinstance(updates, dict) or set(updates) - allowed or not updates:
            raise RuntimeError("invalid private credential adoption record")
        if any(not isinstance(value, str) or not value for value in updates.values()):
            raise RuntimeError("invalid native credential")
        values = parse_env(self.env_file.read_text()) | updates
        atomic_write(self.env_file, serialize_env(values))
        path.unlink()
        self.settings = load_settings(self.env_file, mode="dev")

    def wait_health(self):
        deadline = time.monotonic() + self.timeout
        ports = [self.settings.control_port, self.settings.status_port]
        with httpx.Client(trust_env=False, timeout=5) as client:
            while time.monotonic() < deadline:
                self.snapshots()
                try:
                    if all(
                        client.get(f"http://127.0.0.1:{port}/health/ready").status_code == 200
                        for port in ports
                    ):
                        ids = self.compose("ps", "--all", "--quiet").stdout.split()
                        inspection = subprocess.run(
                            ["docker", "inspect", *ids], capture_output=True, text=True, timeout=15
                        )
                        containers = json.loads(inspection.stdout)
                        if (
                            not ids
                            or inspection.returncode
                            or any(
                                item["State"]["Status"] != "running"
                                or item["State"].get("Health", {}).get("Status", "healthy")
                                != "healthy"
                                for item in containers
                            )
                        ):
                            time.sleep(3)
                            continue
                        return
                except (httpx.HTTPError, ValueError):
                    pass
                time.sleep(3)
        raise RuntimeError("runtime readiness exceeded bounded wait")

    def execute(self):
        apply_install(self.settings, self.env_file)
        self.checked_compose("config", "--quiet")
        self.snapshots()
        self.started = True  # A failed up can still have created owned containers.
        self.checked_compose("up", "-d")
        self.wait_native()
        check_outcomes(self.operator("apply"))
        self.adopt_credentials()
        apply_install(self.settings, self.env_file)
        self.checked_compose("up", "-d")
        check_outcomes(self.operator("apply"), unchanged=True)
        check_outcomes(self.operator("verify"), unchanged=True)
        check_outcomes(self.operator("plan"), unchanged=True)
        values = parse_env(self.env_file.read_text())
        values.update(
            HOMESERVER_DOWNLOAD_MAX_ACTIVE="3",
            HOMESERVER_SEED_MAX_ACTIVE="7",
            HOMESERVER_TORRENT_MAX_ACTIVE="10",
        )
        atomic_write(self.env_file, serialize_env(values))
        self.settings = load_settings(self.env_file, mode="dev")
        apply_install(self.settings, self.env_file)
        self.checked_compose("up", "-d")
        changed = self.operator("apply")
        check_outcomes(changed)
        qbit = next(row for row in changed if row["service"] == "qbittorrent")
        if {item["key"] for item in qbit["changes"]} != {
            "max_active_downloads",
            "max_active_uploads",
            "max_active_torrents",
        }:
            raise RuntimeError("qBittorrent configuration change was not observed")
        check_outcomes(self.operator("verify"), unchanged=True)
        check_outcomes(self.operator("apply"), unchanged=True)
        self.wait_health()

    def cleanup(self):
        if self.started:
            self.checked_compose("down", "--volumes", "--remove-orphans", "--timeout", "15")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, help="new isolated fixture directory; retained privately"
    )
    parser.add_argument(
        "--timeout", type=int, default=300, help="native/runtime wait bound in seconds"
    )
    args = parser.parse_args(argv)
    if not 30 <= args.timeout <= 600:
        parser.error("timeout must be between 30 and 600 seconds")
    runner = None
    code = 0
    try:
        require_docker()
        root = args.root or PROJECT / ".runtime" / ("fresh-" + uuid.uuid4().hex[:12])
        env_file, settings = prepare_fixture(root)
        runner = Runner(settings, env_file, args.timeout)
        runner.execute()
    except DependencyError:
        print("fresh-stack: Docker/Compose unavailable; real acceptance not run", file=sys.stderr)
        code = 3
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyError, TypeError):
        print(
            "fresh-stack: isolated real-service validation failed; details retained privately",
            file=sys.stderr,
        )
        code = 4
    finally:
        if runner:
            try:
                runner.cleanup()
            except (OSError, RuntimeError):
                print("fresh-stack: owned project cleanup failed", file=sys.stderr)
                code = 4
    if code == 0:
        print(
            "fresh-stack: seven real services verified; repeated apply stable; "
            "qBit change/readback and readiness verified (dev CPU fixture)"
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
