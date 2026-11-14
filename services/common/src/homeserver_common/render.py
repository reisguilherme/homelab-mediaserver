"""Render a complete stack without Compose's implicit environment precedence."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .service_environment import environment_for_service
from .settings import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[4]
IMAGES = {
    "jellyfin": "jellyfin/jellyfin@sha256:"
    "357724bf0ae27a672c7cbaa899db2d9abeb13dbd8657ccce750258a4c059d037",
    "jellyfin-proxy": "nginx@sha256:"
    "0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94",
    "public-proxy": "nginx@sha256:"
    "0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94",
    "seerr-proxy": "nginx@sha256:"
    "0985e772fb9f729e6fa0980da05fca5d9c468e870eed43071545afa9d2e27d94",
    "integration-proxy": "homeserver-control:dev",
    "jellyfin-relay": "homeserver-control:dev",
    "public-security": "homeserver-control:dev",
    "service-proxy": "caddy@sha256:"
    "d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f",
    "seerr": "ghcr.io/seerr-team/seerr@sha256:"
    "27602401178d54f1964442287b9f23f67a3fa2252645ee8065839ea1c3f69e45",
    "sonarr": "linuxserver/sonarr:4.0.15",
    "radarr": (
        "lscr.io/linuxserver/radarr@sha256:"
        "adb6c09d6b729ea5e642c99cea35af72702ef476bf4763f153299ac5db9f0b4f"
    ),
    "prowlarr": "linuxserver/prowlarr:1.35.1",
    "qbittorrent": "linuxserver/qbittorrent:5.1.2",
    "bazarr": "linuxserver/bazarr:1.6.2",
    "qbit-monitor": "caddy@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f",
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


def render_storage_extra_override() -> dict:
    """Optional second USB bind; the standard two-pool override is unchanged."""
    return {'services': {
        name: {'volumes': [bind('/srv/external2', '/storage/hdd2',
                                read_only=name != 'control-worker')]}
        for name in ('control-worker', 'control-api', 'download-gateway', 'telemetry',
                     'host-metrics', 'init', 'operator')
    }}


def render_storage_fourth_override() -> dict:
    """Optional third USB bind; earlier two/three-pool overrides stay unchanged."""
    return {'services': {
        name: {'volumes': [bind('/srv/external3', '/storage/hdd3',
                                read_only=name != 'control-worker')]}
        for name in ('control-worker', 'control-api', 'download-gateway', 'telemetry',
                     'host-metrics', 'init', 'operator')
    }}


def render_storage_override() -> dict:
    """Opt-in, fixed technical paths; never derive disk identities from .env."""
    services = {}
    for name in ('sonarr', 'radarr', 'qbittorrent', 'control-worker', 'control-api',
                 'download-gateway', 'telemetry', 'host-metrics', 'operator'):
        readonly = name in ('control-api', 'download-gateway', 'telemetry', 'host-metrics')
        services[name] = {'volumes': [bind('/srv/media-view', '/data', read_only=readonly)]}
    for name, readonly in (('jellyfin', False), ('bazarr', False)):
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


def render_storage_single_override() -> dict:
    """Independent large-disk layout; omit retired media pool bindings."""
    result = render_storage_override()
    services = result['services']
    for name in ('control-worker', 'control-api', 'download-gateway', 'telemetry',
                 'host-metrics', 'init', 'operator'):
        services[name]['volumes'] = [
            value for value in services[name]['volumes']
            if value['target'] not in ('/storage/ssd', '/storage/hdd')
        ]
        services[name]['volumes'].append(
            bind('/srv/external3', '/storage/hdd3', read_only=name != 'control-worker'))
    for name in ('sonarr', 'radarr', 'qbittorrent', 'jellyfin', 'bazarr'):
        services[name]['environment'] = {'HOMESERVER_STORAGE_SINGLE_GUARD': '1'}
        services[name]['volumes'] += [
            bind('/srv/external3', '/run/storage-physical', read_only=True),
            bind('/srv/appdata/control/storage-native.identity',
                 '/run/storage-native.identity', read_only=True),
            bind('/proc/1/mountinfo', '/run/host-mountinfo', read_only=True),
            bind('/dev/disk/by-uuid', '/run/host-uuids', read_only=True),
        ]
    return result


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

    def public_environment(role: str, **overrides: str) -> dict[str, str]:
        return environment_for_service(role, env | {
            'HOMESERVER_SERVICE_ROLE': role, **overrides,
        })

    hardened = {
        'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'read_only': True, 'user': f"{value('SERVICE_UID')}:{value('SERVICE_GID')}",
        'tmpfs': ['/tmp:rw,noexec,nosuid,size=64m'],
        'pids_limit': 128,
    }
    proxy_hardened = hardened | {
        'tmpfs': hardened['tmpfs'] + [
            f"/var/cache/nginx:rw,noexec,nosuid,size=32m,uid={value('SERVICE_UID')},"
            f"gid={value('SERVICE_GID')}",
            f"/var/run:rw,noexec,nosuid,size=4m,uid={value('SERVICE_UID')},"
            f"gid={value('SERVICE_GID')}",
        ],
        'mem_limit': '256m',
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
        "service-proxy": service(
            "service-proxy",
            ["edge_control"],
            [bind(code / "deploy/service-proxy/Caddyfile", "/etc/caddy/Caddyfile",
                  read_only=True)],
            ports=[{"host_ip": address, "published": "80", "target": 80, "protocol": "tcp"}
                   for address in addresses],
            depends_on=["jellyfin-proxy", "qbit-monitor", "telemetry"],
        ),
        "jellyfin": service(
            "jellyfin",
            {'public_jellyfin': {'ipv4_address': '172.30.240.2'}},
            [
                bind(appdata / "jellyfin", "/config"),
                bind(media / "media", "/data/media"),
                bind(value("TRANSCODE_ROOT"), "/transcode"),
            ],
            user=f"{value('SERVICE_UID')}:{value('SERVICE_GID')}",
            environment={"TZ": value("TIMEZONE")},
            dns=['1.1.1.1', '1.0.0.1'],
            cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
        ),
        "jellyfin-proxy": service(
            "jellyfin-proxy",
            {'public_jellyfin': {'ipv4_address': '172.30.240.6', 'gw_priority': 1},
             'public_relay': {'ipv4_address': '172.30.240.36'}, 'edge_control': {}},
            [
                bind(
                    code / "deploy/jellyfin-proxy/nginx.conf",
                    "/etc/nginx/nginx.conf",
                    read_only=True,
                )
            ],
            ports=ports("JELLYFIN_PORT", 8096),
            depends_on=["jellyfin", "jellyfin-relay", "control-api"],
            **proxy_hardened,
        ),
        'jellyfin-relay': service(
            'jellyfin-relay',
            {'public_jellyfin': {'ipv4_address': '172.30.240.3'},
             'public_relay': {'ipv4_address': '172.30.240.34'}},
            [], environment=public_environment('jellyfin-relay'),
            entrypoint=['python', '-m', 'homeserver_common.runtime'],
            command=['uvicorn', 'homeserver_control.api.jellyfin_relay:create_app',
                     '--factory', '--host', '0.0.0.0', '--port', '8083',
                     '--no-proxy-headers', '--no-access-log', '--ws-max-size', '8388608',
                     '--log-level', value('LOG_LEVEL').lower()],
            depends_on=['jellyfin'], mem_limit='512m', **hardened,
        ),
        'public-security': service(
            'public-security',
            {'public_jellyfin': {'ipv4_address': '172.30.240.4'},
             'public_seerr': {'ipv4_address': '172.30.240.19'},
             'public_guard': {'ipv4_address': '172.30.240.50'}},
            [bind(appdata / 'public-security', '/state')],
            environment=public_environment(
                'public-security', HOMESERVER_PUBLIC_ACCESS_STATE_PATH='/state/limits.sqlite3',
                HOMESERVER_PUBLIC_ACCESS_TRUSTED_PROXY_IPS='172.30.240.51'),
            entrypoint=['python', '-m', 'homeserver_common.runtime'],
            command=['uvicorn', 'homeserver_control.api.public_access:create_app',
                     '--factory', '--host', '0.0.0.0', '--port', '8080',
                     '--no-proxy-headers', '--no-access-log',
                     '--log-level', value('LOG_LEVEL').lower()],
            depends_on=['jellyfin', 'seerr'], mem_limit='256m', **hardened,
        ),
        'public-proxy': service(
            'public-proxy',
            {'public_jellyfin': {'ipv4_address': '172.30.240.5'},
             'public_seerr': {'ipv4_address': '172.30.240.20'},
             'public_relay': {'ipv4_address': '172.30.240.35'},
             'public_guard': {'ipv4_address': '172.30.240.51'},
             'public_ingress': {'ipv4_address': '172.30.240.82'}},
            [bind(code / 'deploy/public-proxy/nginx.conf', '/etc/nginx/nginx.conf',
                  read_only=True)],
            ports=ports('PUBLIC_JELLYFIN_PORT', 8097) + ports('PUBLIC_SEERR_PORT', 5056),
            depends_on=['jellyfin', 'seerr', 'jellyfin-relay', 'public-security'],
            **proxy_hardened,
        ),
        'seerr-proxy': service(
            'seerr-proxy',
            {'public_seerr': {'ipv4_address': '172.30.240.21', 'gw_priority': 1},
             'edge_control': {}},
            [bind(code / 'deploy/seerr-proxy/nginx.conf', '/etc/nginx/nginx.conf',
                  read_only=True)],
            ports=ports('SEERR_PORT', 5055), depends_on=['seerr'], **proxy_hardened,
        ),
        'integration-proxy': service(
            'integration-proxy',
            {'seerr_integration': {'ipv4_address': '172.30.240.67'},
             'public_jellyfin': {'ipv4_address': '172.30.240.11'},
             'automation_backends': {'ipv4_address': '172.30.240.98'}},
            [], environment=public_environment('integration-proxy'),
            entrypoint=['python', '-m', 'homeserver_common.runtime'],
            command=['uvicorn', 'homeserver_control.api.integration_proxy:create_app',
                     '--factory', '--host', '0.0.0.0', '--port', '8787',
                     '--no-proxy-headers', '--no-access-log',
                     '--log-level', value('LOG_LEVEL').lower()],
            depends_on=['sonarr', 'radarr'], mem_limit='512m', **hardened,
        ),
        "seerr": service(
            "seerr",
            {'public_seerr': {'ipv4_address': '172.30.240.18'},
             'seerr_integration': {'ipv4_address': '172.30.240.66'}},
            [bind(appdata / "seerr", "/app/config")],
            init=True,
            dns=['1.1.1.1', '1.0.0.1'],
            user=f"{value('SERVICE_UID')}:{value('SERVICE_GID')}",
            environment={
                "TZ": value("TIMEZONE"),
                "NODE_OPTIONS": "--no-network-family-autoselection --dns-result-order=ipv4first",
            },
            cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
        ),
        "sonarr": service(
            "sonarr",
            {'apps': {}, 'edge_control': {}, 'egress': {},
             'automation_backends': {'ipv4_address': '172.30.240.100'}},
            [bind(appdata / "sonarr", "/config"), bind(media, "/data")],
            ports=ports("SONARR_PORT", 8989),
            environment=identity,
        ),
        "radarr": service(
            "radarr",
            {'apps': {}, 'edge_control': {}, 'egress': {},
             'automation_backends': {'ipv4_address': '172.30.240.99'}},
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
            # Preserve the installation's verified resolver override.
            dns=['1.1.1.1', '1.0.0.1'],
        ),
        "bazarr": service(
            "bazarr",
            ["apps", "edge_control", "egress"],
            [bind(appdata / "bazarr", "/config"), bind(media / "media", "/data/media")],
            ports=ports("BAZARR_PORT", 6767),
            environment=identity,
            # The installation's local resolver returns NXDOMAIN for OpenSubtitles.
            dns=["1.1.1.1", "8.8.8.8"],
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
            {'edge_control': {}, 'apps': {}, 'telemetry': {},
             'public_jellyfin': {'ipv4_address': '172.30.240.8'},
             'public_seerr': {'ipv4_address': '172.30.240.23'}},
            [state, live, bind(media, "/data", read_only=True)],
            environment=control_env,
            ports=ports("CONTROL_PORT", 8080),
            healthcheck=healthcheck(ready_http),
            command=[
                "uvicorn",
                "homeserver_control.api.app:app",
                "--no-proxy-headers",
                "--no-access-log",
                "--ws-max-size", "8388608",
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
            {'apps': {}, 'egress': {},
             'public_jellyfin': {'ipv4_address': '172.30.240.7'},
             'public_seerr': {'ipv4_address': '172.30.240.22'}},
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
            environment=environment_for_service('telemetry', env)
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
            {'apps': {}, 'transfer': {},
             'public_jellyfin': {'ipv4_address': '172.30.240.9'},
             'public_seerr': {'ipv4_address': '172.30.240.24'}},
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
    for name in ("control-api", "control-worker", "download-gateway"):
        services[name]["volumes"].append(bind(env_file.parent, "/project", read_only=True))
        services[name]["environment"].update(
            HOMESERVER_ENV_PATH="/project/" + env_file.name,
            HOMESERVER_ENV_MODE=value("ENVIRONMENT"),
        )
    services['telemetry']['volumes'].append(
        bind(env_file, '/project/' + env_file.name, read_only=True))
    for name in ('control-api', 'control-worker', 'download-gateway', 'telemetry', 'host-metrics'):
        services[name]['environment']['HOMESERVER_SERVICE_ROLE'] = name
        services[name]['environment'] = environment_for_service(name, services[name]['environment'])
        if name in ('control-api', 'control-worker', 'download-gateway', 'telemetry'):
            # Loader inputs are consumed before the final child environment filter.
            services[name]['environment'].update(
                HOMESERVER_ENV_PATH='/project/' + env_file.name,
                HOMESERVER_ENV_MODE=value('ENVIRONMENT'),
            )
        services[name]['entrypoint'] = ['python', '-m', 'homeserver_common.runtime']
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
            **{
                name: {
                    'internal': internal, 'enable_ipv6': False,
                    'driver_opts': {'com.docker.network.bridge.name': bridge},
                    'ipam': {'config': [{'subnet': subnet}]},
                }
                for name, bridge, subnet, internal in (
                    ('public_jellyfin', 'hs-public-jf', '172.30.240.0/28', False),
                    ('public_seerr', 'hs-public-seer', '172.30.240.16/28', False),
                    ('public_relay', 'hs-public-rel', '172.30.240.32/28', True),
                    ('public_guard', 'hs-public-auth', '172.30.240.48/28', True),
                    ('seerr_integration', 'hs-public-int', '172.30.240.64/28', True),
                    ('public_ingress', 'hs-public-edge', '172.30.240.80/28', False),
                    ('automation_backends', 'hs-public-arr', '172.30.240.96/28', True),
                )
            },
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
