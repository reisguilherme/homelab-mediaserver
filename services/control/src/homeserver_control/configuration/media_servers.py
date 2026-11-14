import httpx

from homeserver_common.jellyfin_auth import jellyfin_headers
from homeserver_common.seerr_csrf import SeerrClient

from .arr import collection
from .native_config import NativeConfigurationError, ServiceOutcome, environment, plan_settings


def jellyfin_admin_password(env: dict[str, str]) -> str:
    return env.get('HOMESERVER_JELLYFIN_ADMIN_PASSWORD') or env.get('HOMESERVER_ADMIN_PASSWORD', '')


def seerr_jellyfin_connection():
    return {'ip': 'integration-proxy', 'port': 8787, 'urlBase': '/jellyfin', 'useSsl': False}


async def bootstrap_jellyfin(settings, client, mode):
    env = environment(settings)
    response = await client.get("/System/Info/Public")
    response.raise_for_status()
    completed = response.json().get("StartupWizardCompleted")
    if not isinstance(completed, bool):
        return ServiceOutcome("jellyfin", "unsupported", message="Unknown startup capability")
    if not completed:
        changes = plan_settings(
            "jellyfin",
            {},
            {
                "startup.account": env.get("HOMESERVER_ADMIN_USERNAME", "admin"),
                "password": jellyfin_admin_password(env),
            },
        )
        if mode != "apply":
            return ServiceOutcome("jellyfin", "planned" if mode == "plan" else "drift", changes)
        if not jellyfin_admin_password(env):
            raise NativeConfigurationError("Jellyfin startup requires explicit admin password")
        # Upstream GET initializes the first native user before POST updates it.
        response = await client.get("/Startup/User")
        response.raise_for_status()
        response = await client.post(
            "/Startup/User",
            json={
                "Name": env.get("HOMESERVER_ADMIN_USERNAME", "admin"),
                "Password": jellyfin_admin_password(env),
            },
        )
        # 403 means password already set after interrupted startup; do not reset it.
        if response.status_code != 403:
            response.raise_for_status()
        # Authenticate before completing, so conflicting credentials cannot seal setup.
    token_valid = False
    if completed and client.headers.get("Authorization"):
        response = await client.get("/System/Info")
        if response.status_code not in (401, 403):
            response.raise_for_status()
            token_valid = True
    if not token_valid:
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
        if not token:
            raise NativeConfigurationError("Jellyfin authentication token missing")
        client.headers.update(jellyfin_headers(token))
    if not completed:
        try:
            response = await client.post("/Startup/Complete")
            response.raise_for_status()
        except httpx.TransportError:
            pass
        observed = await client.get("/System/Info/Public")
        observed.raise_for_status()
        if observed.json().get("StartupWizardCompleted") is not True:
            raise NativeConfigurationError("Jellyfin startup read-back drift")
    return None


async def bootstrap_seerr(settings, client, mode):
    client = SeerrClient(client)
    env = environment(settings)
    response = await client.get("/api/v1/settings/public")
    response.raise_for_status()
    initialized = response.json().get("initialized")
    if not isinstance(initialized, bool):
        return ServiceOutcome(
            "seerr", "unsupported", message="Unknown Seerr initialization capability"
        )
    if initialized:
        if not env.get("HOMESERVER_SEERR_API_KEY"):
            return ServiceOutcome(
                "seerr",
                "unsupported",
                message="Adopt existing Seerr API key; credentials are not reset",
            )
        return None
    changes = plan_settings(
        "seerr",
        {},
        {
            "startup.account": env.get("HOMESERVER_ADMIN_USERNAME", "admin"),
            "password": jellyfin_admin_password(env),
        },
    )
    if mode != "apply":
        return ServiceOutcome("seerr", "planned" if mode == "plan" else "drift", changes)
    if not jellyfin_admin_password(env):
        raise NativeConfigurationError(
            "Seerr initialization requires explicit Jellyfin admin credentials"
        )
    credentials = {
        "username": env.get("HOMESERVER_ADMIN_USERNAME", "admin"),
        "password": jellyfin_admin_password(env),
        "serverType": 2,
    }
    response = await client.post("/api/v1/auth/jellyfin", json=credentials)
    if response.status_code == 500:
        # Fresh Seerr needs connection fields; interrupted setup already has them
        # and rejects a hostname on login. Try existing configuration first.
        connection = seerr_jellyfin_connection()
        response = await client.post(
            "/api/v1/auth/jellyfin",
            json=credentials
            | {
                'hostname': connection['ip'],
                **{key: value for key, value in connection.items() if key != 'ip'},
            },
        )
    response.raise_for_status()
    # Existing users/passwords are never reset; Jellyfin native authentication creates
    # the first Seerr account only when no accounts exist.
    return None


async def reconcile_media_server(service, settings, client, mode, *, transport=None):
    if service == "seerr":
        return await reconcile_seerr(settings, client, mode, transport=transport)
    env = environment(settings)
    users = await client.get("/Users")
    users.raise_for_status()
    username = env.get("HOMESERVER_ADMIN_USERNAME", "admin")
    changes = []
    if "HOMESERVER_TRANSCODE_THREADS" in env:
        endpoint = "/System/Configuration/encoding"
        response = await client.get(endpoint)
        if response.status_code in (404, 405):
            return ServiceOutcome("jellyfin", "unsupported", message="Encoding API unavailable")
        response.raise_for_status()
        encoding = response.json()
        if not isinstance(encoding, dict) or "EncodingThreadCount" not in encoding:
            return ServiceOutcome(
                "jellyfin", "unsupported", message="Encoding thread capability unavailable"
            )
        desired_encoding = {"EncodingThreadCount": int(env["HOMESERVER_TRANSCODE_THREADS"])}
        encoding_changes = plan_settings("jellyfin", encoding, desired_encoding)
        changes += encoding_changes
        if encoding_changes and mode == "apply":
            try:
                response = await client.post(endpoint, json=encoding | desired_encoding)
                response.raise_for_status()
            except httpx.TransportError:
                pass
            observed = await client.get(endpoint)
            observed.raise_for_status()
            if plan_settings("jellyfin", observed.json(), desired_encoding):
                raise NativeConfigurationError("Jellyfin encoding read-back drift")
    if not any(user["Name"] == username for user in users.json()):
        changes += plan_settings(
            "jellyfin",
            {},
            {"account": username, "password": jellyfin_admin_password(env)},
        )
        if mode == "apply":
            if not jellyfin_admin_password(env):
                raise NativeConfigurationError("New Jellyfin account requires password")
            try:
                response = await client.post(
                    "/Users/New",
                    json={"Name": username, "Password": jellyfin_admin_password(env)},
                )
                response.raise_for_status()
            except httpx.TransportError:
                pass
            users = await client.get("/Users")
            users.raise_for_status()
            if not any(user["Name"] == username for user in users.json()):
                raise NativeConfigurationError("Jellyfin user read-back drift")
    matches = [user for user in users.json() if user["Name"] == username]
    if len(matches) > 1:
        raise NativeConfigurationError("Ambiguous Jellyfin user identity")
    deletion = env.get("HOMESERVER_JELLYFIN_ENABLE_MEDIA_DELETION", "")
    if matches:
        user = matches[0]
        policy = user.get("Policy")
        if not isinstance(policy, dict):
            return ServiceOutcome(
                "jellyfin", "unsupported", changes, "Native user policy capability unavailable"
            )
        desired_policy = {
            "IsAdministrator": True,
            "EnableCollectionManagement": True,
            "EnableSubtitleManagement": True,
        }
        if deletion:
            desired_policy["EnableContentDeletion"] = deletion == "true"
        policy_changes = plan_settings("jellyfin", policy, desired_policy)
        changes += policy_changes
        if policy_changes and mode == "apply":
            payload = policy | desired_policy
            # The pinned Jellyfin 10.10.7 writes this permission but omits it
            # from GetUserDto, which always reports false. Grant it alongside
            # owner changes, without treating that broken read-back as drift.
            if "EnableLyricManagement" in policy:
                payload["EnableLyricManagement"] = True
            try:
                response = await client.post(
                    f"/Users/{user['Id']}/Policy", json=payload
                )
                response.raise_for_status()
            except httpx.TransportError:
                pass
            observed = await client.get("/Users")
            observed.raise_for_status()
            users_by_id = [item for item in observed.json() if item.get("Id") == user["Id"]]
            if len(users_by_id) != 1 or plan_settings(
                "jellyfin", users_by_id[0].get("Policy", {}), desired_policy
            ):
                raise NativeConfigurationError("Jellyfin user policy read-back drift")
    for name, kind, path in [
        ("Movies", "movies", "/data/media/movies"),
        ("TV", "tvshows", "/data/media/tv"),
    ]:
        response = await client.get("/Library/VirtualFolders")
        response.raise_for_status()
        libraries = response.json()
        matches = [library for library in libraries if path in library.get("Locations", [])]
        if matches:
            if len(matches) != 1:
                raise NativeConfigurationError("Ambiguous Jellyfin library identity")
            if kind == "movies" and matches[0].get("CollectionType") == "movies":
                changes += await reconcile_jellyfin_collections(env, client, matches[0], mode)
            continue
        if any(library.get("Name") == name for library in libraries):
            return ServiceOutcome(
                "jellyfin",
                "unsupported",
                changes,
                "Library name exists with different path; explicitly adopt native path",
            )
        changes += plan_settings("jellyfin", {}, {f"library.{name}": path})
        if mode == "apply":
            try:
                response = await client.post(
                    "/Library/VirtualFolders",
                    # Materialize CollectionFolders before Seerr discovers MediaFolders.
                    params={"name": name, "collectionType": kind, "refreshLibrary": "true"},
                    json={
                        "LibraryOptions": {
                            "PathInfos": [{"Path": path}],
                            "EnableRealtimeMonitor": True,
                        } | ({
                            "AutomaticallyAddToCollection": env.get(
                                "HOMESERVER_JELLYFIN_AUTO_COLLECTIONS", "true"
                            ) == "true",
                        } if kind == "movies" else {})
                    },
                )
                response.raise_for_status()
            except httpx.TransportError:
                pass
            observed = await client.get("/Library/VirtualFolders")
            observed.raise_for_status()
            if not any(path in library.get("Locations", []) for library in observed.json()):
                raise NativeConfigurationError("Jellyfin library read-back drift")
    return ServiceOutcome(
        "jellyfin",
        "drift"
        if changes and mode == "verify"
        else "planned"
        if changes and mode == "plan"
        else "verified",
        changes,
    )


async def reconcile_jellyfin_collections(env, client, library, mode):
    library_id = library.get("ItemId")
    options = library.get("LibraryOptions")
    if not library_id or not isinstance(options, dict):
        raise NativeConfigurationError("Jellyfin library options capability unavailable")
    desired = env.get("HOMESERVER_JELLYFIN_AUTO_COLLECTIONS", "true") == "true"
    key = f"library.{library_id}.AutomaticallyAddToCollection"
    changes = plan_settings(
        "jellyfin", {key: options.get("AutomaticallyAddToCollection")}, {key: desired}
    )
    if changes and mode == "apply":
        try:
            response = await client.post(
                "/Library/VirtualFolders/LibraryOptions",
                json={
                    "Id": library_id,
                    "LibraryOptions": options | {"AutomaticallyAddToCollection": desired},
                },
            )
            response.raise_for_status()
        except httpx.TransportError:
            pass
        response = await client.get("/Library/VirtualFolders")
        response.raise_for_status()
        observed = [row for row in response.json() if row.get("ItemId") == library_id]
        if len(observed) != 1 or observed[0].get("LibraryOptions", {}).get(
            "AutomaticallyAddToCollection"
        ) is not desired:
            raise NativeConfigurationError("Jellyfin collections read-back drift")
    return changes


async def reconcile_seerr_security(env, client, mode):
    origin = env.get('HOMESERVER_PUBLIC_SEERR_ORIGIN')
    if not origin:
        return []
    parsed = httpx.URL(origin)
    if parsed.scheme != 'https' or not parsed.host or parsed.username or parsed.password:
        raise NativeConfigurationError('Public Seerr requires an HTTPS origin')
    changes = []
    for category, desired in [
        ('main', {'newPlexLogin': False}),
        ('network', {'csrfProtection': True, 'trustProxy': True}),
    ]:
        endpoint = f'/api/v1/settings/{category}'
        response = await client.get(endpoint)
        response.raise_for_status()
        actual = response.json()
        if not isinstance(actual, dict) or not desired.keys() <= actual.keys():
            raise NativeConfigurationError('Native Seerr security settings unavailable')
        planned = plan_settings('seerr', actual, desired)
        changes += planned
        if planned and mode == 'apply':
            try:
                response = await client.post(endpoint, json=desired)
                response.raise_for_status()
            except httpx.TransportError:
                pass
            observed = await client.get(endpoint)
            observed.raise_for_status()
            if plan_settings('seerr', observed.json(), desired):
                raise NativeConfigurationError('Native Seerr security settings read-back drift')
    return changes


async def reconcile_seerr(settings, client, mode, *, transport=None):
    client = SeerrClient(client)
    env = environment(settings)
    changes = await reconcile_seerr_security(env, client, mode)
    endpoint = "/api/v1/settings/jellyfin"
    response = await client.get(endpoint)
    response.raise_for_status()
    actual = response.json()
    desired = seerr_jellyfin_connection()
    connection_changes = plan_settings("seerr", actual, desired)
    changes += connection_changes
    if connection_changes and mode == "apply":
        try:
            # Native POST merges connection fields into existing settings. GET
            # also exposes read-only name/libraries, which cannot be echoed back.
            response = await client.post(endpoint, json=desired)
            response.raise_for_status()
        except httpx.TransportError:
            pass
        observed = await client.get(endpoint)
        observed.raise_for_status()
        if plan_settings("seerr", observed.json(), desired):
            raise NativeConfigurationError("Seerr connection read-back drift")
    for service, port in [("radarr", 7878), ("sonarr", 8989)]:
        api_key = env.get(f"HOMESERVER_{service.upper()}_API_KEY")
        if not api_key:
            continue
        url = env.get(f"HOMESERVER_{service.upper()}_URL", f"http://{service}:{port}")
        async with httpx.AsyncClient(
            base_url=url,
            headers={"X-Api-Key": api_key},
            transport=transport,
            timeout=float(env.get("HOMESERVER_HTTP_TIMEOUT_SECONDS", "15")),
            trust_env=False,
        ) as arr:
            response = await arr.get("/api/v3/qualityprofile")
            response.raise_for_status()
            profiles = [item for item in response.json() if item.get("name") == "HomeServer"]
        named_profiles = "HOMESERVER_DOWNLOAD_DEFAULT_PROFILE" in env
        if len(profiles) != 1 and not named_profiles:
            if mode == "plan":
                changes += plan_settings(
                    "seerr", {}, {f"{service}.profile": "Resolve native ID after Arr apply"}
                )
                continue
            raise NativeConfigurationError(
                "Seerr requires verified native HomeServer quality profile"
            )
        desired_arr = {
            "name": f"HomeServer {service.title()}",
            "hostname": "integration-proxy",
            "port": 8787,
            "useSsl": False,
            "baseUrl": f"/{service}",
            "apiKey": api_key,
            "activeDirectory": "/data/media/movies" if service == "radarr" else "/data/media/tv",
            "is4k": False,
            "isDefault": True,
            "syncEnabled": True,
            "preventSearch": True,
        }
        if service == "radarr":
            desired_arr["minimumAvailability"] = "released"
        else:
            desired_arr["enableSeasonFolders"] = True
        if named_profiles:
            from .quality_profiles import reconcile_seerr_profile_references

            async with httpx.AsyncClient(
                base_url=url, headers={"X-Api-Key": api_key}, transport=transport,
                timeout=float(env.get("HOMESERVER_HTTP_TIMEOUT_SECONDS", "15")), trust_env=False,
            ) as arr:
                changes += await reconcile_seerr_profile_references(
                    service, env, client, arr, desired_arr, mode
                )
        else:
            desired_arr.update(activeProfileId=profiles[0]["id"],
                               activeProfileName=profiles[0]["name"])
            changes += await collection(
                "seerr", client, f"/api/v1/settings/{service}", desired_arr, "name", mode
            )
    # Jellyseerr 2.7.3's bare GET /library disables all libraries. Read the
    # connection resource instead, which is read-only on both API generations.
    response = await client.get(endpoint)
    response.raise_for_status()
    libraries = response.json().get("libraries")
    if not isinstance(libraries, list):
        return ServiceOutcome("seerr", "unsupported", changes, "Unknown native library shape")
    enabled_ids = {str(item["id"]) for item in libraries if item.get("enabled") is True}
    if mode == "apply" and not libraries:
        response = await client.post("/api/v1/settings/jellyfin/library/sync")
        if response.status_code == 404:
            response = await client.get(
                "/api/v1/settings/jellyfin/library",
                params={"sync": "true"} | (
                    {"enable": ",".join(sorted(enabled_ids))} if enabled_ids else {}
                ),
            )
        if response.status_code == 501:
            return ServiceOutcome(
                "seerr", "unsupported", changes, "Native library sync unsupported"
            )
        response.raise_for_status()
        libraries = response.json()
    if not isinstance(libraries, list):
        return ServiceOutcome("seerr", "unsupported", changes, "Unknown native library shape")
    if not libraries:
        changes += plan_settings("seerr", {}, {"libraries": "Sync existing Jellyfin libraries"})
        return ServiceOutcome(
            "seerr",
            "planned" if mode == "plan" else "drift",
            changes,
            "No Jellyfin libraries discovered; confirm connection and sync before initialization",
        )
    pending = [
        library
        for library in libraries
        if library.get("name") in ("Movies", "TV") and library.get("enabled") is not True
    ]
    enabled_ids |= {str(library["id"]) for library in pending}
    for library in pending:
        changes += plan_settings(
            "seerr",
            {f"library.{library['id']}.enabled": library.get("enabled")},
            {f"library.{library['id']}.enabled": True},
        )
    if mode == "apply" and pending:
        for library in pending:
            try:
                response = await client.put(
                    f"/api/v1/settings/jellyfin/library/{library['id']}", json={"enabled": True}
                )
                if response.status_code == 404:
                    response = await client.get(
                        "/api/v1/settings/jellyfin/library",
                        params={"enable": ",".join(sorted(enabled_ids))},
                    )
                    response.raise_for_status()
                    break
                response.raise_for_status()
            except httpx.TransportError:
                pass
        observed = await client.get(endpoint)
        observed.raise_for_status()
        observed_libraries = observed.json().get("libraries", [])
        if not isinstance(observed_libraries, list) or not enabled_ids <= {
            str(item["id"]) for item in observed_libraries if item.get("enabled") is True
        }:
            raise NativeConfigurationError("Seerr library read-back drift")
    if mode == "apply":
        public = await client.get("/api/v1/settings/public")
        public.raise_for_status()
        if public.json().get("initialized") is False:
            response = await client.post("/api/v1/settings/initialize")
            response.raise_for_status()
            observed = await client.get("/api/v1/settings/public")
            observed.raise_for_status()
            if observed.json().get("initialized") is not True:
                raise NativeConfigurationError("Seerr initialization read-back drift")
    return ServiceOutcome(
        "seerr",
        "drift"
        if changes and mode == "verify"
        else "planned"
        if changes and mode == "plan"
        else "verified",
        changes,
    )
