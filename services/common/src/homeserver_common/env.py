"""Parse dotenv as data. Shell expansion and inline comments are never evaluated."""

import ipaddress
import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .settings import FIELDS, PRODUCTION_REQUIRED, Settings

ALIASES = {
    "ARR_UID": "SERVICE_UID",
    "ARR_GID": "SERVICE_GID",
    "WORKER_INTERVAL": "WORKER_INTERVAL_SECONDS",
    "SOURCE_SLOW_SECONDS": "SOURCE_SLOW_WINDOW_SECONDS",
    "RESTIC_REPOSITORY": "BACKUP_REPOSITORY",
    "RESTIC_PASSWORD_FILE": "BACKUP_PASSWORD_FILE",
    "TELEMETRY_PORT": "STATUS_PORT",
}
INTERNAL = {
    "DB_PATH",
    "CAPACITY_SNAPSHOT",
    "HOST_SNAPSHOT",
    "TELEMETRY_SNAPSHOT",
    "RECOVERY_MODE",
    "MEDIA_ROOTS",
    "QBIT_CREDENTIALS_FILE",
    "WORKER_ONCE",
    "COLLECTOR_TOKEN",
    "COLLECTOR_TOKEN_FILE",
}


def parse_env(text):
    values = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"env line {line_number}: expected KEY=value")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"env line {line_number}: invalid key")
        if key in values:
            raise ValueError(f"{key}: duplicate key")
        if value.startswith('"'):
            try:
                value = json.loads(value)
                if not isinstance(value, str):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError(f"{key}: invalid quoted string") from None
        elif value.startswith("'"):
            if not value.endswith("'") or len(value) < 2:
                raise ValueError(f"{key}: invalid quoted string")
            value = value[1:-1]
        values[key] = value
    return values


def serialize_env(values):
    return "".join(
        f"{key}={json.dumps(str(value), ensure_ascii=False)}\n" for key, value in values.items()
    )


def convert(key, raw, field):
    try:
        if field.kind in ("int", "port"):
            value = int(raw)
        elif field.kind == "float":
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError
        elif field.kind == "bool":
            if raw.lower() not in ("true", "false", "1", "0"):
                raise ValueError
            value = raw.lower() in ("true", "1")
        elif field.kind == "optionalbool":
            if raw not in ("", "true", "false"):
                raise ValueError
            value = raw
        elif field.kind in ("list", "hashlist", "optionallist"):
            value = tuple(part.strip() for part in raw.split(",") if part.strip())
            if (not value and field.kind == "list") or len(set(value)) != len(value):
                raise ValueError
            if field.kind == "hashlist" and any(
                not re.fullmatch(r"[0-9a-fA-F]{40}", item) for item in value
            ):
                raise ValueError
            if field.kind == "hashlist":
                value = tuple(item.lower() for item in value)
        elif field.kind == "json":
            value = json.loads(raw)
            if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
                raise ValueError
        else:
            value = raw
        if field.minimum is not None and value < field.minimum:
            raise ValueError
        if field.kind == "port" and value > 65535:
            raise ValueError
        if field.choices and any(
            item not in field.choices for item in (value if isinstance(value, tuple) else (value,))
        ):
            raise ValueError
        if field.kind == "ip":
            ipaddress.ip_address(value)
        if field.kind == "url" and value:
            url = urlsplit(value)
            if url.scheme not in ("http", "https") or not url.hostname or url.username:
                raise ValueError
            _ = url.port
        if field.kind == "path" and (not value or not Path(value).is_absolute()):
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ValueError(f"{key}: invalid {field.kind}") from None


def load_settings(path: Path, *, mode="dev") -> Settings:
    if mode not in ("dev", "prod"):
        raise ValueError("mode: expected dev or prod")
    path = Path(path).resolve()
    raw = parse_env(path.read_text(encoding="utf-8"))
    for old, new in ALIASES.items():
        old, new = "HOMESERVER_" + old, "HOMESERVER_" + new
        if old in raw:
            if new in raw and raw[old] != raw[new]:
                raise ValueError(f"{new}: conflicting legacy alias {old}")
            raw[new] = raw.pop(old)
    for key in raw:
        if not key.startswith("HOMESERVER_"):
            continue
        name = key[11:].lower()
        if name.upper() in INTERNAL:
            continue
        if name.endswith("_file") and name[:-5] in FIELDS and FIELDS[name[:-5]].secret:
            continue
        if name not in FIELDS:
            raise ValueError(f"{key}: unknown managed key")
    values = {name: field.default for name, field in FIELDS.items()}
    values["environment"] = mode
    if mode == "dev":
        for name in (
            "install_root",
            "appdata_root",
            "media_root",
            "transcode_root",
            "backup_staging_root",
            "run_root",
        ):
            values[name] = str(path.parent / ".runtime/dev" / name.removesuffix("_root"))
        values["access_mode"] = "lan"
    for name, field in FIELDS.items():
        key = "HOMESERVER_" + name.upper()
        if field.secret and key + "_FILE" in raw:
            if key in raw:
                raise ValueError(f"{key}: value and _FILE are mutually exclusive")
            secret_path = Path(raw[key + "_FILE"])
            if not secret_path.is_absolute():
                secret_path = path.parent / secret_path
            try:
                raw[key] = secret_path.read_text(encoding="utf-8").rstrip("\r\n")
            except OSError:
                raise ValueError(f"{key}: secret file unavailable") from None
        if key in raw:
            values[name] = convert(key, raw[key], field)
    if values["environment"] != mode:
        raise ValueError("HOMESERVER_ENVIRONMENT: conflicts with command mode")
    try:
        ZoneInfo(values["timezone"])
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("HOMESERVER_TIMEZONE: invalid timezone") from None
    ports = [
        values[name + "_port"]
        for name in (
            "jellyfin",
            "seerr",
            "sonarr",
            "radarr",
            "prowlarr",
            "bazarr",
            "control",
            "status",
            "qbit_monitor",
        )
    ]
    if len(ports) != len(set(ports)):
        raise ValueError("HOMESERVER_*_PORT: duplicate published ports")
    if values["torrent_max_active"] < max(values["download_max_active"], values["seed_max_active"]):
        raise ValueError("HOMESERVER_TORRENT_MAX_ACTIVE: incompatible active limits")
    if values["source_min_time_gain_percent"] >= 100:
        raise ValueError("HOMESERVER_SOURCE_MIN_TIME_GAIN_PERCENT: must be below 100")
    if values["metrics_max_age_seconds"] < values["metrics_interval_seconds"]:
        raise ValueError("HOMESERVER_METRICS_MAX_AGE_SECONDS: below collection interval")
    if values["capacity_snapshot_max_age_seconds"] < values["metrics_interval_seconds"]:
        raise ValueError("HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS: below collection interval")
    if mode == "dev":
        for name in ("lan_bind_ip", "tailscale_bind_ip", "qbit_peer_bind_ip"):
            if not ipaddress.ip_address(values[name]).is_loopback:
                raise ValueError(f"HOMESERVER_{name.upper()}: dev requires loopback")
    if mode == "prod":
        required = list(PRODUCTION_REQUIRED)
        if values["backup_enabled"]:
            required += ["backup_repository", "backup_password"]
        if values["alerts_enabled"]:
            required += ["alert_webhook_url"]
        for name in required:
            if not values[name] or values[name] in ("unconfigured", "changeme"):
                raise ValueError(f"HOMESERVER_{name.upper()}: required in production")
        if (
            values["access_mode"] in ("tailscale", "both")
            and ipaddress.ip_address(values["tailscale_bind_ip"]).is_loopback
        ):
            raise ValueError(
                "HOMESERVER_TAILSCALE_BIND_IP: production requires explicit Tailscale bind"
            )
    return Settings(values)
