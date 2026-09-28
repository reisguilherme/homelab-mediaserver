import json
from copy import deepcopy

import httpx
import pytest

from homeserver_control.configuration.arr import reconcile_arr
from homeserver_control.configuration.native_config import NativeConfigurationError


def native_definition(
    identifier, quality_id, name, source, resolution, minimum, maximum, preferred
):
    return {
        "id": identifier,
        "quality": {
            "id": quality_id,
            "name": name,
            "source": source,
            "resolution": resolution,
            "modifier": "none",
        },
        "title": name,
        "weight": identifier,
        "minSize": minimum,
        "maxSize": maximum,
        "preferredSize": preferred,
        "manual": {"group": "preserve"},
    }


class NativeArr:
    """HTTP fixture preserving native IDs and API read-back semantics."""

    def __init__(self, definitions, *, accept_size_writes=True, omit_nullable_sizes=False):
        self.definitions = deepcopy(definitions)
        self.accept_size_writes = accept_size_writes
        self.omit_nullable_sizes = omit_nullable_sizes
        self.requests = []
        self.size_writes = []
        self.config_writes = []
        self.accept_config_writes = True
        self.config = {
            "downloadclient": {
                "id": 4,
                "enableCompletedDownloadHandling": False,
                "checkForFinishedDownloadInterval": 2,
                "manual": {"keep": "download"},
            },
            "mediamanagement": {
                "id": 8,
                "copyUsingHardlinks": True,
                "minimumFreeSpaceWhenImporting": 100,
                "manual": {"keep": "media"},
            },
        }
        self.collections = {"qualityprofile": [], "downloadclient": []}

    def request(self, request):
        self.requests.append((request.method, request.url.path))
        endpoint = request.url.path.split("/")[3]
        if endpoint == "config":
            resource = request.url.path.split("/")[4]
            assert resource in self.config
            if request.method == "PUT":
                payload = json.loads(request.content)
                assert request.url.path == f"/api/v3/config/{resource}/{payload['id']}"
                self.config_writes.append((resource, payload))
                if self.accept_config_writes:
                    self.config[resource] = payload
                return httpx.Response(202, json=payload)
            assert request.method == "GET"
            return httpx.Response(200, json=self.config[resource])
        if endpoint == "qualitydefinition":
            if request.method == "PUT":
                payload = json.loads(request.content)
                assert request.url.path == f"/api/v3/qualitydefinition/{payload['id']}"
                self.size_writes.append(payload)
                if self.accept_size_writes:
                    self.definitions = [
                        payload if row["id"] == payload["id"] else row for row in self.definitions
                    ]
                return httpx.Response(202, json=payload)
            assert request.method == "GET"
            rows = self.definitions
            if self.omit_nullable_sizes:
                rows = [
                    {
                        key: value
                        for key, value in row.items()
                        if key not in ("maxSize", "preferredSize") or value is not None
                    }
                    for row in rows
                ]
            return httpx.Response(200, json=rows)
        if request.url.path.endswith("/schema"):
            if endpoint == "qualityprofile":
                return httpx.Response(
                    200,
                    json={
                        "name": "Any",
                        "items": [
                            {"quality": row["quality"], "allowed": True}
                            for row in self.definitions
                            if isinstance(row, dict) and isinstance(row.get("quality"), dict)
                        ],
                    },
                )
            assert endpoint == "downloadclient"
            return httpx.Response(
                200,
                json=[
                    {
                        "implementation": "QBittorrent",
                        "fields": [
                            {"name": name, "value": ""}
                            for name in (
                                "host",
                                "port",
                                "useSsl",
                                "urlBase",
                                "username",
                                "password",
                                "movieCategory",
                                "tvCategory",
                            )
                        ],
                    }
                ],
            )
        assert endpoint in self.collections
        rows = self.collections[endpoint]
        if request.method == "POST":
            row = json.loads(request.content) | {"id": 71 + len(rows)}
            rows.append(row)
            return httpx.Response(201, json=row)
        if request.method == "PUT":
            row = json.loads(request.content)
            rows[:] = [row if current["id"] == row["id"] else current for current in rows]
            return httpx.Response(202, json=row)
        assert request.method == "GET"
        return httpx.Response(200, json=rows)


def settings(**overrides):
    return {
        "HOMESERVER_MOVIE_ROOT": "",
        "HOMESERVER_SERIES_ROOT": "",
        "HOMESERVER_MEDIA_RESOLUTIONS": "2160,1080,720",
        "HOMESERVER_MEDIA_SOURCES": "remux,bluray,webdl",
        "HOMESERVER_QUALITY_MIN_MIB_PER_MIN_720": "10",
        "HOMESERVER_QUALITY_MIN_MIB_PER_MIN_1080": "20",
        "HOMESERVER_QUALITY_MIN_MIB_PER_MIN_2160": "50",
    } | overrides


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_native_size_floors_have_no_upper_limit_preserve_ids_and_are_idempotent(service):
    definitions = [
        native_definition(11, 3, "WEBDL-1080p", "web", 1080, 0, 140, 95),
        native_definition(12, 7, "Bluray-1080p", "bluray", 1080, 5, 140, 5),
        native_definition(13, 31, "Bluray-2160p Remux", "blurayRaw", 2160, 0, 0, None),
        native_definition(14, 15, "WEBRip-1080p", "web", 1080, 0, 100, 40),
        native_definition(15, 1, "HDTV-720p", "television", 720, 0, 80, 35),
        native_definition(16, 2, "WEBDL-720p", "web", 720, 2, 80, 50),
    ]
    native = NativeArr(definitions)
    async with httpx.AsyncClient(
        base_url=f"http://{service}", transport=httpx.MockTransport(native.request)
    ) as client:
        first = await reconcile_arr(service, settings(), client, "apply")
        second = await reconcile_arr(service, settings(), client, "apply")
        verified = await reconcile_arr(service, settings(), client, "verify")
    assert first.status == second.status == verified.status == "verified"
    assert not second.changes
    assert not verified.changes
    assert len(native.size_writes) == 4
    rows = {row["id"]: row for row in native.definitions}
    assert (rows[11]["minSize"], rows[11]["maxSize"], rows[11]["preferredSize"]) == (20, None, 95)
    assert (rows[12]["minSize"], rows[12]["maxSize"], rows[12]["preferredSize"]) == (20, None, 20)
    assert (rows[13]["minSize"], rows[13]["maxSize"], rows[13]["preferredSize"]) == (50, None, None)
    assert (rows[16]["minSize"], rows[16]["maxSize"], rows[16]["preferredSize"]) == (10, None, 50)
    assert rows[14] == definitions[3]
    assert rows[15] == definitions[4]
    assert all(row["manual"] == {"group": "preserve"} for row in native.definitions)
    assert {row["quality"]["id"] for row in native.size_writes} == {3, 7, 31, 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, status", [("plan", "planned"), ("verify", "drift")])
async def test_size_floor_plan_and_verify_expose_drift_without_writing(mode, status):
    definition = native_definition(18, 3, "WEBDL-1080p", "web", 1080, 0, 150, 95)
    native = NativeArr([definition])
    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(native.request)
    ) as client:
        result = await reconcile_arr("sonarr", settings(), client, mode)
    assert result.status == status
    assert native.definitions == [definition]
    assert not native.size_writes
    assert any(change.after == 20 for change in result.changes)
    assert any(change.after is None and change.before == 150 for change in result.changes)


@pytest.mark.asyncio
async def test_native_size_write_acknowledgement_without_persistence_is_rejected():
    native = NativeArr(
        [native_definition(18, 3, "WEBDL-1080p", "web", 1080, 0, 150, 95)],
        accept_size_writes=False,
    )
    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(native.request)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="read-back drift"):
            await reconcile_arr("sonarr", settings(), client, "apply")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformation", ["container", "missing", "unknown", "preferred", "duplicate"]
)
async def test_unknown_native_size_schema_is_unsupported_without_any_writes(malformation):
    valid = native_definition(18, 3, "WEBDL-1080p", "web", 1080, 0, 150, 95)
    definitions = [valid, native_definition(19, 7, "Bluray-1080p", "bluray", 1080, 5, 140, 95)]
    if malformation == "container":
        definitions = {"items": definitions}
    elif malformation == "missing":
        del definitions[1]["minSize"]
    elif malformation == "unknown":
        definitions[1]["minSize"] = "unknown"
    elif malformation == "preferred":
        definitions[1]["preferredSize"] = "unknown"
    else:
        definitions[1]["id"] = 18
    native = NativeArr(definitions)
    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(native.request)
    ) as client:
        result = await reconcile_arr("sonarr", settings(), client, "apply")
    assert result.status == "unsupported"
    assert "size" in result.message.lower()
    assert not any(method != "GET" for method, _ in native.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_native_omitted_null_sizes_are_unlimited_and_idempotent(service):
    native = NativeArr(
        [
            native_definition(11, 3, "WEBDL-1080p", "web", 1080, 0, None, None),
            native_definition(12, 7, "Bluray-1080p", "bluray", 1080, 5, 140, None),
        ],
        omit_nullable_sizes=True,
    )
    async with httpx.AsyncClient(
        base_url=f"http://{service}", transport=httpx.MockTransport(native.request)
    ) as client:
        first = await reconcile_arr(service, settings(), client, "apply")
        second = await reconcile_arr(service, settings(), client, "apply")
        verified = await reconcile_arr(service, settings(), client, "verify")
    assert first.status == second.status == verified.status == "verified"
    assert not second.changes
    assert not verified.changes
    assert len(native.size_writes) == 2
    assert all(row["minSize"] == 20 and row["maxSize"] is None for row in native.size_writes)
    assert all("preferredSize" not in row for row in native.size_writes)


@pytest.mark.asyncio
async def test_allowed_native_webdl_and_remux_do_not_enable_webrip_in_quality_profile():
    native = NativeArr(
        [
            native_definition(11, 3, "WEBDL-1080p", "web", 1080, 20, None, 95),
            native_definition(12, 7, "Bluray-1080p", "bluray", 1080, 20, None, 95),
            native_definition(13, 31, "Bluray-2160p Remux", "blurayRaw", 2160, 50, None, 95),
            native_definition(14, 15, "WEBRip-1080p", "web", 1080, 0, None, 95),
            native_definition(15, 41, "WEB-Unknown", "web", 1080, 0, 125, 95),
        ]
    )
    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(native.request)
    ) as client:
        outcome = await reconcile_arr("sonarr", settings(), client, "apply")
    assert outcome.status == "verified"
    items = native.collections["qualityprofile"][0]["items"]
    assert {item["quality"]["id"] for item in items if item["allowed"]} == {3, 7, 31}
    assert not native.size_writes


@pytest.mark.asyncio
async def test_legacy_partial_environment_never_reads_or_writes_quality_sizes():
    native = NativeArr([])
    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(native.request)
    ) as client:
        outcome = await reconcile_arr("sonarr", {"HOMESERVER_SERIES_ROOT": ""}, client, "apply")
    assert outcome.status == "verified"
    assert not native.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_arr_import_guards_apply_without_overwriting_native_fields_and_are_idempotent(
    service,
):
    native = NativeArr([native_definition(11, 3, "WEBDL-1080p", "web", 1080, 20, None, 95)])
    native.config["downloadclient"]["enableCompletedDownloadHandling"] = True
    native.config["mediamanagement"]["copyUsingHardlinks"] = False
    original = deepcopy(native.config)
    async with httpx.AsyncClient(
        base_url=f"http://{service}", transport=httpx.MockTransport(native.request)
    ) as client:
        first = await reconcile_arr(service, settings(), client, "apply")
        second = await reconcile_arr(service, settings(), client, "apply")
        verified = await reconcile_arr(service, settings(), client, "verify")
    assert first.status == second.status == verified.status == "verified"
    assert not second.changes
    assert not verified.changes
    assert native.config["downloadclient"] == original["downloadclient"] | {
        "enableCompletedDownloadHandling": False
    }
    assert native.config["mediamanagement"] == original["mediamanagement"] | {
        "copyUsingHardlinks": True
    }
    assert len(native.config_writes) == 2
    assert all("/config/" in path for method, path in native.requests if method == "PUT")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, status", [("plan", "planned"), ("verify", "drift")])
async def test_arr_import_guard_drift_is_reported_without_writing(mode, status):
    native = NativeArr([native_definition(11, 3, "WEBDL-1080p", "web", 1080, 20, None, 95)])
    native.config["downloadclient"]["enableCompletedDownloadHandling"] = True
    native.config["mediamanagement"]["copyUsingHardlinks"] = False
    original = deepcopy(native.config)
    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(native.request)
    ) as client:
        result = await reconcile_arr("sonarr", settings(), client, mode)
    assert result.status == status
    assert native.config == original
    assert not native.config_writes
    changes = {change.key: change.after for change in result.changes}
    assert changes["enableCompletedDownloadHandling"] is False
    assert changes["copyUsingHardlinks"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resource, key",
    [
        ("downloadclient", "enableCompletedDownloadHandling"),
        ("mediamanagement", "copyUsingHardlinks"),
    ],
)
async def test_arr_import_guard_unpersisted_write_is_rejected(resource, key):
    native = NativeArr([native_definition(11, 3, "WEBDL-1080p", "web", 1080, 20, None, 95)])
    native.config[resource][key] = not native.config[resource][key]
    native.accept_config_writes = False
    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(native.request)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="read-back drift"):
            await reconcile_arr("sonarr", settings(), client, "apply")


@pytest.mark.asyncio
@pytest.mark.parametrize("malformation", ["container", "id", "missing", "boolean"])
async def test_arr_unknown_import_guard_schema_is_unsupported_before_guard_writes(malformation):
    native = NativeArr([native_definition(11, 3, "WEBDL-1080p", "web", 1080, 20, None, 95)])
    native.config["downloadclient"]["enableCompletedDownloadHandling"] = True
    if malformation == "container":
        native.config["mediamanagement"] = []
    elif malformation == "id":
        del native.config["mediamanagement"]["id"]
    elif malformation == "missing":
        del native.config["mediamanagement"]["copyUsingHardlinks"]
    else:
        native.config["mediamanagement"]["copyUsingHardlinks"] = "true"
    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(native.request)
    ) as client:
        result = await reconcile_arr("sonarr", settings(), client, "apply")
    assert result.status == "unsupported"
    assert not native.config_writes
