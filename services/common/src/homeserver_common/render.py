"""Render a complete stack without Compose's implicit environment precedence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[4]
IMAGES = {
    "jellyfin": "jellyfin/jellyfin:10.10.7",
    "jellyfin-proxy": "nginx:1.30.5-alpine",
    "seerr": "fallenbagel/jellyseerr:2.7.3",
    "sonarr": "linuxserver/sonarr:4.0.15",
    "radarr": (
        "lscr.io/linuxserver/radarr@sha256:"
        "adb6c09d6b729ea5e642c99cea35af72702ef476bf4763f153299ac5db9f0b4f"
    ),
    "prowlarr": "linuxserver/prowlarr:1.35.1",
    "qbittorrent": "linuxserver/qbittorrent:5.1.2",
    "bazarr": "linuxserver/bazarr:1.5.1",
    "qbit-monitor": "caddy:2.10.2-alpine",
    "control-api": "homeserver-control:dev",
    "control-worker": "homeserver-control:dev",
    "download-gateway": "homeserver-control:dev",
    "operator": "homeserver-control:dev",
    "telemetry": "homeserver-telemetry:dev",
    "byparr": (
        "ghcr.io/thephaseless/byparr@sha256:"
        "874f719518f617d03a60e03411fc5d090647e1a877041e81f8dc965927c7deb6"
    ),
}


def bind(source: Path | str, target: str, *, read_only: bool = False) -> dict:
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": read_only,
        "bind": {"create_host_path": False},
    }


def render_storage_override() -> dict:
    """Opt-in, fixed technical paths; never derive disk identities from .env."""
    services = {}
    for name in ('sonarr', 'radarr', 'qbittorrent', 'control-worker', 'control-api',
                 'download-gateway', 'telemetry', 'host-metrics', 'operator'):
        readonly = name in ('control-api', 'download-gateway', 'telemetry', 'host-metrics')
        services[name] = {'volumes': [bind('/srv/media-view', '/data', read_only=readonly)]}
    for name, readonly in (('jellyfin', True), ('bazarr', False)):
        services[name] = {'volumes': [
            bind('/srv/media-view/media', '/data/media', read_only=readonly)]}
    services['init'] = {'volumes': [bind('/srv/media-view', '/srv/data', read_only=True)]}
    for name in ('control-worker', 'control-api', 'download-gateway', 'telemetry',
                 'host-metrics', 'init', 'operator'):
        readonly = name != 'control-worker'
        services[name]['volumes'] += [
            bind('/srv/data', '/storage/ssd', read_only=readonly),
            bind('/srv/external', '/storage/hdd', read_only=readonly),
            bind('/proc/1/mountinfo', '/run/host-mountinfo', read_only=True),
            bind('/dev/disk/by-uuid', '/run/host-uuids', read_only=True),
            bind('/srv/appdata/control/storage.json', '/run/homeserver/storage.json',
                 read_only=True),
        ]
    services['control-worker'].update(
        user='0:0', cap_drop=['ALL'], cap_add=['CHOWN', 'DAC_OVERRIDE', 'SETUID', 'SETGID'],
        entrypoint=['python', '-m', 'homeserver_common.storage_startup',
                    'python', '-m', 'homeserver_common.runtime'])
    for name in ('sonarr', 'radarr', 'qbittorrent', 'jellyfin', 'bazarr'):
        target = '/data/media' if name in ('jellyfin', 'bazarr') else '/data'
        original = '/jellyfin/jellyfin' if name == 'jellyfin' else '/init'
        services[name]['volumes'].append(
            bind('./scripts/storage-entrypoint.sh', '/run/storage-entrypoint.sh', read_only=True))
        services[name]['entrypoint'] = ['/bin/sh', '/run/storage-entrypoint.sh', target, original]
    return {'services': services}


def render_stack(
    settings: Settings, *, images: dict[str, str] | None = None, env_file: Path | None = None
) -> dict[str, object]:
    env = settings.as_environment()

    def value(key):
        return env["HOMESERVER_" + key]

    media, appdata = (Path(value(key)) for key in ("MEDIA_ROOT", "APPDATA_ROOT"))
    run = appdata / "control"
    code = PROJECT_ROOT
    env_file = Path(env_file) if env_file is not None else code / ".env"
    image_map = IMAGES | (images or {})
    addresses = (
        ["127.0.0.1"]
        if value("ENVIRONMENT") == "dev"
        else ["0.0.0.0"]
    )
    addresses = list(dict.fromkeys(addresses))
    logging = {
        "driver": "json-file",
        "options": {"max-size": value("LOG_MAX_SIZE_MB") + "m", "max-file": value("LOG_MAX_FILES")},
    }

    def ports(key: str, target: int, *, binds=None, protocols=("tcp",)) -> list:
        return [
            {"host_ip": address, "published": value(key), "target": target, "protocol": protocol}
            for address in (binds if binds is not None else addresses)
            for protocol in protocols
        ]

    def service(name: str, networks: list, volumes: list, **options):
        return {
            "image": image_map[name],
            "restart": "unless-stopped",
            "networks": networks,
            "volumes": volumes,
            "logging": logging,
            **options,
        }

    def healthcheck(code: str) -> dict:
        return {
            "test": ["CMD", "python", "-c", code],
            "interval": "15s",
            "timeout": "10s",
            "retries": 3,
            "start_period": "60s",
        }

    ready_http = "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=5).close()"

    identity = {"PUID": value("SERVICE_UID"), "PGID": value("SERVICE_GID"), "TZ": value("TIMEZONE")}
    control_env = dict(env)
    control_env.update(
        {
            "HOMESERVER_MEDIA_ROOT": "/data",
            "HOMESERVER_MEDIA_ROOTS": "/data/media:/data/torrents",
            "HOMESERVER_APPDATA_ROOT": "/var/lib/homeserver",
            "HOMESERVER_RUN_ROOT": "/run/homeserver",
            "HOMESERVER_DB_PATH": "/var/lib/homeserver/control.sqlite",
            "HOMESERVER_CAPACITY_SNAPSHOT": "/run/homeserver/capacity.json",
            "HOMESERVER_HOST_SNAPSHOT": "/run/homeserver/host.json",
            "HOMESERVER_RECOVERY_MODE": "/var/lib/homeserver/RECOVERY_MODE",
            "HOMESERVER_ARR_UID": value("SERVICE_UID"),
            "HOMESERVER_ARR_GID": value("SERVICE_GID"),
        }
    )
    state = bind(appdata / "control", "/var/lib/homeserver")
    live = bind(run, "/run/homeserver", read_only=True)
    services = {
        "jellyfin": service(
            "jellyfin",
            ["apps", "telemetry", "egress"],
            [
                bind(appdata / "jellyfin", "/config"),
                bind(media / "media", "/data/media", read_only=True),
                bind(value("TRANSCODE_ROOT"), "/transcode"),
            ],
            user=f"{value('SERVICE_UID')}:{value('SERVICE_GID')}",
            environment={"TZ": value("TIMEZONE")},
        ),
        "jellyfin-proxy": service(
            "jellyfin-proxy",
            ["apps", "edge_control"],
            [
                bind(
                    code / "deploy/jellyfin-proxy/nginx.conf",
                    "/etc/nginx/nginx.conf",
                    read_only=True,
                )
            ],
            ports=ports("JELLYFIN_PORT", 8096),
            depends_on=["jellyfin", "control-api"],
        ),
        "seerr": service(
            "seerr",
            ["apps", "edge_control", "egress"],
            [bind(appdata / "seerr", "/app/config")],
            ports=ports("SEERR_PORT", 5055),
            user=f"{value('SERVICE_UID')}:{value('SERVICE_GID')}",
            environment={
                "TZ": value("TIMEZONE"),
                "NODE_OPTIONS": "--no-network-family-autoselection --dns-result-order=ipv4first",
            },
        ),
        "sonarr": service(
            "sonarr",
            ["apps", "edge_control", "egress"],
            [bind(appdata / "sonarr", "/config"), bind(media, "/data")],
            ports=ports("SONARR_PORT", 8989),
            environment=identity,
        ),
        "radarr": service(
            "radarr",
            ["apps", "edge_control", "egress"],
            [bind(appdata / "radarr", "/config"), bind(media, "/data")],
            ports=ports("RADARR_PORT", 7878),
            environment=identity,
        ),
        "prowlarr": service(
            "prowlarr",
            ["apps", "edge_control", "egress"],
            [bind(appdata / "prowlarr", "/config")],
            ports=ports("PROWLARR_PORT", 9696),
            environment=identity,
        ),
        "bazarr": service(
            "bazarr",
            ["apps", "edge_control", "egress"],
            [bind(appdata / "bazarr", "/config"), bind(media / "media", "/data/media")],
            ports=ports("BAZARR_PORT", 6767),
            environment=identity,
        ),
        "qbittorrent": service(
            "qbittorrent",
            ["transfer", "egress_transfer"],
            [bind(appdata / "qbittorrent", "/config"), bind(media, "/data")],
            environment=identity
            | {"WEBUI_PORT": "8080", "TORRENTING_PORT": value("QBIT_PEER_PORT")},
            ports=ports(
                "QBIT_PEER_PORT",
                int(value("QBIT_PEER_PORT")),
                binds=[value("QBIT_PEER_BIND_IP")],
                protocols=("tcp", "udp"),
            ),
        ),
        "qbit-monitor": service(
            "qbit-monitor",
            ["transfer", "edge_control"],
            [bind(code / "deploy/qbit-monitor/Caddyfile", "/etc/caddy/Caddyfile", read_only=True)],
            ports=ports(
                "QBIT_MONITOR_PORT",
                8080,
                binds=["127.0.0.1"],
            ),
        ),
        "control-api": service(
            "control-api",
            ["edge_control", "apps", "telemetry"],
            [state, live, bind(media, "/data", read_only=True)],
            environment=control_env,
            ports=ports("CONTROL_PORT", 8080),
            healthcheck=healthcheck(ready_http),
            command=[
                "uvicorn",
                "homeserver_control.api.app:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8080",
                "--log-level",
                value("LOG_LEVEL").lower(),
            ],
        ),
        "control-worker": service(
            "control-worker",
            ["apps", "egress"],
            [state, live, bind(media, "/data")],
            environment=control_env,
            command=["python", "-m", "homeserver_control.worker"],
        healthcheck=healthcheck(
                "import os,time; "
                "from homeserver_control.persistence.heartbeat import WorkerHeartbeatStore; "
                "assert WorkerHeartbeatStore(os.environ['HOMESERVER_DB_PATH']).ready("
                "now=time.time(),"
                "max_age=float(os.environ.get('HOMESERVER_WORKER_HEARTBEAT_MAX_AGE_SECONDS','90')))"
            ),
        ),
        "download-gateway": service(
            "download-gateway",
            ["apps", "transfer"],
            [state, live, bind(media, "/data", read_only=True)],
            environment=control_env,
            healthcheck=healthcheck(
                "import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:8081/health/live',timeout=5).close()"
            ),
            command=[
                "uvicorn",
                "homeserver_control.gateway.app:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8081",
                "--log-level",
                value("LOG_LEVEL").lower(),
            ],
        ),
        "telemetry": service(
            "telemetry",
            ["telemetry", "edge_control", "egress"],
            [live, bind(media, "/data", read_only=True)],
            environment={
                key: val
                for key, val in env.items()
                if key.endswith(("_PUBLIC_URL", "_PORT"))
                or key in ("HOMESERVER_TAILSCALE_HOSTNAME", "HOMESERVER_METRICS_MAX_AGE_SECONDS")
            }
            | {
                "HOMESERVER_HOST_SNAPSHOT": "/run/homeserver/host.json",
                "HOMESERVER_CAPACITY_SNAPSHOT": "/run/homeserver/capacity.json",
                "HOMESERVER_MEDIA_ROOT": "/data",
                "HOMESERVER_MEDIA_UUID": value("MEDIA_UUID"),
                "HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS": value(
                    "CAPACITY_SNAPSHOT_MAX_AGE_SECONDS"
                ),
            },
            ports=ports("STATUS_PORT", 8080),
            healthcheck=healthcheck(ready_http),
            command=[
                "uvicorn",
                "homeserver_telemetry.app:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8080",
                "--log-level",
                value("LOG_LEVEL").lower(),
            ],
        ),
        "operator": service(
            "operator",
            ["apps", "transfer"],
            [
                bind(appdata, "/srv/appdata"),
                bind(media, "/data"),
                bind(run, "/run/homeserver"),
                bind(env_file.parent, "/project"),
            ],
            profiles=["operator"],
            environment={"HOMESERVER_NATIVE_CONTEXT": "container"},
            user="0:0",
            entrypoint=["python", "-m", "homeserver_common.cli"],
        ),
        "init": service(
            "operator",
            [],
            [
                bind(appdata, "/srv/appdata"),
                bind(media, "/srv/data"),
                bind(value("TRANSCODE_ROOT"), "/srv/transcode"),
                bind(run, "/run/homeserver"),
                bind(env_file.parent, "/project", read_only=True),
            ],
            restart="no",
            user="0:0",
            environment={"HOMESERVER_NATIVE_CONTEXT": "container"},
            command=[
                "python", "-m", "homeserver_common.host", "--env-file",
                "/project/" + env_file.name,
            ],
        ),
        "host-metrics": service(
            "telemetry",
            [],
            [live | {"read_only": False}, bind(media, "/data", read_only=True),
             bind("/sys", "/sys", read_only=True)],
            network_mode="host",
            pid="host",
            environment={
                "HOMESERVER_METRICS_INTERVAL_SECONDS": value("METRICS_INTERVAL_SECONDS"),
                "HOMESERVER_MEDIA_ROOT": "/data",
            },
            command=["python", "/app/scripts/host-metrics.py", "--loop"],
            user=f"{value('SERVICE_UID')}:{value('SERVICE_GID')}",
        ),
    }
    services["host-metrics"].pop("networks")
    for name in ("control-api", "control-worker", "download-gateway", "telemetry", "host-metrics"):
        services[name]["volumes"].append(bind(env_file.parent, "/project", read_only=True))
        services[name]["environment"].update(
            HOMESERVER_ENV_PATH="/project/" + env_file.name,
            HOMESERVER_ENV_MODE=value("ENVIRONMENT"),
        )
        services[name]["entrypoint"] = ["python", "-m", "homeserver_common.runtime"]
    for name in ("control-api", "control-worker", "download-gateway", "telemetry"):
        services[name]["user"] = f"{value('SERVICE_UID')}:{value('SERVICE_GID')}"
    if value("TRANSCODE_MODE") == "intel":
        device = value("INTEL_RENDER_DEVICE")
        services["jellyfin"].update(
            devices=[f"{device}:{device}"],
            group_add=list(dict.fromkeys([value("RENDER_GID"), value("VIDEO_GID")])),
        )
    if value("BYPARR_ENABLED") == "true" or value("ENVIRONMENT") == "prod":
        services["byparr"] = service("byparr", ["apps", "egress"], [], shm_size="512m")
    if images is None:
        for name, filename in (("control-api", "control"), ("telemetry", "telemetry")):
            services[name]["build"] = {
                "context": str(PROJECT_ROOT),
                "dockerfile": f"deploy/Dockerfile.{filename}",
            }
    services["operator"]["restart"] = "no"
    for name, spec in services.items():
        if name not in ("init", "operator"):
            dependencies = spec.get("depends_on", [])
            spec["depends_on"] = {
                dependency: {"condition": "service_started"} for dependency in dependencies
            } | {"init": {"condition": "service_completed_successfully"}}
    return {
        "name": value("INSTANCE_NAME"),
        "services": services,
        "networks": {
            "apps": {"internal": True},
            "transfer": {"internal": True},
            "telemetry": {"internal": True},
            "edge_control": {"internal": False},
            "egress": {"internal": False},
            "egress_transfer": {"internal": False},
        },
    }


def literal_compose(value):
    """Escape all literal dollars before Compose interpolates its input."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [literal_compose(item) for item in value]
    if isinstance(value, dict):
        return {key: literal_compose(item) for key, item in value.items()}
    return value


def atomic_write(path: Path, contents: str | bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents.encode() if isinstance(contents, str) else contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_stack(settings: Settings, path: Path, *, images=None, env_file=None) -> None:
    atomic_write(
        path,
        json.dumps(
            literal_compose(render_stack(settings, images=images, env_file=env_file)), indent=2
        )
        + "\n",
    )
