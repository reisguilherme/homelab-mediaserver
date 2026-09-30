from copy import deepcopy
from math import isfinite
from urllib.parse import urlsplit

import httpx

from .credential_state import CredentialState
from .native_config import NativeConfigurationError, ServiceOutcome, environment, plan_settings


def configured_resolutions(service, env):
    field = (
        "HOMESERVER_MOVIE_RESOLUTIONS" if service == "radarr" else "HOMESERVER_SERIES_RESOLUTIONS"
    )
    legacy = env.get("HOMESERVER_MEDIA_RESOLUTIONS", "2160,1080" if service == "radarr" else "1080")
    return set(map(int, env.get(field, legacy).split(",")))


async def reconcile_indexer_filters(service, settings, client, mode):
    """Keep native search restricted to the same named sources as worker selection."""
    env = environment(settings)
    if "HOMESERVER_RELEASE_INDEXER_PRIORITY" not in env:
        return []
    from homeserver_control.worker.release_quality import release_indexer

    allowed = set(env["HOMESERVER_RELEASE_INDEXER_PRIORITY"].split(","))
    endpoint = "/api/v1/indexer" if service == "prowlarr" else "/api/v3/indexer"
    response = await client.get(endpoint)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or type(row.get("id")) is not int
        or not isinstance(row.get("name"), str)
        for row in rows
    ):
        raise NativeConfigurationError("Native indexer schema unsupported")
    changes = []
    for current in rows:
        enabled = release_indexer({"indexer": current["name"]}) in allowed
        desired = (
            {"enable": enabled}
            if service == "prowlarr"
            else {
                "enableRss": False,
                "enableAutomaticSearch": False,
                "enableInteractiveSearch": enabled,
            }
        )
        changes += await collection(
            service,
            client,
            endpoint,
            {"id": current["id"], "name": current["name"], **desired},
            "name",
            mode,
            settings=settings,
        )
    return changes


def native_quality_source(quality):
    """Normalize native names without treating WEBRip as WEB-DL."""
    source = str(quality.get("source", "")).lower()
    name = "".join(
        character for character in str(quality.get("name", "")).lower() if character.isalnum()
    )
    if "webrip" in name:
        return "webrip"
    if "remux" in name or source == "blurayraw":
        return "remux"
    if "webdl" in name:
        return "webdl"
    return source


async def reconcile_quality_sizes(service, env, client, mode):
    """Configure only allowed native definitions, in MiB per runtime minute."""
    minimums = {
        resolution: float(env[f"HOMESERVER_QUALITY_MIN_MIB_PER_MIN_{resolution}"])
        for resolution in (720, 1080, 2160)
        if f"HOMESERVER_QUALITY_MIN_MIB_PER_MIN_{resolution}" in env
    }
    if not minimums:
        return []
    resolutions = configured_resolutions(service, env)
    sources = set(env.get("HOMESERVER_MEDIA_SOURCES", "remux,bluray,webdl").split(","))
    endpoint = "/api/v3/qualitydefinition"
    response = await client.get(endpoint)
    if response.status_code in (404, 405, 501):
        return None
    response.raise_for_status()
    try:
        rows = response.json()
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    identifiers = set()
    pending = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("quality"), dict)
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or row["id"] in identifiers
        ):
            return None
        identifiers.add(row["id"])
        quality = row["quality"]
        resolution = quality.get("resolution")
        if (
            resolution not in resolutions
            or resolution not in minimums
            or native_quality_source(quality) not in sources
        ):
            continue
        if "minSize" not in row:
            return None
        for field in ("minSize", "maxSize", "preferredSize"):
            value = row.get(field)  # Native JSON omits nullable maximum/preferred sizes.
            if value is not None and (
                type(value) not in (int, float) or not isfinite(value) or value < 0
            ):
                return None
        desired = {"minSize": minimums[resolution], "maxSize": None}
        if row.get("preferredSize") is not None and row["preferredSize"] < desired["minSize"]:
            desired["preferredSize"] = desired["minSize"]
        pending.append((row, desired, plan_settings(service, row, desired)))
    if not pending:
        return None
    # Validate the complete schema before writing any quality definition.
    changes = []
    for current, desired, planned in pending:
        changes.extend(planned)
        if planned and mode == "apply":
            try:
                response = await client.put(endpoint + f"/{current['id']}", json=current | desired)
                response.raise_for_status()
            except httpx.TransportError:
                pass
            response = await client.get(endpoint)
            response.raise_for_status()
            observed = response.json()
            matches = (
                [
                    row
                    for row in observed
                    if isinstance(row, dict) and row.get("id") == current["id"]
                ]
                if isinstance(observed, list)
                else []
            )
            if len(matches) != 1 or plan_settings(service, matches[0], desired):
                raise NativeConfigurationError("Native quality size read-back drift")
    return changes


async def reconcile_import_guards(service, env, client, mode):
    """Keep import ownership in the gateway and preserve seeding through hardlinks."""
    if not any(
        f"HOMESERVER_QUALITY_MIN_MIB_PER_MIN_{resolution}" in env
        for resolution in (720, 1080, 2160)
    ):
        return []
    resources = (
        ("downloadclient", {"enableCompletedDownloadHandling": False}),
        (
            "mediamanagement",
            {"copyUsingHardlinks": True}
            | ({"skipFreeSpaceCheckWhenImporting": True} if service == "radarr" else {}),
        ),
    )
    pending = []
    for resource, desired in resources:
        endpoint = f"/api/v3/config/{resource}"
        response = await client.get(endpoint)
        if response.status_code in (404, 405, 501):
            return None
        response.raise_for_status()
        try:
            current = response.json()
        except ValueError:
            return None
        if (
            not isinstance(current, dict)
            or type(current.get("id")) is not int
            or current["id"] <= 0
            or any(type(current.get(key)) is not bool for key in desired)
        ):
            return None
        pending.append((endpoint, current, desired, plan_settings(service, current, desired)))
    # Validate both native resources before changing either import guard.
    changes = []
    for endpoint, current, desired, planned in pending:
        changes.extend(planned)
        if planned and mode == "apply":
            try:
                response = await client.put(endpoint + f"/{current['id']}", json=current | desired)
                response.raise_for_status()
            except httpx.TransportError:
                pass
            response = await client.get(endpoint)
            response.raise_for_status()
            observed = response.json()
            if (
                not isinstance(observed, dict)
                or type(observed.get("id")) is not int
                or observed["id"] != current["id"]
                or any(observed.get(key) is not value for key, value in desired.items())
            ):
                raise NativeConfigurationError("Native import guard read-back drift")
    return changes


async def bootstrap_arr_account(service, settings, client, mode):
    """Initialize only an absent UI account; never reset an existing account."""
    env = environment(settings)
    if not env.get("HOMESERVER_ADMIN_PASSWORD"):
        return []
    endpoint = f"/api/{'v1' if service == 'prowlarr' else 'v3'}/config/host"
    response = await client.get(endpoint)
    response.raise_for_status()
    current = response.json()
    if not isinstance(current, dict) or "username" not in current or "password" not in current:
        raise NativeConfigurationError("Native UI account capability unavailable")
    if current["username"]:
        return []
    desired = {
        "username": env.get("HOMESERVER_ADMIN_USERNAME", "admin"),
        "password": env["HOMESERVER_ADMIN_PASSWORD"],
        "passwordConfirmation": env["HOMESERVER_ADMIN_PASSWORD"],
        "authenticationMethod": "forms",
        "authenticationRequired": "enabled",
    }
    changes = plan_settings(service, current, desired)
    if mode == "apply":
        try:
            response = await client.put(
                endpoint + f"/{current.get('id', 1)}", json=current | desired
            )
            response.raise_for_status()
        except httpx.TransportError:
            pass
        response = await client.get(endpoint)
        response.raise_for_status()
        observed = response.json()
        if (
            observed.get("username") != desired["username"]
            or not observed.get("password")
            or observed.get("authenticationMethod") != "forms"
            or observed.get("authenticationRequired") != "enabled"
        ):
            raise NativeConfigurationError("Native UI account read-back drift")
    return changes


async def collection(service, client, endpoint, desired, identity, mode, *, settings=None):
    """Upsert by stable identity; never remove resources or overwrite foreign fields."""
    response = await client.get(endpoint)
    response.raise_for_status()
    rows = response.json()
    matches = (
        [row for row in rows if row.get("id") == desired["id"]]
        if desired.get("id")
        else [row for row in rows if row.get(identity) == desired[identity]]
    )
    if desired.get("id") and not matches:
        raise NativeConfigurationError("Adopted native ID missing; refusing replacement")
    if len(matches) > 1:
        raise NativeConfigurationError("ambiguous native resource identity")
    current = matches[0] if matches else {}
    comparison = deepcopy(desired)
    comparison.pop("presets", None)  # Schema-only choices, absent from stored native resource.
    if "fields" in comparison:
        comparison["fields"] = [
            {"name": field["name"], "value": field.get("value")} for field in comparison["fields"]
        ]

    ledger = CredentialState(settings)

    def masked_values(row):
        return {
            f"{service}:{endpoint}:{row.get('id')}:{field['name']}": field.get("value")
            for field in comparison.get("fields", [])
            if any(
                actual.get("name") == field["name"] and actual.get("value") == "********"
                for actual in row.get("fields", [])
            )
        }

    async def managed_values(row, *, acknowledged=False):
        row = deepcopy(row)
        if "fields" in comparison:
            fields = {field["name"]: field.get("value") for field in row.get("fields", [])}
            masked = masked_values(row)
            proven = acknowledged or (
                bool(masked) and all(ledger.matches(key, value) for key, value in masked.items())
            )
            if masked and proven:
                response = await client.post(endpoint + "/test", json=row)
                response.raise_for_status()
                for field in comparison["fields"]:
                    if fields.get(field["name"]) == "********":
                        fields[field["name"]] = field.get("value")
            row["fields"] = [
                {"name": field["name"], "value": fields.get(field["name"])}
                for field in comparison["fields"]
            ]
        return row

    changes = plan_settings(service, await managed_values(current), comparison)
    if changes and mode == "apply":
        payload = current | desired
        if service == "seerr" and endpoint in (
            "/api/v1/settings/radarr",
            "/api/v1/settings/sonarr",
        ):
            # Seerr identifies existing instances through the route; its schema
            # rejects the read-only id in a request body.
            payload.pop("id", None)
        if "fields" in desired and current:
            values = {field["name"]: field for field in desired["fields"]}
            payload["fields"] = [
                field | values.pop(field["name"], {}) for field in current.get("fields", [])
            ]
            payload["fields"].extend(values.values())
        acknowledged = True
        try:
            response = await client.request(
                "PUT" if current else "POST",
                endpoint + (f"/{current['id']}" if current else ""),
                json=payload,
            )
            response.raise_for_status()
        except httpx.TransportError:
            acknowledged = False
        observed = await client.get(endpoint)
        observed.raise_for_status()
        matches = [row for row in observed.json() if row.get(identity) == desired[identity]]
        if len(matches) != 1 or plan_settings(
            service, await managed_values(matches[0], acknowledged=acknowledged), comparison
        ):
            raise NativeConfigurationError("native resource read-back drift")
        if masked_values(matches[0]):
            ledger.record(masked_values(matches[0]))
    return changes


async def reconcile_arr(service, settings, client, mode):
    env = environment(settings)
    root = env.get(
        "HOMESERVER_MOVIE_ROOT" if service == "radarr" else "HOMESERVER_SERIES_ROOT",
        "/data/media/movies" if service == "radarr" else "/data/media/tv",
    )
    changes = await reconcile_quality_sizes(service, env, client, mode)
    if changes is None:
        return ServiceOutcome(
            service, "unsupported", message="Native quality size schema unsupported"
        )
    guard_changes = await reconcile_import_guards(service, env, client, mode)
    if guard_changes is None:
        return ServiceOutcome(
            service, "unsupported", changes, "Native import guard schema unsupported"
        )
    changes += guard_changes
    changes += await reconcile_indexer_filters(service, settings, client, mode)
    if root:
        changes += await collection(
            service, client, "/api/v3/rootfolder", {"path": root}, "path", mode
        )
    if any(
        key in env
        for key in (
            "HOMESERVER_MEDIA_RESOLUTIONS",
            "HOMESERVER_MOVIE_RESOLUTIONS",
            "HOMESERVER_SERIES_RESOLUTIONS",
        )
    ):
        response = await client.get("/api/v3/qualityprofile/schema")
        response.raise_for_status()
        template = response.json()
        if not isinstance(template, dict) or not isinstance(template.get("items"), list):
            return ServiceOutcome(
                service, "unsupported", changes, "Native quality profile schema unsupported"
            )
        profile = deepcopy(template)
        profile.pop("id", None)
        profile["name"] = "HomeServer"
        resolutions = configured_resolutions(service, env)
        sources = set(env.get("HOMESERVER_MEDIA_SOURCES", "remux,bluray,webdl").split(","))

        def configure(items):
            for item in items:
                if item.get("items"):
                    configure(item["items"])
                    item["allowed"] = any(child["allowed"] for child in item["items"])
                else:
                    quality = item.get("quality", {})
                    source = native_quality_source(quality)
                    item["allowed"] = quality.get("resolution") in resolutions and source in sources

        configure(profile["items"])
        allowed = [
            item.get("quality", {}).get("id", item.get("id"))
            for item in profile["items"]
            if item["allowed"]
        ]
        if not allowed:
            return ServiceOutcome(
                service, "unsupported", changes, "No native qualities match configured policy"
            )
        profile["cutoff"] = allowed[-1]
        profile["upgradeAllowed"] = env.get("HOMESERVER_AUTOMATIC_UPGRADES", "false") == "true"
        changes += await collection(
            service, client, "/api/v3/qualityprofile", profile, "name", mode, settings=settings
        )
        response = await client.get("/api/v3/downloadclient/schema")
        response.raise_for_status()
        templates = [
            item for item in response.json() if item.get("implementation") == "QBittorrent"
        ]
        if len(templates) != 1:
            return ServiceOutcome(
                service, "unsupported", changes, "Native qBittorrent client schema unavailable"
            )
        download = deepcopy(templates[0])
        download.pop("id", None)
        download.update(
            name="HomeServer Gateway",
            enable=True,
            priority=1,
            removeCompletedDownloads=False,
            removeFailedDownloads=False,
        )
        gateway = urlsplit(env.get("HOMESERVER_QBIT_GATEWAY_URL", "http://download-gateway:8081"))
        category = "movieCategory" if service == "radarr" else "tvCategory"
        values = {
            "host": gateway.hostname,
            "port": gateway.port or (443 if gateway.scheme == "https" else 80),
            "useSsl": gateway.scheme == "https",
            "urlBase": gateway.path,
            "username": "arr",
            "password": env.get("HOMESERVER_ARR_TOKEN", ""),
            category: "radarr" if service == "radarr" else "sonarr",
        }
        names = {item["name"] for item in download["fields"]}
        if not set(values) <= names:
            return ServiceOutcome(
                service, "unsupported", changes, "Native gateway fields unavailable"
            )
        for field in download["fields"]:
            if field["name"] in values:
                field["value"] = values[field["name"]]
        changes += await collection(
            service, client, "/api/v3/downloadclient", download, "name", mode, settings=settings
        )
    return ServiceOutcome(
        service,
        "drift"
        if changes and mode == "verify"
        else "planned"
        if changes and mode == "plan"
        else "verified",
        changes,
    )
