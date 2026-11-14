"""Expand Radarr's Dublado title search without making Portuguese mandatory.

Radarr expands translated title searches from positive LanguageSpecifications
in a quality profile. A score of one is only a discovery hint; the worker's
audio evidence policy remains responsible for release selection.
"""

from copy import deepcopy

from .native_config import NativeConfigurationError, plan_settings

FORMAT_NAME = "HomeServer Dublado Search"
_LANGUAGES = {18: "Portuguese", 30: "Portuguese (Brazil)"}


async def _rows(client, path):
    response = await client.get(path)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise NativeConfigurationError("Native dubbed language configuration schema unsupported")
    return rows


def _managed(rows, name):
    matches = [row for row in rows if row.get("name") == name]
    if len(matches) > 1 or (
        matches and (type(matches[0].get("id")) is not int or matches[0]["id"] <= 0)
    ):
        raise NativeConfigurationError("Ambiguous native dubbed language configuration")
    return matches[0] if matches else None


def _semantic_format(row):
    """Ignore native IDs, field labels/options and order, retaining actual logic."""
    if row is None:
        return None
    specs = row.get("specifications")
    if not isinstance(specs, list):
        return None
    conditions = []
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get("fields"), list):
            return None
        fields = {}
        for field in spec["fields"]:
            if not isinstance(field, dict) or not isinstance(field.get("name"), str):
                return None
            if field["name"] in fields:
                return None
            fields[field["name"]] = field.get("value")
        if spec.get("implementation") == "LanguageSpecification":
            fields.setdefault("exceptLanguage", False)
        conditions.append(
            {
                "implementation": spec.get("implementation"),
                "negate": spec.get("negate", False),
                "required": spec.get("required", False),
                "values": fields,
            }
        )
    # Sorting native IDs/names would make equivalent specifications look different.
    import json

    conditions.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return {
        "name": row.get("name"),
        "includeCustomFormatWhenRenaming": row.get("includeCustomFormatWhenRenaming", False),
        "conditions": conditions,
    }


def _format(languages):
    selected = []
    for identifier, name in _LANGUAGES.items():
        matches = [row for row in languages if row.get("id") == identifier]
        if len(matches) > 1 or (
            matches and (type(matches[0].get("id")) is not int or matches[0].get("name") != name)
        ):
            raise NativeConfigurationError("Native Portuguese language identity unsupported")
        if matches:
            selected.append((identifier, name))
    if not selected:
        raise NativeConfigurationError("Native Portuguese search language unavailable")
    return {
        "name": FORMAT_NAME,
        "includeCustomFormatWhenRenaming": False,
        "specifications": [
            {
                "name": name,
                "implementation": "LanguageSpecification",
                "negate": False,
                "required": False,
                "fields": [
                    {"name": "value", "value": identifier},
                    {"name": "exceptLanguage", "value": False},
                ],
            }
            for identifier, name in selected
        ],
    }


def _profile_score(profile, format_id):
    if profile is None:
        return None
    items = profile.get("formatItems")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise NativeConfigurationError("Native Dublado format scores unsupported")
    if format_id is None:
        return None
    matches = [item for item in items if item.get("format") == format_id]
    if len(matches) > 1 or (matches and type(matches[0].get("score")) is not int):
        raise NativeConfigurationError("Native Dublado format scores ambiguous")
    return matches[0]["score"] if matches else None


async def reconcile_dubbed_language_search(client, mode):
    """Call after named profiles; only the managed CF and Dublado score are written."""
    if mode not in {"plan", "apply", "verify"}:
        raise ValueError("unsupported configuration mode")
    desired = _format(await _rows(client, "/api/v3/language"))
    current = _managed(await _rows(client, "/api/v3/customformat"), FORMAT_NAME)
    profile = _managed(await _rows(client, "/api/v3/qualityprofile"), "Dublado")
    if mode == "apply" and profile is None:
        raise NativeConfigurationError("Dublado profile required before language configuration")
    format_id = current["id"] if current else None
    score = _profile_score(profile, format_id)
    changes = plan_settings(
        "radarr",
        {"dubbedSearchFormat": _semantic_format(current)},
        {"dubbedSearchFormat": _semantic_format(desired)},
    )
    changes += plan_settings(
        "radarr",
        {"qualityprofile.Dublado.searchLanguageScore": score},
        {"qualityprofile.Dublado.searchLanguageScore": 1},
    )
    if mode != "apply" or not changes:
        return changes

    if _semantic_format(current) != _semantic_format(desired):
        if current is None:
            response = await client.post("/api/v3/customformat", json=desired)
        else:
            response = await client.put(
                f"/api/v3/customformat/{current['id']}", json=current | desired
            )
        response.raise_for_status()
        current = _managed(await _rows(client, "/api/v3/customformat"), FORMAT_NAME)
        if current is None or _semantic_format(current) != _semantic_format(desired):
            raise NativeConfigurationError("Native dubbed search format read-back drift")
    format_id = current["id"]
    # Native CF creation adds a score-zero entry to all profiles. Read it back
    # before changing Dublado so concurrent native additions are not overwritten.
    profile = _managed(await _rows(client, "/api/v3/qualityprofile"), "Dublado")
    if profile is None:
        raise NativeConfigurationError("Dublado profile disappeared during configuration")
    if _profile_score(profile, format_id) == 1:
        return changes
    payload = deepcopy(profile)
    items = payload["formatItems"]
    existing = next((item for item in items if item.get("format") == format_id), None)
    if existing is None:
        items.append({"format": format_id, "name": FORMAT_NAME, "score": 1})
    else:
        existing["score"] = 1
    response = await client.put(f"/api/v3/qualityprofile/{profile['id']}", json=payload)
    response.raise_for_status()
    observed = _managed(await _rows(client, "/api/v3/qualityprofile"), "Dublado")
    if observed is None or any(observed.get(key) != value for key, value in payload.items()):
        raise NativeConfigurationError("Native Dublado language score read-back drift")
    return changes
