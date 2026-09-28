import hashlib
import json
from urllib.parse import urlsplit

import httpx

from .native_config import NativeConfigurationError, ServiceOutcome, environment, plan_settings


async def bootstrap_bazarr_account(settings, client, mode):
    env = environment(settings)
    if not env.get("HOMESERVER_ADMIN_PASSWORD"):
        return []
    endpoint = "/api/system/settings"
    response = await client.get(endpoint)
    response.raise_for_status()
    auth = response.json().get("auth", {})
    if not isinstance(auth, dict) or not {"username", "password", "type"} <= auth.keys():
        raise NativeConfigurationError("Bazarr UI account capability unavailable")
    if auth["username"]:
        return []
    desired = {
        "username": env.get("HOMESERVER_ADMIN_USERNAME", "admin"),
        "password": env["HOMESERVER_ADMIN_PASSWORD"],
        "type": "form",
    }
    changes = plan_settings("bazarr", auth, desired)
    if mode == "apply":
        try:
            response = await client.post(
                endpoint, data={f"settings-auth-{key}": value for key, value in desired.items()}
            )
            response.raise_for_status()
        except httpx.TransportError:
            pass
        response = await client.get(endpoint)
        response.raise_for_status()
        # Native save_settings hashes plaintext exactly once; do not reapply stored hashes.
        expected = desired | {
            "password": hashlib.md5(desired["password"].encode("utf-8")).hexdigest()
        }
        if plan_settings("bazarr", response.json().get("auth", {}), expected):
            raise NativeConfigurationError("Bazarr UI account read-back drift")
    return changes


async def reconcile_bazarr(settings, client, mode):
    response = await client.get("/api/system/settings")
    response.raise_for_status()
    actual = response.json()
    if not isinstance(actual, dict) or not isinstance(actual.get("general"), dict):
        return ServiceOutcome("bazarr", "unsupported", message="Unknown settings API shape")
    env = environment(settings)
    desired = {
        "general": {
            "enabled_providers": [
                value
                for value in env.get("HOMESERVER_BAZARR_PROVIDERS", "subdl,opensubtitlescom").split(
                    ","
                )
                if value
            ]
        }
    }
    for service in ("sonarr", "radarr"):
        if env.get(f"HOMESERVER_{service.upper()}_API_KEY"):
            url = urlsplit(env[f"HOMESERVER_{service.upper()}_URL"])
            desired[service] = {
                "ip": url.hostname,
                "port": url.port or 80,
                "ssl": url.scheme == "https",
                "base_url": url.path.rstrip("/"),
                "apikey": env[f"HOMESERVER_{service.upper()}_API_KEY"],
            }
            desired["general"]["use_" + service] = True
    for section, fields in {
        "subdl": {"api_key": "SUBDL_API_KEY"},
        "opensubtitlescom": {
            "username": "OPENSUBTITLES_USERNAME",
            "password": "OPENSUBTITLES_PASSWORD",
        },
    }.items():
        values = {
            key: env["HOMESERVER_" + field]
            for key, field in fields.items()
            if env.get("HOMESERVER_" + field)
        }
        if values:
            desired[section] = values
    changes = []
    data = {}
    managed_profile = None
    if "HOMESERVER_SUBTITLE_LANGUAGES" in env:
        response = await client.get("/api/system/languages/profiles")
        if response.status_code in (404, 405):
            return ServiceOutcome(
                "bazarr", "unsupported", message="Language profiles API unavailable"
            )
        response.raise_for_status()
        profiles = response.json()
        if not isinstance(profiles, list) or any("profileId" not in item for item in profiles):
            return ServiceOutcome(
                "bazarr", "unsupported", message="Unknown language profile API shape"
            )
        matches = [item for item in profiles if item.get("name") == "HomeServer"]
        if len(matches) > 1:
            raise NativeConfigurationError("Ambiguous Bazarr profile identity")
        existing = matches[0] if matches else {}
        languages = [
            {"pt-BR": "pb", "en-US": "en"}[code]
            for code in env["HOMESERVER_SUBTITLE_LANGUAGES"].split(",")
        ]
        profile_id = existing.get(
            "profileId", max((item["profileId"] for item in profiles), default=0) + 1
        )
        managed_profile = existing | {
            "profileId": profile_id,
            "name": "HomeServer",
            "cutoff": 1,
            "items": [
                {
                    "id": index,
                    "language": language,
                    "forced": "False",
                    "hi": "False",
                    "audio_exclude": "True" if language == "pb" else "False",
                    "audio_only_include": "False",
                }
                for index, language in enumerate(languages, 1)
            ],
            "mustContain": existing.get("mustContain", []),
            "mustNotContain": existing.get("mustNotContain", []),
            "originalFormat": existing.get("originalFormat"),
        }
        profile_changes = plan_settings("bazarr", existing, managed_profile)
        changes += profile_changes
        if profile_changes:
            # Native endpoint deletes omitted profiles. Include EVERY unmanaged ID unchanged.
            merged = [
                managed_profile if item.get("profileId") == profile_id else item
                for item in profiles
            ]
            if not existing:
                merged.append(managed_profile)
            data["languages-profiles"] = json.dumps(merged)
        response = await client.get("/api/system/languages")
        response.raise_for_status()
        enabled = [item["code2"] for item in response.json() if item.get("enabled")]
        if not set(languages) <= set(enabled):
            desired_languages = sorted(set(enabled) | set(languages))
            data["languages-enabled"] = desired_languages
            changes += plan_settings(
                "bazarr", {"enabled_languages": enabled}, {"enabled_languages": desired_languages}
            )
        desired["general"].update(
            serie_default_enabled=True,
            movie_default_enabled=True,
            serie_default_profile=profile_id,
            movie_default_profile=profile_id,
        )
    for section, fields in desired.items():
        if section not in actual or any(key not in actual[section] for key in fields):
            return ServiceOutcome(
                "bazarr", "unsupported", message="Native provider settings shape unsupported"
            )
        changes += plan_settings(
            "bazarr", {section: {key: actual[section][key] for key in fields}}, {section: fields}
        )
        for key, value in fields.items():
            if actual[section][key] != value:
                data[f"settings-{section}-{key}"] = (
                    str(value).lower()
                    if isinstance(value, bool)
                    else [""]
                    if value == []
                    else value
                )
    if changes and mode == "apply":
        try:
            response = await client.post("/api/system/settings", data=data)
            response.raise_for_status()
        except httpx.TransportError:
            pass
        observed = await client.get("/api/system/settings")
        observed.raise_for_status()
        for section, fields in desired.items():
            if plan_settings("bazarr", observed.json().get(section, {}), fields):
                raise NativeConfigurationError("Bazarr read-back drift")
        if managed_profile:
            observed = await client.get("/api/system/languages/profiles")
            observed.raise_for_status()
            preserved = {item["profileId"] for item in observed.json()}
            if not {item["profileId"] for item in profiles} <= preserved:
                raise NativeConfigurationError("Bazarr unmanaged profile read-back drift")
            matches = [
                item
                for item in observed.json()
                if item["profileId"] == managed_profile["profileId"]
            ]
            if len(matches) != 1 or plan_settings("bazarr", matches[0], managed_profile):
                raise NativeConfigurationError("Bazarr profile read-back drift")
    return ServiceOutcome(
        "bazarr",
        "drift"
        if changes and mode == "verify"
        else "planned"
        if changes and mode == "plan"
        else "verified",
        changes,
    )
