"""Native animation profiles admit Brazilian dubbing without changing live action."""

import json
from copy import deepcopy

import httpx
import pytest

from homeserver_control.configuration.arr import reconcile_animation_quality_profile, reconcile_arr


class NativeProfiles:
    def __init__(self, *, languages=None, language_status=200):
        self.base = {
            "id": 20,
            "name": "HomeServer",
            "language": {"id": -2, "name": "Original"},
            "upgradeAllowed": False,
            "cutoff": 7,
            "minFormatScore": 0,
            "cutoffFormatScore": 15,
            "minUpgradeFormatScore": 1,
            "formatItems": [{"format": 3, "name": "Fixture HDR", "score": 15}],
            "items": [
                {"quality": {"id": 4, "name": "HDTV-720p", "resolution": 720,
                             "source": "television"}, "allowed": False},
                {"quality": {"id": 7, "name": "Bluray-1080p", "resolution": 1080,
                             "source": "bluray"}, "allowed": True},
                {"quality": {"id": 19, "name": "Remux-2160p", "resolution": 2160,
                             "source": "blurayRaw"}, "allowed": False},
            ],
            "manual": "preserved base field",
        }
        self.foreign = self.base | {"id": 11, "name": "Manual", "manual": "foreign"}
        self.profiles = deepcopy([self.base, self.foreign])
        self.languages = ([{"id": -1, "name": "Any", "nameLower": "any"},
                           {"id": -2, "name": "Original"},
                           {"id": 1, "name": "English"}]
                          if languages is None else languages)
        self.language_status = language_status
        self.writes = []
        self.reads = []
        self.download = {
            "id": 12, "name": "HomeServer Gateway", "implementation": "QBittorrent",
            "enable": True, "priority": 1, "removeCompletedDownloads": False,
            "removeFailedDownloads": False,
            "fields": [{"name": name, "value": value} for name, value in {
                "host": "download-gateway", "port": 8081, "useSsl": False, "urlBase": "",
                "username": "arr", "password": "fixture", "movieCategory": "radarr",
            }.items()],
        }

    def handle(self, request):
        path = request.url.path
        if request.method == "GET":
            self.reads.append(path)
        if path == "/api/v3/language":
            assert request.method == "GET"
            return httpx.Response(self.language_status, json=self.languages)
        if path == "/api/v3/qualityprofile/schema":
            schema = deepcopy(self.base)
            schema.pop("id")
            schema.pop("manual")
            schema["name"] = "Any"
            return httpx.Response(200, json=schema)
        if path == "/api/v3/downloadclient/schema":
            schema = deepcopy(self.download)
            schema.pop("id")
            return httpx.Response(200, json=[schema])
        if path == "/api/v3/downloadclient":
            assert request.method == "GET"
            return httpx.Response(200, json=[self.download])
        if path.startswith("/api/v3/qualityprofile"):
            if request.method in {"POST", "PUT"}:
                payload = json.loads(request.content)
                self.writes.append((request.method, path, deepcopy(payload)))
                if isinstance(payload.get("language"), dict):
                    payload["language"] = {
                        key: payload["language"][key] for key in ("id", "name")
                    }
                if request.method == "POST":
                    payload["id"] = max(row["id"] for row in self.profiles) + 1
                    self.profiles.append(payload)
                else:
                    self.profiles = [
                        payload if row["id"] == payload["id"] else row for row in self.profiles
                    ]
                return httpx.Response(200, json=payload)
            return httpx.Response(200, json=self.profiles)
        raise AssertionError(f"Unexpected native endpoint {request.method} {path}")


def environment():
    return {
        "HOMESERVER_MOVIE_ROOT": "",
        "HOMESERVER_MOVIE_RESOLUTIONS": "1080",
        "HOMESERVER_MEDIA_SOURCES": "bluray",
        "HOMESERVER_ARR_TOKEN": "fixture",
        "HOMESERVER_ANIMATION_AUDIO_PREFERENCES": "dual,pt-BR,original",
    }


@pytest.mark.asyncio
async def test_radarr_animation_profile_plan_verify_apply_and_idempotent():
    native = NativeProfiles()
    before = deepcopy(native.profiles)
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle),
    ) as client:
        verify = await reconcile_arr("radarr", environment(), client, "verify")
        assert verify.status == "drift"
        assert native.writes == []
        planned = await reconcile_arr("radarr", environment(), client, "plan")
        assert planned.status == "planned"
        assert native.writes == []
        applied = await reconcile_arr("radarr", environment(), client, "apply")
        assert applied.status == "verified"
        assert applied.changes
        assert not (await reconcile_arr("radarr", environment(), client, "apply")).changes
        assert (await reconcile_arr("radarr", environment(), client, "verify")).status == "verified"
    assert native.profiles[:2] == before
    animation = native.profiles[2]
    assert animation["name"] == "HomeServer Animation"
    assert animation["language"] == {"id": -1, "name": "Any"}
    for key in ("items", "cutoff", "upgradeAllowed", "minFormatScore", "cutoffFormatScore",
                "minUpgradeFormatScore", "formatItems"):
        assert animation[key] == native.base[key]
    assert len(native.writes) == 1
    assert native.writes[0][:2] == ("POST", "/api/v3/qualityprofile")
    assert native.writes[0][2]["language"] == {"id": -1, "name": "Any"}


@pytest.mark.asyncio
async def test_existing_animation_profile_keeps_identity_and_foreign_fields():
    native = NativeProfiles()
    native.profiles.append(deepcopy(native.base) | {
        "id": 35, "name": "HomeServer Animation", "manual": "keep animation field",
    })
    before = deepcopy(native.profiles[:2])
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle),
    ) as client:
        outcome = await reconcile_arr("radarr", environment(), client, "apply")
    assert outcome.status == "verified"
    assert native.profiles[:2] == before
    assert native.profiles[2]["id"] == 35
    assert native.profiles[2]["manual"] == "keep animation field"
    assert native.profiles[2]["language"] == {"id": -1, "name": "Any"}
    assert [write[:2] for write in native.writes] == [("PUT", "/api/v3/qualityprofile/35")]


@pytest.mark.asyncio
@pytest.mark.parametrize("languages,status", [
    ([{"id": 1, "name": "English"}], 200),
    ([{"id": 5, "name": "Any"}], 200),
    ([{"id": -1, "name": "Original"}], 200),
    ([{"id": -1, "name": "Any"}, {"id": -1, "name": "Any"}], 200),
    ([{"id": -1, "name": "Any"}, {"id": -1, "name": "Original"}], 200),
    ([{"id": True, "name": "Any"}], 200),
    ([], 200),
    ({"id": -1, "name": "Any"}, 200),
    ([{"id": -1, "name": "Any"}, None], 200),
    ([], 404),
    ([], 405),
    ([], 501),
])
async def test_missing_any_language_reports_unsupported_without_mutation(languages, status):
    native = NativeProfiles(languages=languages, language_status=status)
    before = deepcopy(native.profiles)
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle),
    ) as client:
        outcome = await reconcile_arr("radarr", environment(), client, "apply")
    assert outcome.status == "unsupported"
    assert "animation" in outcome.message.lower()
    assert native.profiles == before
    assert native.writes == []


@pytest.mark.asyncio
async def test_unconfigured_animation_does_not_query_or_change_language():
    native = NativeProfiles()
    settings = environment()
    settings.pop("HOMESERVER_ANIMATION_AUDIO_PREFERENCES")
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle),
    ) as client:
        outcome = await reconcile_arr("radarr", settings, client, "apply")
    assert outcome.status == "verified"
    assert "/api/v3/language" not in native.reads
    assert native.writes == []


@pytest.mark.asyncio
async def test_sonarr_does_not_get_radarr_animation_language_profile():
    def handler(request):
        raise AssertionError("Sonarr must not query or configure a hard language profile")

    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(handler),
    ) as client:
        assert await reconcile_animation_quality_profile(
            "sonarr", environment(), client, "apply", {},
        ) == []
