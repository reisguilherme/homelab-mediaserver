import httpx

from homeserver_common.jellyfin_auth import jellyfin_headers
from homeserver_common.seerr_csrf import SeerrClient

from .arr import bootstrap_arr_account, reconcile_arr
from .bazarr import bootstrap_bazarr_account, reconcile_bazarr
from .media_servers import (
    bootstrap_jellyfin,
    bootstrap_seerr,
    jellyfin_admin_password,
    reconcile_media_server,
)
from .native_config import NativeConfigurationError, ServiceOutcome, environment, plan_settings
from .prowlarr import reconcile_prowlarr
from .qbittorrent import (
    apply_qbit_settings,
    effective_qbit_preferences,
    read_qbit_settings,
    reconcile_qbit_categories,
)
from .service_paths import reconcile_service_path, select_native_base_url

PORTS = {
    "qbit": 8080,
    "radarr": 7878,
    "sonarr": 8989,
    "prowlarr": 9696,
    "bazarr": 6767,
    "jellyfin": 8096,
    "seerr": 5055,
}


async def discover_native_credentials(settings, *, transport=None):
    """Return secrets for atomic operator-env persistence. NEVER print this mapping.

    This authenticates existing credentials, reads the actual Seerr key, and obtains
    a Jellyfin session token. It never regenerates keys or resets account passwords.
    Native startup must already have completed before calling this function.
    """
    env = environment(settings)
    updates = {}
    try:
        async with httpx.AsyncClient(
            base_url=env.get("HOMESERVER_JELLYFIN_URL", "http://jellyfin:8096"),
            timeout=float(env.get("HOMESERVER_HTTP_TIMEOUT_SECONDS", "15")),
            transport=transport,
            trust_env=False,
        ) as client:
            token = env.get("HOMESERVER_JELLYFIN_API_KEY")
            if token:
                response = await client.get("/System/Info", headers=jellyfin_headers(token))
                if response.status_code in (401, 403):
                    token = None
                else:
                    response.raise_for_status()
            if not token:
                response = await client.post(
                    "/Users/AuthenticateByName",
                    headers={
                        "Authorization": (
                            'MediaBrowser Client="HomeServer", Device="Configurator", '
                            'DeviceId="homeserver-config", Version="1"'
                        )
                    },
                    json={
                        "Username": env.get("HOMESERVER_ADMIN_USERNAME", "admin"),
                        "Pw": jellyfin_admin_password(env),
                    },
                )
                response.raise_for_status()
                token = response.json().get("AccessToken")
                updates["HOMESERVER_JELLYFIN_API_KEY"] = token
        async with httpx.AsyncClient(
            base_url=env.get("HOMESERVER_SEERR_URL", "http://seerr:5055"),
            timeout=float(env.get("HOMESERVER_HTTP_TIMEOUT_SECONDS", "15")),
            transport=transport,
            trust_env=False,
        ) as client:
            client = SeerrClient(client)
            key = env.get("HOMESERVER_SEERR_API_KEY")
            if key:
                response = await client.get("/api/v1/settings/main", headers={"X-Api-Key": key})
                if response.status_code in (401, 403):
                    key = None
                else:
                    response.raise_for_status()
            if not key:
                response = await client.post(
                    "/api/v1/auth/jellyfin",
                    json={
                        "username": env.get("HOMESERVER_ADMIN_USERNAME", "admin"),
                        "password": jellyfin_admin_password(env),
                        "serverType": 2,
                    },
                )
                response.raise_for_status()
                response = await client.get("/api/v1/settings/main")
                response.raise_for_status()
                key = response.json().get("apiKey")
                updates["HOMESERVER_SEERR_API_KEY"] = key
        if not isinstance(token, str) or not token or not isinstance(key, str) or not key:
            raise NativeConfigurationError("Native credential discovery incomplete")
        return updates
    except (httpx.HTTPError, KeyError, ValueError, TypeError):
        raise NativeConfigurationError("Native credential discovery failed") from None


async def reconcile_native_settings(settings, mode, *, transport=None):
    env = environment(settings)
    # Migrate native routes before validating any cross-service connection. The
    # old URL remains live until restart; a second apply finishes the connections.
    path_outcomes = []
    for name in ("radarr", "sonarr", "prowlarr", "bazarr", "jellyfin"):
        prefix = "HOMESERVER_" + name.upper()
        url = env.get(prefix + "_URL", "")
        if not url or httpx.URL(url).path in ("", "/"):
            continue
        try:
            key = env.get(prefix + "_API_KEY", "")
            headers = (jellyfin_headers(key) if key else {}) if name == "jellyfin" else {
                "X-Api-Key": key,
            }
            async with httpx.AsyncClient(
                base_url=url, headers=headers, transport=transport,
                timeout=float(env.get("HOMESERVER_HTTP_TIMEOUT_SECONDS", "15")),
                trust_env=False,
            ) as client:
                await select_native_base_url(name, client)
                if client.base_url.path in ("", "/"):
                    changes = await reconcile_service_path(name, settings, client, mode)
                    # Even saved settings require the restart if the new route is absent.
                    path_outcomes.append(ServiceOutcome(
                        name, "planned" if mode in ("apply", "plan") else "drift", changes,
                        "Restart native services, then run config apply again",
                    ))
        except (httpx.HTTPError, ValueError, RuntimeError, KeyError, TypeError):
            # The normal reconciler below reports sanitized per-service errors.
            continue
    if path_outcomes:
        affected = {item.service: item for item in path_outcomes}
        return [
            affected.get(name) or ServiceOutcome(
                "qbittorrent" if name == "qbit" else name,
                "planned" if mode in ("apply", "plan") else "drift",
                message="Connections deferred until native base path restart",
            )
            for name in PORTS
        ]
    outcomes = []
    for name, port in PORTS.items():
        prefix = "HOMESERVER_" + name.upper()
        url = env.get(prefix + "_URL", f"http://{'qbittorrent' if name == 'qbit' else name}:{port}")
        try:
            key = env.get(prefix + "_API_KEY", "")
            headers = (jellyfin_headers(key) if key else {}) if name == "jellyfin" else {
                "X-Api-Key": key,
            }
            async with httpx.AsyncClient(
                base_url=url,
                headers=headers,
                timeout=float(env.get("HOMESERVER_HTTP_TIMEOUT_SECONDS", "15")),
                transport=transport,
                trust_env=False,
            ) as client:
                await select_native_base_url(name, client)
                if name == "qbit":
                    changes = (
                        await apply_qbit_settings(settings, client)
                        if mode == "apply"
                        else plan_settings(
                            "qbittorrent",
                            await read_qbit_settings(settings, client),
                            effective_qbit_preferences(settings),
                        )
                    )
                    if mode != "apply":
                        changes += await reconcile_qbit_categories(client, mode)
                    outcome = ServiceOutcome(
                        "qbittorrent",
                        "drift"
                        if changes and mode == "verify"
                        else "planned"
                        if changes and mode == "plan"
                        else "verified",
                        changes,
                    )
                elif name in ("radarr", "sonarr"):
                    account_changes = await bootstrap_arr_account(name, settings, client, mode)
                    outcome = await reconcile_arr(name, settings, client, mode)
                    outcome.changes = account_changes + outcome.changes
                    if account_changes and mode != "apply" and outcome.status == "verified":
                        outcome.status = "planned" if mode == "plan" else "drift"
                elif name == "prowlarr":
                    account_changes = await bootstrap_arr_account(name, settings, client, mode)
                    outcome = await reconcile_prowlarr(settings, client, mode)
                    outcome.changes = account_changes + outcome.changes
                    if account_changes and mode != "apply" and outcome.status == "verified":
                        outcome.status = "planned" if mode == "plan" else "drift"
                elif name == "bazarr":
                    account_changes = await bootstrap_bazarr_account(settings, client, mode)
                    outcome = await reconcile_bazarr(settings, client, mode)
                    outcome.changes = account_changes + outcome.changes
                    if account_changes and mode != "apply" and outcome.status == "verified":
                        outcome.status = "planned" if mode == "plan" else "drift"
                else:
                    early = await (
                        bootstrap_jellyfin(settings, client, mode)
                        if name == "jellyfin"
                        else bootstrap_seerr(settings, client, mode)
                    )
                    outcome = early or await reconcile_media_server(
                        name, settings, client, mode, transport=transport
                    )
                path_changes = await reconcile_service_path(name, settings, client, mode)
                outcome.changes += path_changes
                if path_changes:
                    outcome.message = "Native base path changed; restart this service"
                    outcome.status = "planned" if mode in ("apply", "plan") else "drift"
            outcomes.append(outcome)
        except (httpx.HTTPError, ValueError, RuntimeError, KeyError, TypeError) as error:
            # Never stringify upstream errors: URLs and response bodies can carry secrets.
            reason = (
                f"HTTP {error.response.status_code}"
                if isinstance(error, httpx.HTTPStatusError)
                else type(error).__name__
            )
            outcomes.append(
                ServiceOutcome("qbittorrent" if name == "qbit" else name, "failed", message=reason)
            )
    return outcomes


async def plan_native_settings(settings, *, transport=None):
    return await reconcile_native_settings(settings, "plan", transport=transport)


async def apply_native_settings(settings, *, transport=None):
    return await reconcile_native_settings(settings, "apply", transport=transport)


async def verify_native_settings(settings, *, transport=None):
    return await reconcile_native_settings(settings, "verify", transport=transport)
