"""Single typed registry for operator configuration."""

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from types import MappingProxyType


@dataclass(frozen=True)
class Field:
    default: object
    kind: str
    consumer: str
    unit: str = ""
    secret: bool = False
    choices: tuple = ()
    minimum: float | None = None
    application: str = "restart"


FIELDS: dict[str, Field] = {}
PRODUCTION_REQUIRED = (
    "media_uuid",
    "arr_token",
    "admin_token",
    "csrf_token",
    "admin_password",
    "qbit_password",
    "sonarr_api_key",
    "radarr_api_key",
    "prowlarr_api_key",
    "seerr_api_key",
    "jellyfin_api_key",
    "bazarr_api_key",
)


def register(names, default, kind="str", consumer="render", **kwargs):
    for name in names.split():
        FIELDS[name.lower()] = Field(default, kind, consumer, **kwargs)


register("CONFIG_VERSION", 1, "int", "loader", minimum=1)
register("ENVIRONMENT", "prod", choices=("dev", "prod"))
register("INSTANCE_NAME", "homeserver")
register("TIMEZONE", "America/Sao_Paulo")
for key, value in {
    "INSTALL_ROOT": "/opt/homeserver",
    "APPDATA_ROOT": "/srv/appdata",
    "MEDIA_ROOT": "/srv/data",
    "TRANSCODE_ROOT": "/srv/transcode",
    "BACKUP_STAGING_ROOT": "/srv/backup-staging",
    "RUN_ROOT": "/run/homeserver",
}.items():
    register(key, value, "path")
register("MEDIA_UUID", "", consumer="mount_guard")
register("QBIT_GATEWAY_URL", "http://download-gateway:8081", "url", "native_config")
register("QBIT_QUEUEING_ENABLED", True, "bool", "qbittorrent")
register(
    "UPLOAD_LIMIT_BYTES DOWNLOAD_LIMIT_BYTES",
    -1,
    "int",
    "qbittorrent",
    unit="bytes/s; -1 derives from Mbit/s",
    minimum=-1,
)
register("SERVICE_UID SERVICE_GID RENDER_GID VIDEO_GID", 1000, "int", minimum=0)
register("NETWORK_INTERFACE", "auto", consumer="metrics")
register("ACCESS_MODE", "tailscale", choices=("tailscale", "lan", "both"))
register("LAN_BIND_IP TAILSCALE_BIND_IP QBIT_PEER_BIND_IP", "127.0.0.1", "ip")
register("TAILSCALE_HOSTNAME", "")
PORTS = dict(
    jellyfin=8096,
    seerr=5055,
    sonarr=8989,
    radarr=7878,
    prowlarr=9696,
    bazarr=6767,
    control=8080,
    status=8081,
    qbit_monitor=18080,
)
for service, port in PORTS.items():
    register(service.upper() + "_PORT", port, "port", minimum=1)
    register(service.upper() + "_PUBLIC_URL", "", "url", "dashboard")
for service, port in {**PORTS, "qbit": 8080}.items():
    if service in ("control", "status", "qbit_monitor"):
        continue
    host = "qbittorrent" if service == "qbit" else service
    register(service.upper() + "_URL", f"http://{host}:{port}", "url", "native_config")
    if service != "qbit":
        register(service.upper() + "_API_KEY", "", consumer="native_config", secret=True)
register("QBIT_PEER_PORT", 6881, "port", minimum=1)
register("TRANSCODE_MODE", "cpu", choices=("cpu", "intel"))
register("INTEL_RENDER_DEVICE", "/dev/dri/renderD128", "path")
register("TRANSCODE_THREADS", 0, "int", "jellyfin", minimum=0)
register("JELLYFIN_ENABLE_MEDIA_DELETION", "", "optionalbool", "jellyfin")
for name, default in {
    "DOWNLOAD_MAX_ACTIVE": 4,
    "SEED_MAX_ACTIVE": 8,
    "TORRENT_MAX_ACTIVE": 12,
    "TORRENT_MAX_CONNECTIONS": 500,
    "TORRENT_MAX_CONNECTIONS_PER_TORRENT": 100,
    "UPLOAD_SLOTS": 20,
    "UPLOAD_SLOTS_PER_TORRENT": 4,
}.items():
    register(name, default, "int", "qbittorrent", minimum=1, application="native")
register("QUEUE_IGNORE_SLOW_TORRENTS", True, "bool", "qbittorrent", application="native")
register("UPLOAD_LIMIT_MBIT", 20.0, "float", "qbittorrent", unit="Mbit/s", minimum=0)
register("DOWNLOAD_LIMIT_MBIT", 0.0, "float", "qbittorrent", unit="Mbit/s", minimum=0)
register("SEED_RATIO_LIMIT", -1.0, "float", "qbittorrent", minimum=-1)
register(
    "SEED_TIME_LIMIT_MINUTES SEED_INACTIVE_LIMIT_MINUTES",
    -1,
    "int",
    "qbittorrent",
    unit="minutes",
    minimum=-1,
)
register("MOVIE_QUEUE_PRIORITY", "seeders", consumer="worker", choices=("seeders",))
register("SERIES_ORDER", "sequential", consumer="worker", choices=("sequential",))
for name, value in {
    "WORKER_INTERVAL_SECONDS": 5,
    "SEARCH_RETRY_SECONDS": 300,
    "SOURCE_STALL_SECONDS": 300,
    "SOURCE_SLOW_WINDOW_SECONDS": 300,
    "SOURCE_SEARCH_RETRY_SECONDS": 300,
    "SOURCE_PROBE_SECONDS": 60,
    "MOVIE_PRIORITY_INTERVAL_SECONDS": 60,
    "HTTP_TIMEOUT_SECONDS": 15,
    "SEARCH_TIMEOUT_SECONDS": 90,
    "WORKER_CYCLE_TIMEOUT_SECONDS": 900,
    "CAPACITY_SNAPSHOT_MAX_AGE_SECONDS": 30,
}.items():
    register(name, value, "float", "worker", unit="seconds", minimum=0.001)
register("SOURCE_MIN_RATE_KIB", 1024, "float", "worker", unit="KiB/s", minimum=0)
register("SOURCE_MIN_TIME_GAIN_PERCENT", 20, "float", "worker", unit="percent", minimum=0)
register("SOURCE_PROBE_ENABLED", True, "bool", "worker")
register("SOURCE_PROTECTED_HASHES", (), "hashlist", consumer="worker", unit="SHA1 infohash")
register("WORKER_HEARTBEAT_MAX_AGE_SECONDS", 90, "float", "health", unit="seconds", minimum=1)
register("SUPERVISOR_INTERVAL_SECONDS", 15, "float", "supervisor", unit="seconds", minimum=1)
register("SUPERVISOR_MAX_RESTARTS", 5, "int", "supervisor", minimum=1)
register("SUPERVISOR_RESTART_WINDOW_SECONDS", 300, "float", "supervisor", unit="seconds", minimum=1)
register("MEDIA_RESOLUTIONS", ("2160", "1080"), "list", "quality", choices=("720", "1080", "2160"))
register(
    "MEDIA_SOURCES",
    ("remux", "bluray", "webdl"),
    "list",
    "quality",
    choices=("remux", "bluray", "webdl"),
)
register("PREFER_DOLBY_VISION PREFER_ATMOS", True, "bool", "quality")
for resolution, minimum in ((720, 10), (1080, 20), (2160, 50)):
    register(
        f"QUALITY_MIN_MIB_PER_MIN_{resolution}",
        minimum,
        "float",
        "quality",
        unit="MiB/minute of main video; 0 disables floor",
        minimum=0,
    )
register("AUTOMATIC_UPGRADES", False, "bool", "quality")
register("AUDIO_LANGUAGES", ("original",), "list", "quality")
register("SUBTITLE_LANGUAGES", ("pt-BR", "en-US"), "list", "subtitles", choices=("pt-BR", "en-US"))
register(
    "SUBTITLE_MATCH_MODES",
    ("release", "compatible_edition"),
    "list",
    "subtitles",
    choices=("release", "compatible_edition"),
)
register(
    "SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES",
    ("pt-BR",),
    "optionallist",
    "subtitles",
    choices=("pt-BR",),
)
register("SUBTITLE_ALLOW_GENERIC_ENGLISH", True, "bool", "subtitles")
register("SUBTITLE_CREDITS_MARGIN_SECONDS", 600, "int", "subtitles", unit="seconds", minimum=0)
register(
    "SUBTITLE_PROVIDERS", ("bazarr", "subdl"), "list", "subtitles", choices=("bazarr", "subdl")
)
register(
    "BAZARR_PROVIDERS",
    ("subdl", "opensubtitlescom"),
    "optionallist",
    "bazarr",
    choices=("subdl", "opensubtitlescom"),
)
register(
    "SUBDL_API_KEY OPENSUBTITLES_USERNAME OPENSUBTITLES_PASSWORD",
    "",
    consumer="subtitles",
    secret=True,
)
register(
    "ARR_TOKEN ADMIN_TOKEN CSRF_TOKEN ADMIN_PASSWORD QBIT_PASSWORD",
    "",
    consumer="control",
    secret=True,
)
register("ADMIN_USERNAME QBIT_USERNAME", "admin", consumer="native_config")
register("PROWLARR_INDEXERS", (), "json", "prowlarr", secret=True)
register("BYPARR_ENABLED", False, "bool")
register("BYPARR_URL", "http://byparr:8191", "url", "prowlarr")
register("METRICS_INTERVAL_SECONDS", 5, "float", "metrics", unit="seconds", minimum=0.001)
register("METRICS_MAX_AGE_SECONDS", 90, "float", "metrics", unit="seconds", minimum=0.001)
register(
    "LOG_LEVEL",
    "INFO",
    consumer="logging",
    choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
)
register("LOG_MAX_SIZE_MB", 10, "int", minimum=1, unit="MB")
register("LOG_MAX_FILES", 3, "int", minimum=1)
register("BACKUP_ENABLED ALERTS_ENABLED", False, "bool", "operations")
register("BACKUP_SCHEDULE", "daily", consumer="backup")
register(
    "BACKUP_REPOSITORY BACKUP_TARGET_REPOSITORY BACKUP_SSH_HOST "
    "BACKUP_SSH_USER BACKUP_SSH_KEY_FILE",
    "",
    consumer="backup",
)
register("BACKUP_PASSWORD BACKUP_TARGET_PASSWORD", "", consumer="backup", secret=True)
for name, value in {
    "BACKUP_KEEP_LAST": 3,
    "BACKUP_KEEP_DAILY": 7,
    "BACKUP_KEEP_WEEKLY": 4,
    "BACKUP_KEEP_MONTHLY": 6,
    "BACKUP_STALE_HOURS": 48,
    "BACKUP_STAGING_MAX_GIB": 100,
    "RELEASE_KEEP_COUNT": 3,
    "ALERT_RETRY_SECONDS": 60,
}.items():
    register(name, value, "int", "operations", minimum=0)
register("ALERT_WEBHOOK_URL", "", "url", "alerts", secret=True)


def string_value(value):
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (tuple, list)):
        return ",".join(map(str, value))
    if isinstance(value, float):
        return format(value, "g")
    return str(value)


@dataclass(frozen=True)
class Settings:
    values: dict = dataclass_field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def __getattr__(self, name):
        if name.endswith("_limit_bytes"):
            if self.values.get(name, -1) >= 0:
                return self.values[name]
            return int(self.values[name.replace("_limit_bytes", "_limit_mbit")] * 125000)
        try:
            return self.values[name]
        except KeyError:
            raise AttributeError(name) from None

    def redacted_dict(self):
        return {
            key: ("[redacted]" if value else "[absent]") if FIELDS[key].secret else value
            for key, value in self.values.items()
        }

    def as_environment(self):
        import json

        result = {
            "HOMESERVER_" + key.upper(): json.dumps(value)
            if FIELDS[key].kind == "json"
            else string_value(value)
            for key, value in self.values.items()
        }
        aliases = {
            "ARR_UID": self.service_uid,
            "ARR_GID": self.service_gid,
            "WORKER_INTERVAL": self.worker_interval_seconds,
            "DB_PATH": "/var/lib/homeserver/control.sqlite",
            "CAPACITY_SNAPSHOT": "/run/homeserver/capacity.json",
            "HOST_SNAPSHOT": "/run/homeserver/host.json",
            "RECOVERY_MODE": "/var/lib/homeserver/RECOVERY_MODE",
        }
        result.update({"HOMESERVER_" + key: string_value(value) for key, value in aliases.items()})
        return result
