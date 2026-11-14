import copy
import json

import httpx
import pytest


class NativeFormats:
    def __init__(self):
        self.formats = []
        self.writes = []
        self.languages = [
            {"id": 1, "name": "English"},
            {"id": 18, "name": "Portuguese"},
            {"id": 30, "name": "Portuguese (Brazil)"},
        ]
        self.profiles = [
            {"id": 1, "name": "Standard", "formatItems": [], "minFormatScore": 0},
            {"id": 2, "name": "Max Quality", "formatItems": [], "minFormatScore": 0},
            {
                "id": 3,
                "name": "Dublado",
                "language": {"id": -1, "name": "Any"},
                "formatItems": [{"format": 40, "name": "Existing", "score": 50}],
                "minFormatScore": 0,
                "cutoffFormatScore": 20,
                "upgradeAllowed": False,
                "items": [{"quality": {"id": 7}, "allowed": True}],
            },
        ]

    def enrich(self, value):
        value = copy.deepcopy(value)
        for spec in value["specifications"]:
            spec.update(implementationName="Language", infoLink="https://native.example", id=0)
            for field in spec["fields"]:
                field.update(order=1, label="Localized native label", type="select", advanced=False)
            spec["fields"] = list(reversed(spec["fields"]))
        value["specifications"] = list(reversed(value["specifications"]))
        return value

    def handle(self, request):
        path = request.url.path
        if request.method != "GET":
            payload = json.loads(request.content)
            self.writes.append((request.method, path, copy.deepcopy(payload)))
            if path == "/api/v3/customformat":
                self.formats.append(self.enrich(payload | {"id": 99}))
                # Native Radarr adds a zero-score slot to every profile on CF creation.
                for profile in self.profiles:
                    profile["formatItems"].append(
                        {"format": 99, "name": payload["name"], "score": 0}
                    )
                return httpx.Response(201, json=self.formats[-1])
            if path == "/api/v3/customformat/99":
                self.formats[0] = self.enrich(payload)
                return httpx.Response(200, json=self.formats[0])
            if path == "/api/v3/qualityprofile/3":
                self.profiles[2] = payload
                return httpx.Response(200, json=payload)
            raise AssertionError(f"Unexpected mutation {request.method} {path}")
        values = {
            "/api/v3/language": self.languages,
            "/api/v3/customformat": self.formats,
            "/api/v3/qualityprofile": self.profiles,
        }
        assert path in values, path
        return httpx.Response(200, json=values[path])


@pytest.mark.asyncio
async def test_adds_portuguese_search_languages_only_to_dubbed_without_requiring_cf_score():
    from homeserver_control.configuration.dubbed_language import reconcile_dubbed_language_search

    native = NativeFormats()
    original = copy.deepcopy(native.profiles[2])
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        changes = await reconcile_dubbed_language_search(client, "apply")
    assert changes
    custom = native.formats[0]
    assert custom["includeCustomFormatWhenRenaming"] is False
    assert {
        next(field["value"] for field in spec["fields"] if field["name"] == "value")
        for spec in custom["specifications"]
    } == {18, 30}
    assert all(
        spec["implementation"] == "LanguageSpecification"
        and not spec["negate"]
        and not spec["required"]
        for spec in custom["specifications"]
    )
    assert [(method, path) for method, path, _ in native.writes] == [
        ("POST", "/api/v3/customformat"),
        ("PUT", "/api/v3/qualityprofile/3"),
    ]
    dubbed = native.profiles[2]
    assert next(item["score"] for item in dubbed["formatItems"] if item["format"] == 99) == 1
    assert dubbed["formatItems"][0] == original["formatItems"][0]
    assert {key: value for key, value in dubbed.items() if key != "formatItems"} == {
        key: value for key, value in original.items() if key != "formatItems"
    }
    assert all(profile["formatItems"][0]["score"] == 0 for profile in native.profiles[:2])


@pytest.mark.asyncio
async def test_native_specification_metadata_and_order_do_not_cause_repeated_updates():
    from homeserver_control.configuration.dubbed_language import reconcile_dubbed_language_search

    native = NativeFormats()
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        await reconcile_dubbed_language_search(client, "apply")
        native.writes.clear()
        assert await reconcile_dubbed_language_search(client, "apply") == []
        assert await reconcile_dubbed_language_search(client, "verify") == []
    assert native.writes == []


@pytest.mark.asyncio
async def test_plan_does_not_write_and_supported_language_ids_are_read_from_native():
    from homeserver_control.configuration.dubbed_language import reconcile_dubbed_language_search

    native = NativeFormats()
    native.languages = [{"id": 1, "name": "English"}, {"id": 18, "name": "Portuguese"}]
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        assert await reconcile_dubbed_language_search(client, "plan")
        assert native.writes == []
        await reconcile_dubbed_language_search(client, "apply")
    assert len(native.formats[0]["specifications"]) == 1


@pytest.mark.asyncio
async def test_repairs_semantic_drift_but_preserves_other_formats_and_profile_fields():
    from homeserver_control.configuration.dubbed_language import reconcile_dubbed_language_search

    native = NativeFormats()
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        await reconcile_dubbed_language_search(client, "apply")
        native.formats[0]["specifications"][0]["negate"] = True
        native.profiles[2]["formatItems"][-1]["score"] = -1
        native.writes.clear()
        assert await reconcile_dubbed_language_search(client, "apply")
    assert [(method, path) for method, path, _ in native.writes] == [
        ("PUT", "/api/v3/customformat/99"),
        ("PUT", "/api/v3/qualityprofile/3"),
    ]
    assert not any(spec["negate"] for spec in native.formats[0]["specifications"])
    assert native.profiles[2]["minFormatScore"] == 0


@pytest.mark.asyncio
async def test_unknown_or_conflicting_portuguese_identifiers_never_write():
    from homeserver_control.configuration.dubbed_language import reconcile_dubbed_language_search
    from homeserver_control.configuration.native_config import NativeConfigurationError

    native = NativeFormats()
    native.languages = [{"id": 18, "name": "English"}]
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        with pytest.raises(NativeConfigurationError):
            await reconcile_dubbed_language_search(client, "apply")
    assert native.writes == []


@pytest.mark.asyncio
async def test_ignored_native_write_is_detected_before_changing_profile_score():
    from homeserver_control.configuration.dubbed_language import reconcile_dubbed_language_search
    from homeserver_control.configuration.native_config import NativeConfigurationError

    native = NativeFormats()

    def rejected_write(request):
        response = native.handle(request)
        if request.method == "POST" and request.url.path == "/api/v3/customformat":
            native.formats[0]["specifications"][0]["negate"] = True
        return response

    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(rejected_write)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="read-back drift"):
            await reconcile_dubbed_language_search(client, "apply")
    assert len(native.writes) == 1
    assert native.profiles[2]["formatItems"][-1]["score"] == 0
