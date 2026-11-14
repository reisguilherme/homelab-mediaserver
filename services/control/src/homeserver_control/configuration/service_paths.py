"""Native base paths, including safe discovery of a pre-migration installation."""

from urllib.parse import urlsplit

from .native_config import NativeConfigurationError, SettingChange, environment

NATIVE_PATHS = {"radarr", "sonarr", "prowlarr", "bazarr", "jellyfin"}


async def select_native_base_url(service, client):
    if service not in NATIVE_PATHS or client.base_url.path in ("", "/"):
        return
    endpoint = (
        "/System/Info/Public"
        if service == "jellyfin"
        else "/api/system/status"
        if service == "bazarr"
        else f"/api/{'v1' if service == 'prowlarr' else 'v3'}/system/status"
    )
    response = await client.get(endpoint)
    missing = response.status_code == 404
    if response.is_redirect:
        # Arr sends unmatched routes to login; Jellyfin may send them to /web.
        # Never follow redirects or forward API credentials to another origin.
        target = response.url.join(response.headers.get("location", ""))
        missing = (target.scheme, target.host, target.port) == (
            client.base_url.scheme, client.base_url.host, client.base_url.port
        )
    elif response.is_success:
        missing = response.headers.get("content-type", "").startswith("text/html")
        if not missing and not isinstance(response.json(), dict):
            raise NativeConfigurationError("Native base path discovery unsupported")
    if not missing:
        response.raise_for_status()
        return
    root = client.base_url.copy_with(path="/")
    response = await client.get(root.join(endpoint))
    response.raise_for_status()
    if not isinstance(response.json(), dict):
        raise NativeConfigurationError("Native base path discovery unsupported")
    client.base_url = root


async def reconcile_service_path(service, settings, client, mode):
    if service not in NATIVE_PATHS:
        return []
    env = environment(settings)
    url = env.get(f"HOMESERVER_{service.upper()}_URL", "")
    desired = urlsplit(url).path.rstrip("/")
    if not desired:
        return []  # Explicit legacy fixtures and external integrations keep their root.
    endpoint = (
        "/System/Configuration/network"
        if service == "jellyfin"
        else "/api/system/settings"
        if service == "bazarr"
        else f"/api/{'v1' if service == 'prowlarr' else 'v3'}/config/host"
    )
    response = await client.get(endpoint)
    response.raise_for_status()
    current = response.json()
    key = "BaseUrl" if service == "jellyfin" else "base_url" if service == "bazarr" else "urlBase"
    section = current.get("general", {}) if service == "bazarr" else current
    if not isinstance(section, dict) or key not in section:
        raise NativeConfigurationError("Native base path settings unsupported")
    if section[key].rstrip("/") == desired:
        return []
    changes = [SettingChange(service, key, section[key], desired, restart_required=True)]
    if mode == "apply":
        if service == "bazarr":
            response = await client.post(endpoint, data={"settings-general-base_url": desired})
        elif service == "jellyfin":
            response = await client.post(endpoint, json=current | {key: desired})
        else:
            response = await client.put(endpoint, json=current | {key: desired})
        response.raise_for_status()
        response = await client.get(endpoint)
        # Jellyfin immediately rejects the old prefix but does not install the
        # new route until restart. The successful save remains planned, never
        # verified; the next apply must read it back after restarting.
        if service == "jellyfin" and response.status_code in (302, 404):
            return changes
        response.raise_for_status()
        observed = response.json()
        observed = observed.get("general", {}) if service == "bazarr" else observed
        if observed.get(key, "").rstrip("/") != desired:
            raise NativeConfigurationError("Native base path read-back drift")
    return changes
