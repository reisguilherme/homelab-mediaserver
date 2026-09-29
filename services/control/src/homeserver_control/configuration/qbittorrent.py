import json
from decimal import Decimal

import httpx

from .native_config import NativeConfigurationError, environment, plan_settings


def adopt_qbit_preferences(current: dict) -> dict[str, str]:
    """Import effective values without rounding and without native private fields."""
    names = {
        "max_active_downloads": "DOWNLOAD_MAX_ACTIVE",
        "max_active_uploads": "SEED_MAX_ACTIVE",
        "max_active_torrents": "TORRENT_MAX_ACTIVE",
        "up_limit": "UPLOAD_LIMIT_BYTES",
        "dl_limit": "DOWNLOAD_LIMIT_BYTES",
        "max_ratio": "SEED_RATIO_LIMIT",
        "max_seeding_time": "SEED_TIME_LIMIT_MINUTES",
        "max_inactive_seeding_time": "SEED_INACTIVE_LIMIT_MINUTES",
        "max_uploads": "UPLOAD_SLOTS",
        "max_uploads_per_torrent": "UPLOAD_SLOTS_PER_TORRENT",
        "dont_count_slow_torrents": "QUEUE_IGNORE_SLOW_TORRENTS",
        "max_connec": "TORRENT_MAX_CONNECTIONS",
        "max_connec_per_torrent": "TORRENT_MAX_CONNECTIONS_PER_TORRENT",
        "queueing_enabled": "QBIT_QUEUEING_ENABLED",
    }
    return {
        "HOMESERVER_" + name: str(current[key]).lower()
        for key, name in names.items()
        if key in current
    }


def qbit_preferences(settings) -> dict[str, object]:
    env = environment(settings)

    def integer(key, default):
        return int(env.get("HOMESERVER_" + key, default))

    exact = env.get("HOMESERVER_UPLOAD_LIMIT_BYTES")
    upload = (
        int(exact)
        if exact and int(exact) >= 0
        else int(Decimal(env.get("HOMESERVER_UPLOAD_LIMIT_MBIT", "20")) * 125_000)
    )
    active_seeds = integer("SEED_MAX_ACTIVE", -1)
    # A finite combined limit would still queue finished torrents even when
    # seeding itself is unlimited. Keep the download limit independent.
    active_total = -1 if active_seeds == -1 else integer("TORRENT_MAX_ACTIVE", -1)
    return {
        # Router mappings are prohibited, including WebUI mappings.
        "upnp": False,
        "web_ui_upnp": False,
        "queueing_enabled": env.get("HOMESERVER_QBIT_QUEUEING_ENABLED", "true") == "true",
        "max_active_downloads": integer("DOWNLOAD_MAX_ACTIVE", 10),
        "max_active_uploads": active_seeds,
        "max_active_torrents": active_total,
        "max_connec": integer("TORRENT_MAX_CONNECTIONS", 500),
        "max_connec_per_torrent": integer("TORRENT_MAX_CONNECTIONS_PER_TORRENT", 100),
        "up_limit": upload,
        "max_ratio": float(env.get("HOMESERVER_SEED_RATIO_LIMIT", "-1")),
        "max_ratio_enabled": float(env.get("HOMESERVER_SEED_RATIO_LIMIT", "-1")) >= 0,
        "max_seeding_time": integer("SEED_TIME_LIMIT_MINUTES", -1),
        "max_seeding_time_enabled": integer("SEED_TIME_LIMIT_MINUTES", -1) >= 0,
        "max_inactive_seeding_time": integer("SEED_INACTIVE_LIMIT_MINUTES", -1),
        "max_inactive_seeding_time_enabled": integer("SEED_INACTIVE_LIMIT_MINUTES", -1) >= 0,
        "max_uploads": integer("UPLOAD_SLOTS", 20),
        "max_uploads_per_torrent": integer("UPLOAD_SLOTS_PER_TORRENT", 4),
        "dont_count_slow_torrents": env.get("HOMESERVER_QUEUE_IGNORE_SLOW_TORRENTS", "false")
        == "true",
        "dl_limit": integer("DOWNLOAD_LIMIT_BYTES", -1)
        if integer("DOWNLOAD_LIMIT_BYTES", -1) >= 0
        else int(Decimal(env.get("HOMESERVER_DOWNLOAD_LIMIT_MBIT", "0")) * 125_000),
        # Pause is the only allowed seeding-limit action; never delete native media.
        "max_ratio_act": 0,
    }


def effective_qbit_preferences(settings):
    """Native qBit stores bandwidth in integer KiB, despite accepting API bytes."""
    desired = qbit_preferences(settings)
    for key in ("up_limit", "dl_limit"):
        value = desired[key]
        desired[key] = 0 if value <= 0 else max(1, value // 1024) * 1024
    return desired


async def read_qbit_settings(settings, client: httpx.AsyncClient) -> dict:
    env = environment(settings)
    response = await client.post(
        "/api/v2/auth/login",
        data={
            "username": env.get("HOMESERVER_QBIT_USERNAME", "admin"),
            "password": env.get("HOMESERVER_QBIT_PASSWORD", ""),
        },
    )
    if response.status_code != 200 or response.text.strip() != "Ok.":
        raise NativeConfigurationError("qBittorrent authentication rejected")
    response = await client.get("/api/v2/app/preferences")
    response.raise_for_status()
    return response.json()


async def apply_qbit_settings(settings, client: httpx.AsyncClient):
    current = await read_qbit_settings(settings, client)
    category_changes = await reconcile_qbit_categories(client, "apply")
    desired = effective_qbit_preferences(settings)
    changes = plan_settings("qbittorrent", current, desired)
    if not changes:
        return category_changes
    payload = {key: value for key, value in desired.items() if current.get(key) != value}
    for key in ("max_ratio", "max_seeding_time", "max_inactive_seeding_time"):
        if key in payload or key + "_enabled" in payload:
            payload[key] = desired[key]
            payload[key + "_enabled"] = desired[key + "_enabled"]
    try:
        response = await client.post(
            "/api/v2/app/setPreferences", data={"json": json.dumps(payload)}
        )
        response.raise_for_status()
    except httpx.TransportError:
        # A lost response may have committed: read back before treating it as failure.
        pass
    observed = await client.get("/api/v2/app/preferences")
    observed.raise_for_status()
    if plan_settings("qbittorrent", observed.json(), desired):
        raise NativeConfigurationError("qBittorrent read-back drift")
    return category_changes + changes


async def reconcile_qbit_categories(client: httpx.AsyncClient, mode: str):
    """Bootstrap only owned empty categories through the authenticated internal API."""
    endpoint = "/api/v2/torrents/categories"
    response = await client.get(endpoint)
    response.raise_for_status()
    current = response.json()
    if not isinstance(current, dict):
        raise NativeConfigurationError("qBittorrent category capability unavailable")
    changes = []
    for name in ("radarr", "sonarr"):
        if name in current:
            continue  # Existing save paths and all other categories remain native-managed.
        changes += plan_settings("qbittorrent", {}, {"category." + name: "present"})
        if mode == "apply":
            try:
                response = await client.post(
                    "/api/v2/torrents/createCategory", data={"category": name, "savePath": ""}
                )
                if response.status_code != 409:
                    response.raise_for_status()
            except httpx.TransportError:
                pass
            observed = await client.get(endpoint)
            observed.raise_for_status()
            if not isinstance(observed.json(), dict) or name not in observed.json():
                raise NativeConfigurationError("qBittorrent category read-back drift")
    return changes
