"""Named native profiles preserve media while replacing obsolete profile references."""

import json
from copy import deepcopy

import httpx
import pytest

from homeserver_control.configuration.arr import reconcile_arr
from homeserver_control.configuration.native_config import NativeConfigurationError


class ProfileServer:
    def __init__(self, service="radarr", *, legacy=True):
        self.service = service
        self.media_path = "/api/v3/movie" if service == "radarr" else "/api/v3/series"
        qualities = [
            (3, "WEBDL-1080p", 1080, "web"),
            (7, "Bluray-1080p", 1080, "bluray"),
            (30, "Remux-1080p", 1080, "blurayRaw"),
            (18, "WEBDL-2160p", 2160, "web"),
            (19, "Bluray-2160p", 2160, "bluray"),
            (31, "Remux-2160p", 2160, "blurayRaw"),
            (15, "WEBRip-1080p", 1080, "web"),
            (0, "Unknown", 0, "unknown"),
        ]
        leaves = [
            {
                "quality": {"id": i, "name": n, "resolution": r, "source": s},
                "allowed": True,
                "items": [],
            }
            for i, n, r, s in qualities
        ]
        leaves.append(
            {
                "quality": {
                    "id": 22,
                    "name": "BR-DISK",
                    "resolution": 1080,
                    "source": "bluray",
                    "modifier": "brdisk",
                },
                "allowed": True,
                "items": [],
            }
        )
        self.schema = {
            "name": "Any",
            "upgradeAllowed": True,
            "cutoff": 31,
            "items": [
                {"id": 1000, "name": "Grouped", "allowed": True, "items": list(reversed(leaves))}
            ],
            "formatItems": [],
            "minFormatScore": 0,
        }
        self.profiles = (
            [
                self.schema | {"id": 10, "name": "HomeServer"},
                self.schema | {"id": 11, "name": "HomeServer Animation"},
                self.schema | {"id": 12, "name": "HD-1080p"},
            ]
            if legacy
            else []
        )
        self.media = (
            [
                {
                    "id": 101,
                    "qualityProfileId": 10,
                    "monitored": False,
                    "path": "/media/fixture",
                    "tags": [4],
                    "movieFile": {"id": 31},
                },
                {
                    "id": 102,
                    "qualityProfileId": 11,
                    "monitored": True,
                    "seasons": [{"seasonNumber": 1, "monitored": False}],
                },
                {"id": 103, "qualityProfileId": 12, "monitored": True},
            ]
            if legacy
            else []
        )
        self.languages = [
            {"id": -1, "name": "Any"}, {"id": -2, "name": "Original"},
            {"id": 18, "name": "Portuguese"}, {"id": 30, "name": "Portuguese (Brazil)"},
        ]
        if service == "sonarr":
            self.languages[-1]["id"] = 33
        self.custom_formats = []
        self.writes = []
        self.accept_media_write = True
        self.interrupt_profile_write = False
        self.downloads = []
        self.collections = []
        self.importlists = []
        self.accept_collection_write = True

    def handle(self, request):
        path = request.url.path
        if request.method != "GET":
            self.writes.append((request.method, path))
        if path == "/api/v3/qualityprofile/schema":
            schema = deepcopy(self.schema)
            schema["formatItems"] += [
                {"format": row["id"], "name": row["name"], "score": 0}
                for row in self.custom_formats
            ]
            return httpx.Response(200, json=schema)
        if path == "/api/v3/language":
            return httpx.Response(200, json=self.languages)
        if path == "/api/v3/customformat/schema":
            return httpx.Response(200, json=[
                {"implementation": "LanguageSpecification", "fields": [
                    {"name": "value", "value": 1},
                    {"name": "exceptLanguage", "value": False},
                ]},
                {"implementation": "ReleaseTitleSpecification", "fields": [
                    {"name": "value", "value": ""},
                ]},
            ])
        if path.startswith("/api/v3/customformat"):
            if request.method == "GET":
                assert path == "/api/v3/customformat"
                return httpx.Response(200, json=self.custom_formats)
            payload = json.loads(request.content)
            for spec in payload["specifications"]:
                spec.update(implementationName="Native", infoLink="https://native.example")
                for field in spec["fields"]:
                    field.update(order=1, type="select", label="Native field label")
                spec["fields"] = list(reversed(spec["fields"]))
            payload["specifications"] = list(reversed(payload["specifications"]))
            if request.method == "POST":
                identifiers = [row["id"] for row in self.custom_formats]
                identifiers += [item["format"] for profile in self.profiles
                                for item in profile["formatItems"]]
                payload["id"] = max(identifiers, default=0) + 1
                self.custom_formats.append(payload)
                for profile in self.profiles:
                    profile["formatItems"] = profile["formatItems"] + [{
                        "format": payload["id"], "name": payload["name"], "score": 0,
                    }]
                return httpx.Response(201, json=payload)
            assert request.method == "PUT"
            identifier = int(path.rsplit("/", 1)[1])
            assert payload["id"] == identifier
            assert any(row["id"] == identifier for row in self.custom_formats)
            self.custom_formats[:] = [payload if row["id"] == identifier else row
                                      for row in self.custom_formats]
            return httpx.Response(200, json=payload)
        if path == "/api/v3/importlist":
            assert request.method == "GET"
            return httpx.Response(200, json=self.importlists)
        if path.startswith("/api/v3/collection"):
            assert self.service == "radarr"
            if path == "/api/v3/collection":
                assert request.method == "GET", "Bulk collection updates can trigger searches"
                return httpx.Response(200, json=self.collections)
            identifier = int(path.rsplit("/", 1)[1])
            row = next(row for row in self.collections if row["id"] == identifier)
            if request.method == "PUT" and self.accept_collection_write:
                row.update(json.loads(request.content))
            return httpx.Response(200, json=row)
        if path == "/api/v3/downloadclient/schema":
            return httpx.Response(
                200,
                json=[
                    {
                        "implementation": "QBittorrent",
                        "fields": [
                            {"name": key, "value": ""}
                            for key in (
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
        if path.startswith(self.media_path):
            if path == self.media_path:
                assert request.method == "GET"
                return httpx.Response(200, json=self.media)
            identifier = int(path.rsplit("/", 1)[1])
            row = next(row for row in self.media if row["id"] == identifier)
            if request.method == "PUT":
                payload = json.loads(request.content)
                assert not request.url.query
                if self.accept_media_write:
                    row.update(payload)
            return httpx.Response(200, json=row)
        for endpoint, rows in (
            ("qualityprofile", self.profiles),
            ("downloadclient", self.downloads),
        ):
            if not path.startswith("/api/v3/" + endpoint):
                continue
            if request.method == "POST":
                payload = json.loads(request.content)
                payload["id"] = max([r["id"] for r in rows], default=0) + 1
                rows.append(payload)
                if self.interrupt_profile_write and endpoint == "qualityprofile":
                    self.interrupt_profile_write = False
                    raise httpx.ReadTimeout("fixture", request=request)
            elif request.method == "PUT":
                payload = json.loads(request.content)
                rows[:] = [payload if row["id"] == payload["id"] else row for row in rows]
            elif request.method == "DELETE":
                identifier = int(path.rsplit("/", 1)[1])
                assert all(row["qualityProfileId"] != identifier for row in self.media)
                assert all(row["qualityProfileId"] != identifier for row in self.collections)
                assert all(row["qualityProfileId"] != identifier for row in self.importlists)
                rows[:] = [row for row in rows if row["id"] != identifier]
            return httpx.Response(200, json=rows)
        raise AssertionError((request.method, path))


def settings(**extra):
    return {
        "HOMESERVER_DOWNLOAD_DEFAULT_PROFILE": "Standard",
        "HOMESERVER_MOVIE_ROOT": "",
        "HOMESERVER_SERIES_ROOT": "",
    } | extra


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["radarr", "sonarr"])
async def test_named_profiles_order_nested_qualities_migrate_without_media_changes(service):
    native = ProfileServer(service)
    before = deepcopy(native.media)
    async with httpx.AsyncClient(
        base_url=f"http://{service}", transport=httpx.MockTransport(native.handle)
    ) as client:
        for mode, status in (("plan", "planned"), ("verify", "drift")):
            result = await reconcile_arr(service, settings(), client, mode)
            assert result.status == status
            assert not native.writes
        result = await reconcile_arr(service, settings(), client, "apply")
        assert result.status == "verified"
        again = await reconcile_arr(service, settings(), client, "apply")
        assert again.status == "verified"
        assert not again.changes
    profiles = {row["name"]: row for row in native.profiles}
    assert set(profiles) == {"Max Quality", "Standard", "Dublado"}
    if service == "radarr":
        assert len(native.custom_formats) == 4
    else:
        assert len(native.custom_formats) == 4
    for name, ids in (
        ("Max Quality", [3, 7, 30, 18, 19, 31]),
        ("Standard", [3, 7]),
        ("Dublado", [3, 7]),
    ):
        row = profiles[name]
        assert [leaf["quality"]["id"] for item in row["items"] if item["allowed"]
                for leaf in (item.get("items") or [item])] == ids
        assert row["cutoff"] == (ids[-1] if name == "Max Quality" else 1001)
        assert row["upgradeAllowed"] is False
        if service == "radarr":
            assert row["language"]["id"] == -1
        else:
            assert "language" not in row
    for previous, current, name in zip(
        before, native.media, ("Standard", "Dublado", "Standard"), strict=True
    ):
        assert current == previous | {"qualityProfileId": profiles[name]["id"]}
    last_media_write = max(
        i for i, (_, p) in enumerate(native.writes) if p.startswith(native.media_path)
    )
    assert all(i > last_media_write for i, (m, _) in enumerate(native.writes) if m == "DELETE")


@pytest.mark.asyncio
async def test_fresh_profile_creation_recovers_a_lost_response_without_duplicates():
    native = ProfileServer(legacy=False)
    native.interrupt_profile_write = True
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        assert (await reconcile_arr("radarr", settings(), client, "apply")).status == "verified"
        assert not (await reconcile_arr("radarr", settings(), client, "apply")).changes
    assert len(native.profiles) == 3


@pytest.mark.asyncio
async def test_migration_failed_readback_keeps_old_definitions():
    native = ProfileServer()
    native.accept_media_write = False
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="read-back"):
            await reconcile_arr("radarr", settings(), client, "apply")
    assert {"HomeServer", "HomeServer Animation", "HD-1080p"} <= {
        row["name"] for row in native.profiles
    }
    assert all(method != "DELETE" for method, _ in native.writes)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing", "duplicate", "language", "catalog"])
async def test_unsupported_profile_schema_prevents_profile_or_media_mutation(fault):
    native = ProfileServer()
    if fault == "missing":
        native.schema["items"][0]["items"].pop()
    elif fault == "duplicate":
        native.schema["items"][0]["items"].append(deepcopy(native.schema["items"][0]["items"][0]))
    elif fault == "language":
        native.languages = [{"id": -1, "name": "Any"}]
    else:
        native.media[0]["qualityProfileId"] = 999
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        result = await reconcile_arr("radarr", settings(), client, "apply")
        assert result.status == "unsupported"
    assert not native.writes


@pytest.mark.asyncio
async def test_arr_defers_legacy_definition_removal_until_seerr_references_are_verified():
    native = ProfileServer()
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        outcome = await reconcile_arr(
            "radarr", settings(HOMESERVER_SEERR_URL="http://seerr"), client, "apply"
        )
        assert outcome.status == "verified"
    assert len(native.profiles) == 6
    assert all(method != "DELETE" for method, _ in native.writes)


class SeerrProfiles:
    def __init__(self, native):
        self.native = native
        self.rows = [
            {
                "id": 73,
                "name": "HomeServer Radarr",
                "hostname": "radarr",
                "port": 7878,
                "baseUrl": "",
                "activeProfileId": 10,
                "activeProfileName": "HomeServer",
                "is4k": False,
                "isDefault": True,
                "manual": "keep",
                "overrideRule": [3],
            },
            {
                "id": 74,
                "name": "4K Instance",
                "hostname": "radarr",
                "port": 7878,
                "baseUrl": "",
                "is4k": True,
                "isDefault": True,
                "activeProfileId": 11,
                "activeProfileName": "HomeServer Animation",
            },
        ]
        self.writes = []
        self.accept_write = True
        self.rules = []
        self.requests = []
        if native.service == "sonarr":
            for row in self.rows:
                row.update(hostname="sonarr", port=8989)
            self.rows[0]["name"] = "HomeServer Sonarr"

    def handle(self, request):
        if request.url.host == self.native.service:
            return self.native.handle(request)
        if request.url.path == "/api/v1/settings/public":
            return httpx.Response(200, json={"initialized": True})
        if request.url.path == "/api/v1/overrideRule":
            assert request.method == "GET"
            return httpx.Response(200, json=self.rules)
        if request.url.path == "/api/v1/request":
            assert request.method == "GET"
            skip = int(request.url.params.get("skip", 0))
            return httpx.Response(
                200,
                json={
                    "pageInfo": {"results": len(self.requests)},
                    "results": self.requests[skip : skip + 100],
                },
            )
        if request.url.path == "/api/v1/settings/jellyfin":
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "ip": "integration-proxy",
                    "port": 8787,
                    "urlBase": "/jellyfin",
                    "useSsl": False,
                    "libraries": [{"id": "1", "name": "Movies", "enabled": True}],
                },
            )
        if request.url.path.startswith(f"/api/v1/settings/{self.native.service}"):
            if request.method == "POST":
                payload = json.loads(request.content)
                identifier = max([row["id"] for row in self.rows], default=-1) + 1
                self.writes.append(identifier)
                self.rows.append(payload | {"id": identifier})
            elif request.method == "PUT":
                payload = json.loads(request.content)
                assert "id" not in payload
                identifier = int(request.url.path.rsplit("/", 1)[1])
                self.writes.append(identifier)
                if self.accept_write:
                    self.rows[:] = [
                        payload | {"id": identifier} if row["id"] == identifier else row
                        for row in self.rows
                    ]
            return httpx.Response(200, json=self.rows)
        raise AssertionError((request.method, request.url.path))


@pytest.mark.asyncio
async def test_seerr_migrates_all_instance_references_before_pruning_and_honors_default():
    from homeserver_control.configuration.media_servers import reconcile_seerr

    native = ProfileServer()
    seerr = SeerrProfiles(native)
    env = settings(
        HOMESERVER_DOWNLOAD_DEFAULT_PROFILE="Max Quality",
        HOMESERVER_RADARR_URL="http://radarr:7878",
        HOMESERVER_RADARR_API_KEY="fixture",
        HOMESERVER_SEERR_URL="http://seerr",
    )
    transport = httpx.MockTransport(seerr.handle)
    async with httpx.AsyncClient(base_url="http://radarr", transport=transport) as arr:
        await reconcile_arr("radarr", env, arr, "apply")
    async with httpx.AsyncClient(base_url="http://seerr", transport=transport) as client:
        outcome = await reconcile_seerr(env, client, "plan", transport=transport)
        assert outcome.status == "planned"
        assert not seerr.writes
        outcome = await reconcile_seerr(env, client, "apply", transport=transport)
        assert outcome.status == "verified"
        assert not (await reconcile_seerr(env, client, "apply", transport=transport)).changes
    profiles = {row["name"]: row for row in native.profiles}
    assert set(profiles) == {"Max Quality", "Standard", "Dublado"}
    assert seerr.rows[0]["activeProfileId"] == profiles["Max Quality"]["id"]
    assert seerr.rows[0]["activeProfileName"] == "Max Quality"
    assert seerr.rows[0]["manual"] == "keep"
    assert seerr.rows[0]["overrideRule"] == [3]
    assert seerr.rows[0]["preventSearch"] is True
    assert seerr.rows[1]["activeProfileId"] == profiles["Dublado"]["id"]
    assert seerr.rows[1]["is4k"] is True
    assert [row['id'] for row in seerr.rows] == [73, 74]
    assert all((row['hostname'], row['port'], row['baseUrl']) == (
        'integration-proxy', 8787, '/radarr',
    ) for row in seerr.rows)


@pytest.mark.asyncio
async def test_seerr_failed_readback_prevents_obsolete_profile_removal():
    from homeserver_control.configuration.media_servers import reconcile_seerr

    native = ProfileServer()
    seerr = SeerrProfiles(native)
    env = settings(
        HOMESERVER_RADARR_URL="http://radarr:7878",
        HOMESERVER_RADARR_API_KEY="fixture",
        HOMESERVER_SEERR_URL="http://seerr",
    )
    transport = httpx.MockTransport(seerr.handle)
    async with httpx.AsyncClient(base_url="http://radarr", transport=transport) as arr:
        await reconcile_arr("radarr", env, arr, "apply")
    seerr.accept_write = False
    async with httpx.AsyncClient(base_url="http://seerr", transport=transport) as client:
        with pytest.raises(NativeConfigurationError, match="read-back"):
            await reconcile_seerr(env, client, "apply", transport=transport)
    assert len(native.profiles) == 6
    assert all(method != "DELETE" for method, _ in native.writes)


@pytest.mark.asyncio
async def test_existing_named_profile_selection_and_custom_format_constraints_survive():
    native = ProfileServer()
    native.profiles[0]["minFormatScore"] = 100
    native.profiles[0]["formatItems"] = [{"format": 99, "score": -100}]
    native.profiles.append(
        deepcopy(native.schema)
        | {
            "id": 25,
            "name": "Max Quality",
            "formatItems": [{"format": 6, "score": 12}],
            "minFormatScore": 5,
            "manual": "preserved",
        }
    )
    native.media.append({"id": 104, "qualityProfileId": 25, "monitored": False})
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        await reconcile_arr("radarr", settings(), client, "apply")
    maximum = next(row for row in native.profiles if row["name"] == "Max Quality")
    slots = [{"format": row["id"], "name": row["name"], "score": score}
             for row, score in zip(native.custom_formats, (100, 20, -10, 10), strict=True)]
    assert maximum["id"] == 25
    assert maximum["formatItems"] == [{"format": 6, "score": 12}, *slots]
    assert maximum["minFormatScore"] == 10
    assert maximum["manual"] == "preserved"
    standard = next(row for row in native.profiles if row["name"] == "Standard")
    assert standard["minFormatScore"] == 10
    assert standard["formatItems"] == slots
    dubbed = next(row for row in native.profiles if row["name"] == "Dublado")
    assert dubbed["formatItems"] == [
        slot | {"score": score}
        for slot, score in zip(slots, (100, 10, 990, 1000), strict=True)
    ]
    assert dubbed["minFormatScore"] == 10
    assert native.media[-1] == {"id": 104, "qualityProfileId": 25, "monitored": False}
    assert ("PUT", "/api/v3/movie/104") not in native.writes


@pytest.mark.asyncio
async def test_seerr_adopts_existing_id_zero_without_creating_duplicate_connection():
    from homeserver_control.configuration.media_servers import reconcile_seerr

    native = ProfileServer()
    seerr = SeerrProfiles(native)
    seerr.rows = [seerr.rows[0] | {"id": 0, "name": "My Radarr"}]
    env = settings(
        HOMESERVER_RADARR_URL="http://radarr:7878",
        HOMESERVER_RADARR_API_KEY="fixture",
        HOMESERVER_SEERR_URL="http://seerr",
    )
    transport = httpx.MockTransport(seerr.handle)
    async with httpx.AsyncClient(base_url="http://radarr", transport=transport) as arr:
        await reconcile_arr("radarr", env, arr, "apply")
    async with httpx.AsyncClient(base_url="http://seerr", transport=transport) as client:
        assert (
            await reconcile_seerr(env, client, "apply", transport=transport)
        ).status == "verified"
    assert len(seerr.rows) == 1
    assert seerr.rows[0]["id"] == 0
    assert seerr.rows[0]["activeProfileName"] == "Standard"


@pytest.mark.asyncio
async def test_seerr_unknown_profile_reference_blocks_deletion():
    from homeserver_control.configuration.media_servers import reconcile_seerr

    native = ProfileServer()
    seerr = SeerrProfiles(native)
    seerr.rows[1]["activeProfileId"] = 9876
    env = settings(
        HOMESERVER_RADARR_URL="http://radarr:7878",
        HOMESERVER_RADARR_API_KEY="fixture",
        HOMESERVER_SEERR_URL="http://seerr",
    )
    transport = httpx.MockTransport(seerr.handle)
    async with httpx.AsyncClient(base_url="http://radarr", transport=transport) as arr:
        await reconcile_arr("radarr", env, arr, "apply")
    async with httpx.AsyncClient(base_url="http://seerr", transport=transport) as client:
        with pytest.raises(NativeConfigurationError, match="reference"):
            await reconcile_seerr(env, client, "apply", transport=transport)
    assert len(native.profiles) == 6
    assert not seerr.writes


@pytest.mark.asyncio
async def test_named_sonarr_size_floors_include_uhd_despite_legacy_fhd_setting():
    from homeserver_control.configuration.arr import reconcile_quality_sizes

    rows = [
        {
            "id": 1,
            "quality": {"id": 18, "name": "WEBDL-2160p", "resolution": 2160, "source": "web"},
            "minSize": 0,
            "maxSize": 100,
            "preferredSize": 80,
        }
    ]

    def handle(request):
        if request.method == "PUT":
            rows[0] = json.loads(request.content)
        return httpx.Response(200, json=rows)

    async with httpx.AsyncClient(
        base_url="http://sonarr", transport=httpx.MockTransport(handle)
    ) as client:
        await reconcile_quality_sizes(
            "sonarr",
            settings(
                HOMESERVER_SERIES_RESOLUTIONS="1080",
                HOMESERVER_MEDIA_SOURCES="bluray",
                HOMESERVER_QUALITY_MIN_MIB_PER_MIN_2160="50",
            ),
            client,
            "apply",
        )
    assert rows[0]["minSize"] == 50
    assert rows[0]["maxSize"] is None


@pytest.mark.asyncio
async def test_fresh_seerr_uses_standard_and_is_idempotent():
    from homeserver_control.configuration.media_servers import reconcile_seerr

    native = ProfileServer(legacy=False)
    seerr = SeerrProfiles(native)
    seerr.rows = []
    env = settings(
        HOMESERVER_RADARR_URL="http://radarr:7878",
        HOMESERVER_RADARR_API_KEY="fixture",
        HOMESERVER_SEERR_URL="http://seerr",
    )
    transport = httpx.MockTransport(seerr.handle)
    async with httpx.AsyncClient(base_url="http://radarr", transport=transport) as arr:
        await reconcile_arr("radarr", env, arr, "apply")
    async with httpx.AsyncClient(base_url="http://seerr", transport=transport) as client:
        assert (
            await reconcile_seerr(env, client, "apply", transport=transport)
        ).status == "verified"
        assert not (await reconcile_seerr(env, client, "apply", transport=transport)).changes
    assert len(seerr.rows) == 1
    assert seerr.rows[0]["id"] == 0
    assert seerr.rows[0]["activeProfileName"] == "Standard"


@pytest.mark.asyncio
async def test_sonarr_anime_default_follows_default_and_preserves_other_named_selection():
    from homeserver_control.configuration.media_servers import reconcile_seerr

    native = ProfileServer("sonarr")
    seerr = SeerrProfiles(native)
    seerr.rows[0].update(activeAnimeProfileId=11, activeAnimeProfileName="HomeServer Animation")
    env = settings(
        HOMESERVER_SONARR_URL="http://sonarr:8989",
        HOMESERVER_SONARR_API_KEY="fixture",
        HOMESERVER_SEERR_URL="http://seerr",
    )
    transport = httpx.MockTransport(seerr.handle)
    async with httpx.AsyncClient(base_url="http://sonarr", transport=transport) as arr:
        await reconcile_arr("sonarr", env, arr, "apply")
    maximum = next(row for row in native.profiles if row["name"] == "Max Quality")
    seerr.rows[1].update(activeProfileId=maximum["id"], activeProfileName=maximum["name"])
    async with httpx.AsyncClient(base_url="http://seerr", transport=transport) as client:
        await reconcile_seerr(env, client, "apply", transport=transport)
    assert seerr.rows[0]["activeAnimeProfileId"] == seerr.rows[0]["activeProfileId"]
    assert seerr.rows[0]["activeAnimeProfileName"] == "Standard"
    assert seerr.rows[1]["activeProfileId"] == maximum["id"]
    assert len(native.profiles) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["request", "rule"])
async def test_stored_seerr_overrides_block_pruning_without_changing_request_state(kind):
    from homeserver_control.configuration.media_servers import reconcile_seerr

    native = ProfileServer()
    seerr = SeerrProfiles(native)
    if kind == "request":
        seerr.requests = [
            {"id": 17, "type": "movie", "profileId": 10, "serverId": 73, "status": 1, "is4k": False}
        ]
    else:
        seerr.rules = [{"id": 3, "profileId": 10, "radarrServiceId": 73, "sonarrServiceId": None}]
    env = settings(
        HOMESERVER_RADARR_URL="http://radarr:7878",
        HOMESERVER_RADARR_API_KEY="fixture",
        HOMESERVER_SEERR_URL="http://seerr",
    )
    transport = httpx.MockTransport(seerr.handle)
    async with httpx.AsyncClient(base_url="http://radarr", transport=transport) as arr:
        await reconcile_arr("radarr", env, arr, "apply")
    async with httpx.AsyncClient(base_url="http://seerr", transport=transport) as client:
        with pytest.raises(NativeConfigurationError, match="stored profile"):
            await reconcile_seerr(env, client, "apply", transport=transport)
    assert len(native.profiles) == 6
    assert all(method != "DELETE" for method, _ in native.writes)
    if kind == "request":
        assert seerr.requests[0]["status"] == 1
        assert seerr.requests[0]["profileId"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["qualityCutoffNotMet", "id", "relativePath", "size"])
async def test_radarr_profile_readback_allows_only_derived_cutoff_flag(changed_field):
    native = ProfileServer()
    native.media[0]["movieFile"] = {
        "id": 31,
        "relativePath": "Fixture/film.mkv",
        "size": 1000000,
        "qualityCutoffNotMet": False,
    }
    before = deepcopy(native.media[0])

    def handle(request):
        response = native.handle(request)
        if request.method == "PUT" and request.url.path == "/api/v3/movie/101":
            native.media[0]["movieFile"][changed_field] = {
                "qualityCutoffNotMet": True,
                "id": 999,
                "relativePath": "Other/film.mkv",
                "size": 42,
            }[changed_field]
        return response

    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(handle)
    ) as client:
        if changed_field == "qualityCutoffNotMet":
            result = await reconcile_arr("radarr", settings(), client, "apply")
            assert result.status == "verified"
            standard = next(row for row in native.profiles if row["name"] == "Standard")
            assert native.media[0] == before | {
                "qualityProfileId": standard["id"],
                "movieFile": before["movieFile"] | {"qualityCutoffNotMet": True},
            }
            assert not (await reconcile_arr("radarr", settings(), client, "apply")).changes
        else:
            with pytest.raises(NativeConfigurationError, match="read-back"):
                await reconcile_arr("radarr", settings(), client, "apply")
            assert all(method != "DELETE" for method, _ in native.writes)


def collection_fixture(profile_id=10):
    return {
        "id": 51,
        "title": "Fixture collection",
        "sortTitle": "fixture collection",
        "tmdbId": 400,
        "images": [{"coverType": "poster", "url": "/fixture.jpg"}],
        "overview": "Fixture description",
        "monitored": False,
        "rootFolderPath": "/data/media/movies",
        "qualityProfileId": profile_id,
        "searchOnAdd": False,
        "minimumAvailability": "released",
        "tags": [4],
        "movies": [{"tmdbId": 100, "title": "Fixture movie", "isExisting": True}],
        "missingMovies": 0,
    }


@pytest.mark.asyncio
async def test_radarr_collections_migrate_before_profile_removal_preserving_all_fields():
    native = ProfileServer()
    native.collections = [collection_fixture(), collection_fixture(11) | {"id": 52}]
    before = deepcopy(native.collections)
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        assert (await reconcile_arr("radarr", settings(), client, "plan")).status == "planned"
        assert not native.writes
        assert (await reconcile_arr("radarr", settings(), client, "apply")).status == "verified"
        assert not (await reconcile_arr("radarr", settings(), client, "apply")).changes
    ids = {row["name"]: row["id"] for row in native.profiles}
    assert native.collections == [
        before[0] | {"qualityProfileId": ids["Standard"]},
        before[1] | {"qualityProfileId": ids["Dublado"]},
    ]
    collection_writes = [
        index
        for index, (method, path) in enumerate(native.writes)
        if method == "PUT" and path.startswith("/api/v3/collection/")
    ]
    deletion_indices = [
        index for index, (method, _) in enumerate(native.writes) if method == "DELETE"
    ]
    assert len(collection_writes) == 2
    assert max(collection_writes) < min(deletion_indices)


@pytest.mark.asyncio
async def test_collection_failed_readback_preserves_legacy_definitions():
    native = ProfileServer()
    native.collections = [collection_fixture()]
    native.accept_collection_write = False
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="read-back"):
            await reconcile_arr("radarr", settings(), client, "apply")
    assert all(method != "DELETE" for method, _ in native.writes)


@pytest.mark.asyncio
async def test_legacy_importlist_reference_fails_closed_before_native_mutations():
    native = ProfileServer()
    native.importlists = [
        {
            "id": 81,
            "name": "Fixture import list",
            "qualityProfileId": 10,
            "enableAuto": True,
            "searchOnAdd": True,
            "fields": [{"name": "apiKey", "value": "fixture-private"}],
        }
    ]
    before = deepcopy(native.importlists)
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        assert (await reconcile_arr("radarr", settings(), client, "apply")).status == "unsupported"
    assert not native.writes
    assert native.importlists == before


@pytest.mark.asyncio
async def test_pruning_detects_new_collection_reference_without_deleting_any_definition():
    from homeserver_control.configuration.quality_profiles import prune_native_quality_profiles

    native = ProfileServer()
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        await reconcile_arr(
            "radarr", settings(HOMESERVER_SEERR_URL="http://seerr"), client, "apply"
        )
        native.collections = [collection_fixture()]
        with pytest.raises(NativeConfigurationError, match="in use"):
            await prune_native_quality_profiles("radarr", client, "apply")
    assert all(method != "DELETE" for method, _ in native.writes)


@pytest.mark.asyncio
@pytest.mark.parametrize("service,brazilian_id", [("radarr", 30), ("sonarr", 33)])
async def test_native_ties_use_grouped_quality_and_original_required_custom_formats(
    service, brazilian_id,
):
    native = ProfileServer(service)
    async with httpx.AsyncClient(
        base_url=f"http://{service}", transport=httpx.MockTransport(native.handle)
    ) as client:
        result = await reconcile_arr(service, settings(), client, "apply")
        assert result.status == "verified"
        assert not (await reconcile_arr(service, settings(), client, "apply")).changes
    managed = {row["name"]: row for row in native.custom_formats}
    assert {
        "HomeServer BluRay", "HomeServer Original", "HomeServer Dual PTBR Original",
    } <= managed.keys()
    for name, row in managed.items():
        if name not in {"HomeServer Original", "HomeServer Dual PTBR Original"}:
            continue
        specs = row["specifications"]
        assert any(spec["implementation"] == "LanguageSpecification"
                   and spec["required"] and not spec["negate"]
                   and next(field["value"] for field in spec["fields"]
                            if field["name"] == "value") == -2 for spec in specs)
    dual = managed["HomeServer Dual PTBR Original"]
    assert any(spec["required"] and not spec["negate"]
               and next(field["value"] for field in spec["fields"]
                        if field["name"] == "value") == brazilian_id
               for spec in dual["specifications"])
    for profile in native.profiles:
        assert profile["minFormatScore"] >= 10
        groups = [item for item in profile["items"] if item.get("items") and item["allowed"]]
        expected = [{3, 7}, {18, 19}] if profile["name"] == "Max Quality" else [{3, 7}]
        assert [{item["quality"]["id"] for item in group["items"]} for group in groups] == expected
        if profile["name"] != "Max Quality":
            assert profile["cutoff"] == groups[-1]["id"]
        scores = {item["format"]: item["score"] for item in profile["formatItems"]}
        assert scores[managed["HomeServer BluRay"]["id"]] == 100
        assert scores[managed["HomeServer Original"]["id"]] >= 10
        assert scores[managed["HomeServer Dual PTBR Original"]["id"]] == (
            990 if profile["name"] == "Dublado" else -10
        )


@pytest.mark.asyncio
async def test_ambiguous_managed_formats_fail_before_any_native_mutation():
    native = ProfileServer()
    native.custom_formats = [
        {"id": 40, "name": "HomeServer Original", "specifications": []},
        {"id": 41, "name": "HomeServer Original", "specifications": []},
    ]
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="Ambiguous"):
            await reconcile_arr("radarr", settings(), client, "apply")
    assert not native.writes


@pytest.mark.asyncio
async def test_missing_native_format_schema_fails_closed_before_profile_updates():
    native = ProfileServer()

    def handle(request):
        if request.url.path == "/api/v3/customformat/schema":
            return httpx.Response(200, json=[])
        return native.handle(request)

    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(handle)
    ) as client:
        assert (await reconcile_arr("radarr", settings(), client, "apply")).status == "unsupported"
    assert not native.writes


@pytest.mark.asyncio
async def test_native_managed_format_readback_rejects_drift_before_profile_updates():
    native = ProfileServer()

    def handle(request):
        response = native.handle(request)
        if request.method == "POST" and request.url.path == "/api/v3/customformat":
            native.custom_formats[-1]["specifications"][0]["required"] = False
        return response

    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(handle)
    ) as client:
        with pytest.raises(NativeConfigurationError, match="read-back drift"):
            await reconcile_arr("radarr", settings(), client, "apply")
    assert native.writes == [("POST", "/api/v3/customformat")]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_name,bridge_score", [
    ("Max Quality", 10), ("Standard", 10), ("Dublado", 1000),
])
async def test_asc_declared_dual_survives_native_parser_missing_original(
    profile_name, bridge_score,
):
    import re

    native = ProfileServer()
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(native.handle)
    ) as client:
        assert (await reconcile_arr("radarr", settings(), client, "apply")).status == "verified"
    profile = next(row for row in native.profiles if row["name"] == profile_name)
    assert profile["language"] == {"id": -1, "name": "Any"}
    bridge = next(row for row in native.custom_formats
                  if row["name"] == "HomeServer ASC Dual Original")
    specs = bridge["specifications"]
    title_spec = next(spec for spec in specs
                      if spec["implementation"] == "ReleaseTitleSpecification")
    pattern = next(field["value"] for field in title_spec["fields"] if field["name"] == "value")
    title = "Fixture.2025.1080p.WEB-DL.Dual-Audio Brazilian Original"
    assert re.search(pattern, title)
    for rejected in ("Fixture Dublado Brazilian", "Fixture Dual-Audio", "Fixture Original"):
        assert not re.search(pattern, rejected)
    original_spec = next(spec for spec in specs
                         if spec["implementation"] == "LanguageSpecification")
    assert original_spec["required"] and original_spec["negate"]
    assert next(field["value"] for field in original_spec["fields"]
                if field["name"] == "value") == -2
    # Real ASC parser shape: original=en, parsed languages={pt-br}. Native
    # Original does not match; negated Original makes only the bridge applicable.
    slots = {item["format"]: item["score"] for item in profile["formatItems"]}
    assert slots[bridge["id"]] == bridge_score >= profile["minFormatScore"]
    bluray = next(row for row in native.custom_formats if row["name"] == "HomeServer BluRay")
    assert all(spec["implementation"] != "LanguageSpecification"
               for spec in bluray["specifications"])
