import json
from copy import deepcopy

from .arr import collection
from .native_config import NativeConfigurationError, ServiceOutcome, environment, plan_settings

BYPARR_TAG = "homeserver-byparr"


async def native_tags(client):
    response = await client.get("/api/v1/tag")
    if response.status_code in (404, 405, 501):
        return None
    response.raise_for_status()
    try:
        rows = response.json()
    except ValueError:
        return None
    if not isinstance(rows, list) or any(
        not isinstance(row, dict)
        or type(row.get("id")) is not int
        or row["id"] <= 0
        or not isinstance(row.get("label"), str)
        or not row["label"]
        for row in rows
    ):
        return None
    if len({row["id"] for row in rows}) != len(rows) or len(
        {row["label"].lower() for row in rows}
    ) != len(rows):
        return None
    return rows


async def bind_byparr_tag(client, endpoint, desired, tag_id):
    """Add only the managed linkage; leave existing and explicitly supplied tags intact."""
    response = await client.get(endpoint)
    if response.status_code in (404, 405, 501):
        return None
    response.raise_for_status()
    try:
        rows = response.json()
    except ValueError:
        return None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return None
    matches = [
        row
        for row in rows
        if (
            row.get("id") == desired["id"]
            if desired.get("id")
            else row.get("name") == desired["name"]
        )
    ]
    if len(matches) > 1:
        raise NativeConfigurationError("ambiguous native resource identity")
    current = matches[0] if matches else {}
    tag_lists = (current.get("tags", []), desired.get("tags", []))
    if any(
        not isinstance(tags, list) or any(type(value) is not int or value <= 0 for value in tags)
        for tags in tag_lists
    ):
        return None
    desired["tags"] = sorted({tag_id, *(value for tags in tag_lists for value in tags)})
    return desired


async def reconcile_prowlarr(settings, client, mode):
    env = environment(settings)
    desired = json.loads(env.get("HOMESERVER_PROWLARR_INDEXERS", "[]"))
    changes = []
    byparr_tag_id = None
    for service, port in [("radarr", 7878), ("sonarr", 8989)]:
        key = env.get(f"HOMESERVER_{service.upper()}_API_KEY")
        if not key:
            continue
        response = await client.get("/api/v1/applications/schema")
        if response.status_code in (404, 405):
            return ServiceOutcome(
                "prowlarr", "unsupported", message="Native application schema unavailable"
            )
        response.raise_for_status()
        templates = [
            item for item in response.json() if item.get("implementation") == service.title()
        ]
        if len(templates) != 1:
            return ServiceOutcome(
                "prowlarr", "unsupported", message="Native Arr application unsupported"
            )
        app = deepcopy(templates[0])
        app.pop("id", None)
        app.update(name=f"HomeServer {service.title()}", syncLevel="addOnly", tags=[])
        values = {
            "prowlarrUrl": env.get("HOMESERVER_PROWLARR_URL", "http://prowlarr:9696"),
            "baseUrl": env.get(f"HOMESERVER_{service.upper()}_URL", f"http://{service}:{port}"),
            "apiKey": key,
        }
        if not set(values) <= {field["name"] for field in app.get("fields", [])}:
            return ServiceOutcome(
                "prowlarr", "unsupported", message="Native application fields unsupported"
            )
        for field in app["fields"]:
            if field["name"] in values:
                field["value"] = values[field["name"]]
        changes += await collection(
            "prowlarr", client, "/api/v1/applications", app, "name", mode, settings=settings
        )
    if env.get("HOMESERVER_BYPARR_ENABLED", "false") == "true":
        response = await client.get("/api/v1/indexerProxy/schema")
        if response.status_code in (404, 405):
            return ServiceOutcome(
                "prowlarr", "unsupported", message="Native proxy capabilities unavailable"
            )
        response.raise_for_status()
        templates = [
            item for item in response.json() if item.get("implementation") == "FlareSolverr"
        ]
        if len(templates) != 1:
            return ServiceOutcome(
                "prowlarr",
                "unsupported",
                message="Byparr-compatible FlareSolverr proxy unavailable",
            )
        proxy = deepcopy(templates[0])
        proxy.pop("id", None)
        proxy.update(name="HomeServer Byparr")
        fields = [field for field in proxy.get("fields", []) if field.get("name") == "host"]
        if len(fields) != 1:
            return ServiceOutcome(
                "prowlarr", "unsupported", message="Unknown native proxy host setting"
            )
        fields[0]["value"] = env.get("HOMESERVER_BYPARR_URL", "http://byparr:8191")
        tags = await native_tags(client)
        if tags is None:
            return ServiceOutcome(
                "prowlarr", "unsupported", changes, "Native proxy linkage tag API unsupported"
            )
        changes += await collection(
            "prowlarr", client, "/api/v1/tag", {"label": BYPARR_TAG}, "label", mode
        )
        if mode == "apply":
            tags = await native_tags(client)
            if tags is None:
                raise NativeConfigurationError("Native proxy linkage tag read-back drift")
        matches = [tag for tag in tags if tag["label"] == BYPARR_TAG]
        if not matches:
            if mode != "apply":
                changes += plan_settings(
                    "prowlarr", {}, {"byparr.tagId": "Resolve native linkage tag ID after apply"}
                )
                return ServiceOutcome(
                    "prowlarr", "planned" if mode == "plan" else "drift", changes
                )
            raise NativeConfigurationError("Native proxy linkage tag read-back drift")
        byparr_tag_id = matches[0]["id"]
        proxy = await bind_byparr_tag(client, "/api/v1/indexerProxy", proxy, byparr_tag_id)
        if proxy is None:
            return ServiceOutcome(
                "prowlarr", "unsupported", changes, "Native proxy tag schema unsupported"
            )
        changes += await collection(
            "prowlarr", client, "/api/v1/indexerProxy", proxy, "name", mode, settings=settings
        )
    for item in desired:
        if not isinstance(item, dict) or not item.get("name") or not item.get("implementation"):
            return ServiceOutcome(
                "prowlarr",
                "unsupported",
                changes,
                "Indexer input requires native name and implementation schema",
            )
        response = await client.get("/api/v1/indexer/schema")
        if response.status_code in (404, 405):
            return ServiceOutcome(
                "prowlarr", "unsupported", changes, "Native public definitions API unavailable"
            )
        response.raise_for_status()
        templates = [
            template
            for template in response.json()
            if template.get("implementation") == item["implementation"]
            and (
                template.get("definitionName") == item["definitionName"]
                if item.get("definitionName")
                else template.get("name") == item["name"]
            )
        ]
        if len(templates) != 1:
            return ServiceOutcome(
                "prowlarr",
                "unsupported",
                changes,
                "Native indexer definition unavailable or ambiguous",
            )
        provided = deepcopy(item)
        item = deepcopy(templates[0])
        item.pop("id", None)
        item.update({key: value for key, value in provided.items() if key != "fields"})
        values = {field["name"]: field["value"] for field in provided.get("fields", [])}
        if not set(values) <= {field["name"] for field in item.get("fields", [])}:
            return ServiceOutcome(
                "prowlarr",
                "unsupported",
                changes,
                "Unknown native indexer credential/configuration field",
            )
        for field in item.get("fields", []):
            if field["name"] in values:
                field["value"] = values[field["name"]]
        for key in (
            "added",
            "status",
            "sortName",
            "description",
            "language",
            "encoding",
            "capabilities",
            "supportsRss",
            "supportsSearch",
            "supportsRedirect",
            "supportsPagination",
            "legacyUrls",
            "infoLink",
            "implementationName",
        ):
            item.pop(key, None)
        profile = {
            "name": "HomeServer",
            "enableRss": False,
            "enableAutomaticSearch": False,
            "enableInteractiveSearch": True,
        }
        response = await client.get("/api/v1/appprofile")
        if response.status_code in (404, 405):
            return ServiceOutcome(
                "prowlarr", "unsupported", changes, "Native app-profile API unavailable"
            )
        response.raise_for_status()
        profiles = response.json()
        changes += await collection("prowlarr", client, "/api/v1/appprofile", profile, "name", mode)
        if mode == "apply":
            response = await client.get("/api/v1/appprofile")
            response.raise_for_status()
            profiles = response.json()
        matches = [profile for profile in profiles if profile.get("name") == "HomeServer"]
        if len(matches) != 1:
            if mode == "plan":
                changes += plan_settings(
                    "prowlarr",
                    {},
                    {"indexer.appProfileId": "Resolve native app profile ID after apply"},
                )
                return ServiceOutcome("prowlarr", "planned", changes)
            return ServiceOutcome("prowlarr", "drift", changes, "Managed app profile missing")
        item.update(enable=True, appProfileId=matches[0]["id"])
        if byparr_tag_id is not None:
            item = await bind_byparr_tag(client, "/api/v1/indexer", item, byparr_tag_id)
            if item is None:
                return ServiceOutcome(
                    "prowlarr", "unsupported", changes, "Native indexer tag schema unsupported"
                )
        changes += await collection(
            "prowlarr", client, "/api/v1/indexer", item, "name", mode, settings=settings
        )
    return ServiceOutcome(
        "prowlarr",
        "drift"
        if changes and mode == "verify"
        else "planned"
        if changes and mode == "plan"
        else "verified",
        changes,
    )
