"""Named profiles and resumable, read-back verified native catalog migration."""

from copy import deepcopy

import httpx

from homeserver_common.download_profiles import (
    DEFAULT_PROFILE_NAME,
    PROFILE_NAMES,
    PROFILE_QUALITIES,
)

from .native_config import NativeConfigurationError, environment, plan_settings

QUALITY_FORMAT_NAMES = (
    "HomeServer BluRay", "HomeServer Original", "HomeServer Dual PTBR Original",
    "HomeServer ASC Dual Original",
)


async def quality_formats(client):
    """Build specifications only from supported native schema and language IDs."""
    from .dubbed_language import _rows

    schema = await _rows(client, "/api/v3/customformat/schema")
    languages = await _rows(client, "/api/v3/language")
    ids = {}
    for name in ("Original", "Portuguese (Brazil)"):
        matches = [row for row in languages if row.get("name") == name]
        if len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise NativeConfigurationError("Native original/Brazilian language unavailable")
        ids[name] = matches[0]["id"]
    if ids["Original"] != -2 or ids["Portuguese (Brazil)"] <= 0:
        raise NativeConfigurationError("Native original/Brazilian language identity unsupported")

    def specification(implementation, name, value, *, negate=False):
        matches = [row for row in schema if row.get("implementation") == implementation]
        if len(matches) != 1:
            raise NativeConfigurationError("Native quality format specification unavailable")
        template = matches[0]
        fields = template.get("fields")
        if not isinstance(fields, list) or not any(
            isinstance(field, dict) and field.get("name") == "value" for field in fields
        ):
            raise NativeConfigurationError("Native quality format fields unsupported")
        values = {"value": value}
        if implementation == "LanguageSpecification":
            if not any(field.get("name") == "exceptLanguage" for field in fields):
                raise NativeConfigurationError("Native language format fields unsupported")
            values["exceptLanguage"] = False
        return {
            "name": name, "implementation": implementation,
            "required": True, "negate": negate,
            "fields": [{"name": key, "value": value} for key, value in values.items()],
        }

    original = specification("LanguageSpecification", "Original", ids["Original"])
    brazilian = specification(
        "LanguageSpecification", "Brazilian", ids["Portuguese (Brazil)"]
    )
    bluray = specification(
        "ReleaseTitleSpecification", "BluRay", r"(?i)\b(?:blu[ ._-]?ray|bdmv)\b",
    )
    rip = specification(
        "ReleaseTitleSpecification", "Exclude disc rips", r"(?i)\b(?:bd|br)[ ._-]?rip\b",
        negate=True,
    )
    # ASC declares the original soundtrack in its title while native parsing
    # reports only Brazilian Portuguese. Bridge that bounded declaration without
    # adding its score when the normal original/dual language conditions match.
    asc_dual = specification(
        "ReleaseTitleSpecification", "ASC declared dual original",
        r"(?i)(?<![a-z0-9])dual[ ._-]*audio[ ._-]+brazilian[ ._-]+original(?![a-z0-9])",
    )
    original_missing = specification(
        "LanguageSpecification", "Original missing from native parser", ids["Original"],
        negate=True,
    )
    return [
        {"name": name, "includeCustomFormatWhenRenaming": False, "specifications": specs}
        for name, specs in zip(QUALITY_FORMAT_NAMES,
                               ([bluray, rip], [original], [original, brazilian],
                                [asc_dual, original_missing]),
                               strict=True)
    ]


async def reconcile_quality_formats(service, client, desired, mode):
    from .dubbed_language import _managed, _rows, _semantic_format

    current = await _rows(client, "/api/v3/customformat")
    for definition in desired:
        _managed(current, definition["name"])
    changes = []
    for definition in desired:
        actual = _managed(current, definition["name"])
        changed = _semantic_format(actual) != _semantic_format(definition)
        if changed:
            changes += plan_settings(service, {"qualityFormat": _semantic_format(actual)},
                                     {"qualityFormat": _semantic_format(definition)})
        if changed and mode == "apply":
            if actual is None:
                response = await client.post("/api/v3/customformat", json=definition)
            else:
                response = await client.put(f"/api/v3/customformat/{actual['id']}",
                                            json=actual | definition)
            response.raise_for_status()
            current = await _rows(client, "/api/v3/customformat")
            actual = _managed(current, definition["name"])
            if actual is None or _semantic_format(actual) != _semantic_format(definition):
                raise NativeConfigurationError("Native quality format read-back drift")
    return changes, current


def quality_format_scores(profile, formats):
    """Keep unrelated scores; the worker remains the final explicit audio gate."""
    scores = (100, 10, 990, 1000) if profile["name"] == "Dublado" else (100, 20, -10, 10)
    desired = dict(zip(QUALITY_FORMAT_NAMES, scores, strict=True))
    desired["HomeServer Dublado Search"] = 0
    items = deepcopy(profile.get("formatItems", []))
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise NativeConfigurationError("Native quality format scores unsupported")
    for row in formats:
        if row.get("name") not in desired:
            continue
        matches = [item for item in items if item.get("format") == row.get("id")]
        if len(matches) > 1:
            raise NativeConfigurationError("Native quality format scores ambiguous")
        if matches:
            matches[0]["score"] = desired[row["name"]]
        else:
            items.append({"format": row["id"], "name": row["name"],
                          "score": desired[row["name"]]})
    profile["formatItems"] = items
    profile["minFormatScore"] = max(10, profile.get("minFormatScore", 0))


def profile_readback_matches(service, observed, expected):
    """Compare preserved fields, excluding Radarr's recalculated cutoff flag."""
    if not isinstance(observed, dict):
        return False
    observed, expected = dict(observed), dict(expected)
    if service == "radarr":
        for payload in (observed, expected):
            movie_file = payload.get("movieFile")
            if isinstance(movie_file, dict):
                movie_file = dict(movie_file)
                if (
                    "qualityCutoffNotMet" in movie_file
                    and type(movie_file["qualityCutoffNotMet"]) is not bool
                ):
                    return False
                movie_file.pop("qualityCutoffNotMet", None)
                payload["movieFile"] = movie_file
    return all(observed.get(key) == value for key, value in expected.items())


def default_profile_name(env):
    name = env.get("HOMESERVER_DOWNLOAD_DEFAULT_PROFILE", DEFAULT_PROFILE_NAME)
    if name not in PROFILE_NAMES:
        raise NativeConfigurationError("Unknown configured download profile")
    return name


def validate_profiles(rows):
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or type(row.get("id")) is not int
        or row["id"] <= 0
        or not isinstance(row.get("name"), str)
        for row in rows
    ):
        raise NativeConfigurationError("Native quality profile schema unsupported")
    if len({row["id"] for row in rows}) != len(rows) or len({row["name"] for row in rows}) != len(
        rows
    ):
        raise NativeConfigurationError("Ambiguous native quality profiles")
    return rows


async def read_profiles(client):
    response = await client.get("/api/v3/qualityprofile")
    response.raise_for_status()
    return validate_profiles(response.json())


async def read_catalog(service, client, profiles):
    endpoint = "/api/v3/movie" if service == "radarr" else "/api/v3/series"
    response = await client.get(endpoint)
    response.raise_for_status()
    rows = response.json()
    identifiers = {row["id"] for row in profiles}
    if (
        not isinstance(rows, list)
        or any(
            not isinstance(row, dict)
            or type(row.get("id")) is not int
            or row["id"] <= 0
            or type(row.get("qualityProfileId")) is not int
            or row["qualityProfileId"] not in identifiers
            for row in rows
        )
        or len({row["id"] for row in rows}) != len(rows)
    ):
        raise NativeConfigurationError("Native catalog profile references unsupported")
    return endpoint, rows


async def read_radarr_profile_references(client, profiles):
    """Enumerate the additional references checked by Radarr before deletion."""
    identifiers = {row["id"] for row in profiles}
    references = {}
    for resource in ("collection", "importlist"):
        response = await client.get(f"/api/v3/{resource}")
        response.raise_for_status()
        rows = response.json()
        if (
            not isinstance(rows, list)
            or any(
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or row["id"] <= 0
                or type(row.get("qualityProfileId")) is not int
                or row["qualityProfileId"] not in identifiers
                for row in rows
            )
            or len({row["id"] for row in rows}) != len(rows)
        ):
            raise NativeConfigurationError("Native collection/importlist profiles unsupported")
        references[resource] = rows
    return references


def quality_leaves(template):
    from .arr import native_quality_source

    if not isinstance(template, dict):
        raise NativeConfigurationError("Native quality profile schema unsupported")
    leaves = []

    def visit(items):
        if not isinstance(items, list) or not items:
            raise NativeConfigurationError("Native quality items unsupported")
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("allowed"), bool):
                raise NativeConfigurationError("Native quality item unsupported")
            children = item.get("items")
            if children:
                if item.get("quality") is not None:
                    raise NativeConfigurationError("Ambiguous native quality group")
                visit(children)
                continue
            quality = item.get("quality")
            if (
                children not in (None, [])
                or not isinstance(quality, dict)
                or type(quality.get("id")) is not int
                or quality["id"] < 0
                or type(quality.get("resolution")) is not int
                or not isinstance(quality.get("name"), str)
            ):
                raise NativeConfigurationError("Native quality leaf unsupported")
            leaves.append(deepcopy(item))

    visit(template.get("items"))
    if len({item["quality"]["id"] for item in leaves}) != len(leaves):
        raise NativeConfigurationError("Duplicate native quality identifiers")
    wanted = {}
    for item in leaves:
        quality = item["quality"]
        category = (quality["resolution"], native_quality_source(quality))
        if category in PROFILE_QUALITIES["Max Quality"]:
            if category in wanted:
                raise NativeConfigurationError("Ambiguous native quality category")
            wanted[category] = item
    if set(wanted) != set(PROFILE_QUALITIES["Max Quality"]):
        raise NativeConfigurationError("Required native qualities unavailable")
    return leaves, wanted


async def desired_profiles(service, client, current, formats=()):
    response = await client.get("/api/v3/qualityprofile/schema")
    response.raise_for_status()
    template = response.json()
    leaves, wanted = quality_leaves(template)
    languages = {}
    if service == "radarr":
        response = await client.get("/api/v3/language")
        response.raise_for_status()
        rows = response.json()
        if (
            not isinstance(rows, list)
            or any(
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or not isinstance(row.get("name"), str)
                for row in rows
            )
            or len({row["id"] for row in rows}) != len(rows)
        ):
            raise NativeConfigurationError("Native languages unsupported")
        for identifier, name in ((-2, "Original"), (-1, "Any")):
            if (
                sum(
                    row["id"] == identifier and row["name"].casefold() == name.casefold()
                    for row in rows
                )
                != 1
            ):
                raise NativeConfigurationError("Native original/any language unavailable")
            languages[name] = {"id": identifier, "name": name}
    by_name = {row["name"]: row for row in current}
    desired = []
    for name in PROFILE_NAMES:
        profile = deepcopy(by_name.get(name, template))
        profile.pop("id", None)
        allowed = [deepcopy(wanted[category]) for category in reversed(PROFILE_QUALITIES[name])]
        allowed_ids = {item["quality"]["id"] for item in allowed}
        # Native Bluray includes BDRip/BRRip: tie it with WEB-DL, then use the
        # managed BluRay title score to promote non-rip releases within the group.
        disabled = [
            item | {"allowed": False} for item in leaves if item["quality"]["id"] not in allowed_ids
        ]
        ordered = []
        group_id = max([1000, *(item["quality"]["id"] for item in leaves)]) + 1
        for resolution in sorted({item["quality"]["resolution"] for item in allowed}):
            grouped = [item | {"allowed": True} for item in allowed
                       if item["quality"]["resolution"] == resolution
                       and item["quality"]["id"] in {
                           wanted[(resolution, "bluray")]["quality"]["id"],
                           wanted[(resolution, "webdl")]["quality"]["id"],
                       }]
            ordered.append({"id": group_id, "name": f"HomeServer {resolution}p",
                            "allowed": True, "items": grouped})
            group_id += 1
            ordered.extend(item | {"allowed": True} for item in allowed
                           if item["quality"]["resolution"] == resolution
                           and item not in [wanted[(resolution, "bluray")],
                                            wanted[(resolution, "webdl")]])
        profile.update(
            name=name,
            items=disabled + ordered,
            cutoff=ordered[-1].get("id") or ordered[-1]["quality"]["id"],
            upgradeAllowed=False,
        )
        quality_format_scores(profile, formats)
        if service == "radarr":
            # Native ASC parsing omits its declared original audio; the worker
            # validates trusted title/audio evidence before requesting a grab.
            profile["language"] = languages["Any"]
        else:
            profile.pop("language", None)
        desired.append(profile)
    return desired


async def reconcile_named_quality_profiles(service, settings, client, mode):
    from .arr import collection

    env = environment(settings)
    default = default_profile_name(env)
    # Complete profile, language and catalog validation precedes profile writes.
    try:
        profiles = await read_profiles(client)
        desired = await desired_profiles(service, client, profiles)
        format_definitions = await quality_formats(client)
        endpoint, catalog = await read_catalog(service, client, profiles)
        resources = [(endpoint, row) for row in catalog]
        if service == "radarr":
            references = await read_radarr_profile_references(client, profiles)
            obsolete_ids = {row["id"] for row in profiles if row["name"] not in PROFILE_NAMES}
            if any(row["qualityProfileId"] in obsolete_ids for row in references["importlist"]):
                # Provider updates can validate credentials and trigger list side
                # effects. Preserve them for an explicitly verified migration.
                raise NativeConfigurationError("Native importlist profiles require safe migration")
            resources += [("/api/v3/collection", row) for row in references["collection"]]
    except (NativeConfigurationError, ValueError):
        return None
    changes = []
    format_changes, formats = await reconcile_quality_formats(
        service, client, format_definitions, mode,
    )
    changes += format_changes
    if mode == "apply":
        # CF creation adds zero-score entries; preserve the complete new native state.
        profiles = await read_profiles(client)
    desired = await desired_profiles(service, client, profiles, formats)
    for profile in desired:
        changes += await collection(
            service, client, "/api/v3/qualityprofile", profile, "name", mode, settings=settings
        )
    if mode == "apply":
        profiles = await read_profiles(client)
    names = {row["name"]: row for row in profiles}
    obsolete = {row["id"]: row for row in profiles if row["name"] not in PROFILE_NAMES}
    for endpoint, row in resources:
        previous = obsolete.get(row["qualityProfileId"])
        if previous is None:
            continue
        target_name = "Dublado" if previous["name"] == "HomeServer Animation" else default
        changes += plan_settings(
            service,
            {f"{endpoint}.{row['id']}.profile": previous["name"]},
            {f"{endpoint}.{row['id']}.profile": target_name},
        )
        if mode != "apply":
            continue
        target_id = names[target_name]["id"]
        resource = endpoint + f"/{row['id']}"
        response = await client.get(resource)
        response.raise_for_status()
        actual = response.json()
        if (
            not isinstance(actual, dict)
            or actual.get("id") != row["id"]
            or actual.get("qualityProfileId") not in (row["qualityProfileId"], target_id)
        ):
            raise NativeConfigurationError("Native catalog changed during profile migration")
        payload = actual | {"qualityProfileId": target_id}
        try:
            response = await client.put(resource, json=payload)
            response.raise_for_status()
        except httpx.TransportError:
            pass
        response = await client.get(resource)
        response.raise_for_status()
        observed = response.json()
        if not profile_readback_matches(service, observed, payload):
            raise NativeConfigurationError("Native catalog profile read-back drift")
    if not (env.get("HOMESERVER_SEERR_URL") or env.get("HOMESERVER_SEERR_API_KEY")):
        changes += await prune_native_quality_profiles(service, client, mode)
    elif mode != "apply":
        changes += plan_settings(
            service, {"obsoleteProfiles": sorted(obsolete)}, {"obsoleteProfiles": []}
        )
    return changes


async def prune_native_quality_profiles(service, client, mode):
    """Call only after all external Seerr references have been read-back verified."""
    profiles = await read_profiles(client)
    obsolete = [row for row in profiles if row["name"] not in PROFILE_NAMES]
    if not obsolete:
        return []
    changes = plan_settings(
        service, {"obsoleteProfiles": [row["id"] for row in obsolete]}, {"obsoleteProfiles": []}
    )
    if mode != "apply":
        return changes
    if not set(PROFILE_NAMES) <= {row["name"] for row in profiles}:
        raise NativeConfigurationError("Replacement profiles unavailable for migration")
    obsolete_ids = {row["id"] for row in obsolete}
    for profile in obsolete:
        _, catalog = await read_catalog(service, client, profiles)
        if service == "radarr":
            references = await read_radarr_profile_references(client, profiles)
            catalog += references["collection"] + references["importlist"]
        if any(row["qualityProfileId"] in obsolete_ids for row in catalog):
            raise NativeConfigurationError("Obsolete quality profile remains in use")
        try:
            response = await client.delete(f"/api/v3/qualityprofile/{profile['id']}")
            response.raise_for_status()
        except httpx.TransportError:
            pass
        profiles = await read_profiles(client)
        if any(row["id"] == profile["id"] for row in profiles):
            raise NativeConfigurationError("Native profile removal read-back drift")
    return changes


async def verify_seerr_stored_references(service, client, instances, profiles):
    """Request PUT fires Jellyseerr hooks; leave stored overrides for offline migration."""
    obsolete = {row["id"] for row in profiles if row["name"] not in PROFILE_NAMES}
    if not obsolete:
        return
    instance_ids = {row["id"] for row in instances}
    linked_rules = set()
    for row in instances:
        rules = row.get("overrideRule", [])
        if not isinstance(rules, list) or any(type(identifier) is not int for identifier in rules):
            raise NativeConfigurationError("Seerr override rule references unsupported")
        linked_rules.update(rules)
    response = await client.get("/api/v1/overrideRule")
    response.raise_for_status()
    rules = response.json()
    if not isinstance(rules, list) or any(
        not isinstance(row, dict) or type(row.get("id")) is not int for row in rules
    ):
        raise NativeConfigurationError("Seerr stored profile rule schema unsupported")
    for row in rules:
        if row.get("profileId") in obsolete and (
            row.get(f"{service}ServiceId") in instance_ids or row["id"] in linked_rules
        ):
            raise NativeConfigurationError("Seerr stored profile rules require safe migration")
    skip = 0
    seen = set()
    expected_total = None
    media_type = "movie" if service == "radarr" else "tv"
    while True:
        response = await client.get(
            "/api/v1/request",
            params={
                "take": 100,
                "skip": skip,
                "sortDirection": "asc",
                "mediaType": media_type,
            },
        )
        response.raise_for_status()
        page = response.json()
        if not isinstance(page, dict) or not isinstance(page.get("pageInfo"), dict):
            raise NativeConfigurationError("Seerr stored profile request schema unsupported")
        total = page["pageInfo"].get("results")
        rows = page.get("results")
        if (
            type(total) is not int
            or total < 0
            or not isinstance(rows, list)
            or any(
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or row.get("type") not in ("movie", "tv")
                for row in rows
            )
        ):
            raise NativeConfigurationError("Seerr stored profile request schema unsupported")
        if expected_total is not None and total != expected_total:
            raise NativeConfigurationError("Seerr requests changed during profile migration")
        expected_total = total
        for row in rows:
            if row["id"] in seen:
                raise NativeConfigurationError("Ambiguous Seerr stored profile requests")
            seen.add(row["id"])
            if (
                row["type"] == media_type
                and row.get("profileId") in obsolete
                and (row.get("serverId") is None or row.get("serverId") in instance_ids)
            ):
                raise NativeConfigurationError(
                    "Seerr stored profile requests require safe migration"
                )
        skip += len(rows)
        if skip == total:
            return
        if not rows or skip > total:
            raise NativeConfigurationError("Incomplete Seerr stored profile request pagination")


async def reconcile_seerr_profile_references(service, env, client, arr, desired, mode):
    """Migrate all instances of this Arr before deleting their old profile IDs."""
    from .arr import collection

    profiles = await read_profiles(arr)
    names = {row["name"]: row for row in profiles}
    default = default_profile_name(env)
    if not set(PROFILE_NAMES) <= names.keys():
        if mode in ("plan", "verify"):
            return plan_settings("seerr", {}, {f"{service}.profile": default})
        raise NativeConfigurationError("Seerr requires verified named native profiles")
    ids = {row["id"]: row for row in profiles}
    endpoint = f"/api/v1/settings/{service}"
    native = arr.base_url
    destinations = (
        (desired["hostname"], desired["port"], desired.get("useSsl", False),
         desired["baseUrl"].rstrip("/")),
        (native.host, native.port or (443 if native.scheme == "https" else 80),
         native.scheme == "https", native.path.rstrip("/")),
    )

    def relevant(row):
        return row["name"] == desired["name"] or (
            row.get("hostname"), row.get("port"), row.get("useSsl", False),
            row.get("baseUrl", "").rstrip("/"),
        ) in destinations

    fields = {
        "activeProfileId": "activeProfileName",
        "activeAnimeProfileId": "activeAnimeProfileName",
        "activeProfileId4k": "activeProfileName4k",
    }

    async def read_instances():
        response = await client.get(endpoint)
        response.raise_for_status()
        rows = response.json()
        if (
            not isinstance(rows, list)
            or any(
                not isinstance(row, dict)
                or type(row.get("id")) is not int
                or not isinstance(row.get("name"), str)
                for row in rows
            )
            or len({row["id"] for row in rows}) != len(rows)
        ):
            raise NativeConfigurationError("Seerr native instances unsupported")
        for row in rows:
            if not relevant(row):
                # A host alias sharing this API key might be this same server.
                # Do not remove definitions before its identity is established.
                if row.get("apiKey") == desired["apiKey"]:
                    raise NativeConfigurationError("Ambiguous Seerr Arr instance address")
                continue
            for key, value in row.items():
                if "profileid" in key.lower() and "language" not in key.lower():
                    if key not in fields or (
                        value not in (None, 0) and (type(value) is not int or value not in ids)
                    ):
                        raise NativeConfigurationError("Unknown Seerr native profile reference")
        return rows

    rows = await read_instances()
    managed = [row for row in rows if row["name"] == desired["name"]]
    if not managed:
        managed = [row for row in rows if relevant(row) and row.get("is4k") is not True]
    if len(managed) > 1:
        raise NativeConfigurationError("Ambiguous Seerr native default instance")
    if managed:
        desired["id"] = managed[0]["id"]
    desired.update(activeProfileId=names[default]["id"], activeProfileName=default)
    if service == "sonarr" and managed and managed[0].get("activeAnimeProfileId"):
        # New anime requests use the same explicit default as any other series.
        desired.update(activeAnimeProfileId=names[default]["id"], activeAnimeProfileName=default)
    pending = []
    for row in rows:
        if not relevant(row):
            continue
        update = {"id": row["id"], "name": row["name"]} | {
            key: desired[key] for key in ("hostname", "port", "useSsl", "baseUrl")
        }
        for field, name_field in fields.items():
            previous = ids.get(row.get(field))
            if previous and previous["name"] not in PROFILE_NAMES:
                target = "Dublado" if previous["name"] == "HomeServer Animation" else default
                update.update({field: names[target]["id"], name_field: target})
        if managed and row["id"] == managed[0]["id"]:
            # Defaults deliberately win over obsolete instance selections.
            desired = update | desired
        elif len(update) > 2:
            pending.append(update)
    changes = []
    for update in pending + [desired]:
        changes += await collection("seerr", client, endpoint, update, "name", mode)
    if mode == "apply":
        observed = await read_instances()
        for row in observed:
            if relevant(row) and any(
                row.get(field) in ids and ids[row[field]]["name"] not in PROFILE_NAMES
                for field in fields
            ):
                raise NativeConfigurationError("Seerr obsolete profile read-back drift")
        await verify_seerr_stored_references(
            service, client, [row for row in observed if relevant(row)], profiles
        )
    changes += await prune_native_quality_profiles(service, arr, mode)
    return changes
