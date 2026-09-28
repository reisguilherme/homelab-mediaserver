import httpx
import pytest

from homeserver_control.configuration.arr import collection, reconcile_arr
from homeserver_control.configuration.bazarr import reconcile_bazarr
from homeserver_control.configuration.media_servers import (
    bootstrap_jellyfin,
    bootstrap_seerr,
    reconcile_media_server,
)
from homeserver_control.configuration.prowlarr import reconcile_prowlarr
from homeserver_control.configuration.reconcile import (
    apply_native_settings,
    discover_native_credentials,
    verify_native_settings,
)


@pytest.mark.asyncio
async def test_masked_provider_credentials_require_private_evidence_and_stored_connection_test(
    tmp_path,
):
    import json

    stored = "older-valid-fixture"
    row = {
        "id": 14,
        "name": "Managed",
        "manual": True,
        "fields": [{"name": "apiKey", "value": "********"}],
    }
    writes = []
    tests = []

    def handler(request):
        nonlocal stored
        if request.url.path.endswith("/test"):
            payload = json.loads(request.content)
            assert payload["id"] == 14
            assert payload["fields"][0]["value"] == "********"
            tests.append(stored)
            return httpx.Response(200, json={})
        if request.method == "PUT":
            payload = json.loads(request.content)
            assert payload["manual"] is True
            stored = payload["fields"][0]["value"]
            writes.append(stored)
        return httpx.Response(200, json=[row])

    env = {"HOMESERVER_APPDATA_ROOT": str(tmp_path)}
    transport = httpx.MockTransport(handler)
    desired = {"name": "Managed", "fields": [{"name": "apiKey", "value": "first-valid-fixture"}]}
    async with httpx.AsyncClient(base_url="http://prowlarr", transport=transport) as client:
        assert await collection(
            "prowlarr", client, "/api/v1/applications", desired, "name", "verify", settings=env
        )
        assert not list(tmp_path.iterdir())
        assert not writes
        assert await collection(
            "prowlarr", client, "/api/v1/applications", desired, "name", "apply", settings=env
        )
    path = tmp_path / "native-config-fingerprints.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert "first-valid-fixture" not in path.read_text()
    # New client/ledger models reboot. Both old/new tokens valid, so HMAC detects rotation.
    async with httpx.AsyncClient(base_url="http://prowlarr", transport=transport) as client:
        assert not await collection(
            "prowlarr", client, "/api/v1/applications", desired, "name", "apply", settings=env
        )
        desired["fields"][0]["value"] = "rotated-valid-fixture"
        assert await collection(
            "prowlarr", client, "/api/v1/applications", desired, "name", "apply", settings=env
        )
        assert not await collection(
            "prowlarr", client, "/api/v1/applications", desired, "name", "verify", settings=env
        )
    assert writes == ["first-valid-fixture", "rotated-valid-fixture"]
    assert tests[-1] == "rotated-valid-fixture"


@pytest.mark.asyncio
async def test_masked_credential_lost_ack_never_creates_false_evidence(tmp_path):
    row = {"id": 1, "name": "Managed", "fields": [{"name": "apiKey", "value": "********"}]}

    def handler(request):
        if request.method == "PUT":
            raise httpx.ReadTimeout("lost acknowledgement", request=request)
        return httpx.Response(200, json=[row])

    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(RuntimeError, match="read-back drift"):
            await collection(
                "prowlarr",
                client,
                "/api/v1/applications",
                {"name": "Managed", "fields": [{"name": "apiKey", "value": "new-fixture"}]},
                "name",
                "apply",
                settings={"HOMESERVER_APPDATA_ROOT": str(tmp_path)},
            )
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [True, False])
async def test_seerr_273_legacy_library_route_reads_without_disabling_and_preserves_ids(fresh):
    libraries = (
        []
        if fresh
        else [
            {"id": "manual-id", "name": "Manual", "enabled": True},
            {"id": "movie-id", "name": "Movies", "enabled": False},
            {"id": "tv-id", "name": "TV", "enabled": False},
        ]
    )
    mutations = []

    def handler(request):
        nonlocal libraries
        path = request.url.path
        if path.endswith("/public"):
            return httpx.Response(200, json={"initialized": True})
        if path.endswith("/jellyfin"):
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "ip": "jellyfin",
                    "port": 8096,
                    "urlBase": "",
                    "useSsl": False,
                    "libraries": libraries,
                },
            )
        if path.endswith("/library") and request.method == "GET":
            # Only an initial sync of zero existing libraries can omit enable safely.
            assert "enable" in request.url.params or (
                not libraries and request.url.params.get("sync") == "true"
            )
            mutations.append(dict(request.url.params))
            if request.url.params.get("sync") == "true":
                libraries = [
                    {"id": "movie-id", "name": "Movies", "enabled": False},
                    {"id": "tv-id", "name": "TV", "enabled": False},
                ]
            if "enable" in request.url.params and not request.url.params["enable"]:
                return httpx.Response(400, json={"message": "Empty enable parameter"})
            enabled = request.url.params.get("enable", "").split(",")
            for library in libraries:
                library["enabled"] = library["id"] in enabled
            return httpx.Response(200, json=libraries)
        return httpx.Response(404)

    async with httpx.AsyncClient(
        base_url="http://seerr", transport=httpx.MockTransport(handler)
    ) as client:
        assert (await reconcile_media_server("seerr", {}, client, "plan")).status == "planned"
        assert (await reconcile_media_server("seerr", {}, client, "verify")).status == "drift"
        assert not mutations
        assert (await reconcile_media_server("seerr", {}, client, "apply")).status == "verified"
        count = len(mutations)
        assert not (await reconcile_media_server("seerr", {}, client, "apply")).changes
        assert (await reconcile_media_server("seerr", {}, client, "verify")).status == "verified"
        assert len(mutations) == count
    assert {library["id"] for library in libraries} >= {"movie-id", "tv-id"}
    assert all(library["enabled"] for library in libraries)
    if not fresh:
        assert "manual-id" in mutations[-1]["enable"].split(",")


@pytest.mark.asyncio
async def test_seerr_empty_library_readback_never_reports_verified_or_initializes():
    def handler(request):
        assert not request.url.path.endswith("/initialize")
        if "/library" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json={"ip": "jellyfin", "port": 8096, "urlBase": "", "useSsl": False, "libraries": []},
        )

    async with httpx.AsyncClient(
        base_url="http://seerr", transport=httpx.MockTransport(handler)
    ) as client:
        for mode, expected in [("plan", "planned"), ("verify", "drift"), ("apply", "drift")]:
            outcome = await reconcile_media_server("seerr", {}, client, mode)
            assert outcome.status == expected
            assert outcome.changes


@pytest.mark.asyncio
async def test_seerr_interrupted_bootstrap_authenticates_existing_connection_without_hostname():
    import json

    writes = []

    def handler(request):
        if request.url.path.endswith("/public"):
            return httpx.Response(200, json={"initialized": False})
        body = json.loads(request.content)
        assert "hostname" not in body
        writes.append(body)
        return httpx.Response(200, json={"id": 1})

    async with httpx.AsyncClient(
        base_url="http://seerr", transport=httpx.MockTransport(handler)
    ) as client:
        assert (
            await bootstrap_seerr({"HOMESERVER_ADMIN_PASSWORD": "fixture"}, client, "apply") is None
        )
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_bazarr_ui_bootstrap_hash_readback_and_existing_credentials_preserved():
    import hashlib
    from urllib.parse import parse_qs

    from homeserver_control.configuration.bazarr import bootstrap_bazarr_account

    state = {"general": {}, "auth": {"username": "", "password": "", "type": None}}
    writes = []

    def handler(request):
        if request.method == "POST":
            payload = parse_qs(request.content.decode())
            writes.append(payload)
            state["auth"] = {
                "username": payload["settings-auth-username"][0],
                "password": hashlib.md5(payload["settings-auth-password"][0].encode()).hexdigest(),
                "type": "form",
            }
            raise httpx.ReadError("lost response", request=request)
        return httpx.Response(200, json=state)

    env = {
        "HOMESERVER_ADMIN_USERNAME": "fixture-root",
        "HOMESERVER_ADMIN_PASSWORD": "fixture-private",
    }
    async with httpx.AsyncClient(
        base_url="http://bazarr", transport=httpx.MockTransport(handler)
    ) as client:
        assert await bootstrap_bazarr_account(env, client, "plan")
        assert not writes
        assert "fixture-private" not in repr(await bootstrap_bazarr_account(env, client, "apply"))
        assert not await bootstrap_bazarr_account(
            env | {"HOMESERVER_ADMIN_PASSWORD": "new-unapplied"}, client, "apply"
        )
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_jellyfin_encoding_threads_merge_readback_and_capability():
    import json

    encoding = {"EncodingThreadCount": -1, "EnableHardwareEncoding": False}
    writes = []

    def handler(request):
        if request.url.path == "/Users":
            return httpx.Response(200, json=[{"Name": "admin", "Id": "stable-id"}])
        if request.url.path == "/System/Configuration/encoding":
            if request.method == "POST":
                writes.append(json.loads(request.content))
                encoding.update(writes[-1])
                return httpx.Response(204)
            return httpx.Response(200, json=encoding)
        return httpx.Response(200, json=[{"Locations": ["/data/media/movies", "/data/media/tv"]}])

    env = {"HOMESERVER_TRANSCODE_THREADS": "3"}
    async with httpx.AsyncClient(
        base_url="http://jellyfin", transport=httpx.MockTransport(handler)
    ) as client:
        assert (await reconcile_media_server("jellyfin", env, client, "verify")).status == "drift"
        assert (await reconcile_media_server("jellyfin", env, client, "apply")).changes
        assert not (await reconcile_media_server("jellyfin", env, client, "apply")).changes
        verified = await reconcile_media_server("jellyfin", env, client, "verify")
        assert verified.status == "verified"
        encoding.pop("EncodingThreadCount")
        unsupported = await reconcile_media_server("jellyfin", env, client, "apply")
        assert unsupported.status == "unsupported"
    assert writes == [{"EncodingThreadCount": 3, "EnableHardwareEncoding": False}]


@pytest.mark.asyncio
async def test_jellyfin_delete_policy_preserves_foreign_permissions_and_user_ids():
    import json

    users = [
        {
            "Id": "root-id",
            "Name": "root",
            "Policy": {
                "IsAdministrator": True,
                "EnableContentDeletion": False,
                "EnableContentDeletionFromFolders": ["existing-folder"],
            },
        }
    ]
    libraries = [{"Locations": ["/data/media/movies", "/data/media/tv"]}]
    writes = []

    def handler(request):
        if request.url.path == "/Users":
            return httpx.Response(200, json=users)
        if request.url.path == "/Users/root-id/Policy":
            writes.append(json.loads(request.content))
            users[0]["Policy"] = writes[-1]
            return httpx.Response(204)
        return httpx.Response(200, json=libraries)

    async with httpx.AsyncClient(
        base_url="http://jellyfin", transport=httpx.MockTransport(handler)
    ) as client:
        env = {"HOMESERVER_ADMIN_USERNAME": "root"}
        assert not (await reconcile_media_server("jellyfin", env, client, "apply")).changes
        env["HOMESERVER_JELLYFIN_ENABLE_MEDIA_DELETION"] = "true"
        assert (await reconcile_media_server("jellyfin", env, client, "verify")).status == "drift"
        assert (await reconcile_media_server("jellyfin", env, client, "apply")).status == "verified"
        assert not (await reconcile_media_server("jellyfin", env, client, "apply")).changes
    assert len(writes) == 1
    assert writes[0]["IsAdministrator"] is True
    assert writes[0]["EnableContentDeletionFromFolders"] == ["existing-folder"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service,version", [("radarr", "v3"), ("sonarr", "v3"), ("prowlarr", "v1")]
)
async def test_native_host_bootstrap_once_preserves_existing_account_and_secrets(service, version):
    import json

    from homeserver_control.configuration.arr import bootstrap_arr_account

    state = {"id": 1, "username": "", "password": "", "port": 7878, "foreign": True}
    writes = []

    def handler(request):
        assert request.url.path.startswith(f"/api/{version}/config/host")
        if request.method == "PUT":
            writes.append(json.loads(request.content))
            state.update(writes[-1])
            state["password"] = "native-secret-hash"
            raise httpx.ReadError("response lost fixture secret", request=request)
        return httpx.Response(200, json=state)

    env = {
        "HOMESERVER_ADMIN_USERNAME": "fixture-user",
        "HOMESERVER_ADMIN_PASSWORD": "fixture-secret",
    }
    async with httpx.AsyncClient(
        base_url=f"http://{service}", transport=httpx.MockTransport(handler)
    ) as client:
        plan = await bootstrap_arr_account(service, env, client, "plan")
        assert not writes
        assert "fixture-secret" not in str(plan)
        assert "fixture-user" not in str(plan)
        assert await bootstrap_arr_account(service, env, client, "apply")
        assert not await bootstrap_arr_account(service, env, client, "apply")
        assert not await bootstrap_arr_account(service, env, client, "verify")
    assert len(writes) == 1
    assert writes[0]["foreign"] is True
    assert writes[0]["authenticationMethod"] == "forms"
    assert writes[0]["authenticationRequired"] == "enabled"


@pytest.mark.asyncio
async def test_arr_adopts_equivalent_root_id_preserving_unmanaged_roots():
    state = [{"id": 14, "path": "/data/movies"}, {"id": 8, "path": "/manual"}]

    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=state)

    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(handler)
    ) as client:
        outcome = await reconcile_arr(
            "radarr", {"HOMESERVER_MOVIE_ROOT": "/data/movies"}, client, "apply"
        )
    assert outcome.status == "verified"
    assert not outcome.changes
    assert state[0]["id"] == 14


@pytest.mark.asyncio
async def test_native_provider_field_updates_ignore_metadata_and_preserve_foreign_fields():
    import json

    state = [
        {
            "id": 9,
            "name": "Managed",
            "fields": [
                {"name": "host", "value": "old", "helpText": "effective"},
                {"name": "manual", "value": "preserved"},
            ],
        }
    ]

    def handler(request):
        if request.method == "PUT":
            state[0] = json.loads(request.content)
            state[0].pop("presets", None)  # Native GET omits schema-only choices.
            next(field for field in state[0]["fields"] if field["name"] == "host")["helpText"] = (
                "effective"
            )
        return httpx.Response(200, json=state)

    desired = {
        "name": "Managed",
        "presets": [],
        "fields": [{"name": "host", "value": "gateway", "helpText": "template"}],
    }
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(handler)
    ) as client:
        assert await collection(
            "radarr", client, "/api/v3/downloadclient", desired, "name", "apply"
        )
        assert not await collection(
            "radarr", client, "/api/v3/downloadclient", desired, "name", "apply"
        )
    assert any(
        field["name"] == "manual" and field["value"] == "preserved" for field in state[0]["fields"]
    )


@pytest.mark.asyncio
async def test_partial_services_and_errors_are_redacted():
    def handler(request):
        return httpx.Response(401, text="secret-private-token")

    result = await apply_native_settings(
        {"HOMESERVER_QBIT_PASSWORD": "secret-private-token"}, transport=httpx.MockTransport(handler)
    )
    assert any(item.status == "failed" for item in result)
    assert "secret-private-token" not in repr(result)


@pytest.mark.asyncio
async def test_arr_response_lost_recovers_by_reading_identity():
    state = []

    def handler(request):
        if request.method == "POST":
            state.append({"id": 9, "path": "/data/movies"})
            raise httpx.ReadTimeout("secret-private-token")
        return httpx.Response(200, json=state)

    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_arr(
            "radarr", {"HOMESERVER_MOVIE_ROOT": "/data/movies"}, client, "apply"
        )
        second = await reconcile_arr(
            "radarr", {"HOMESERVER_MOVIE_ROOT": "/data/movies"}, client, "apply"
        )
    assert first.status == second.status == "verified"
    assert len(state) == 1


@pytest.mark.asyncio
async def test_bazarr_applies_native_form_provider_settings_and_preserves_profiles():
    from urllib.parse import parse_qs

    state = {
        "general": {"enabled_providers": [], "use_sonarr": False, "use_radarr": False},
        "subdl": {"api_key": ""},
        "opensubtitlescom": {"username": "", "password": ""},
    }

    def handler(request):
        if request.method == "POST":
            for key, values in parse_qs(request.content.decode(), keep_blank_values=True).items():
                section, field = key.removeprefix("settings-").split("-", 1)
                state[section][field] = values if field == "enabled_providers" else values[0]
            return httpx.Response(204)
        return httpx.Response(200, json=state)

    async with httpx.AsyncClient(
        base_url="http://bazarr", transport=httpx.MockTransport(handler)
    ) as client:
        result = await reconcile_bazarr(
            {"HOMESERVER_BAZARR_PROVIDERS": "subdl", "HOMESERVER_SUBDL_API_KEY": "fixture-secret"},
            client,
            "apply",
        )
    assert result.status == "verified"
    assert state["general"]["enabled_providers"] == ["subdl"]
    assert state["subdl"]["api_key"] == "fixture-secret"
    assert "fixture-secret" not in repr(result)


@pytest.mark.asyncio
async def test_jellyfin_library_adoption_and_missing_create_preserves_users():
    import json

    libraries = [{"Name": "Existing Movies", "ItemId": "12", "Locations": ["/data/media/movies"]}]
    users = [{"Id": "42", "Name": "admin"}]

    def handler(request):
        if request.url.path == "/Users":
            assert request.method == "GET"
            return httpx.Response(200, json=users)
        if request.method == "POST":
            body = json.loads(request.content)
            libraries.append(
                {
                    "Name": request.url.params["name"],
                    "ItemId": "15",
                    "Locations": [item["Path"] for item in body["LibraryOptions"]["PathInfos"]],
                }
            )
            return httpx.Response(204)
        return httpx.Response(200, json=libraries)

    async with httpx.AsyncClient(
        base_url="http://jellyfin", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_media_server("jellyfin", {}, client, "apply")
        second = await reconcile_media_server("jellyfin", {}, client, "apply")
    assert first.status == second.status == "verified"
    assert len(libraries) == 2
    assert libraries[0]["ItemId"] == "12"
    assert not second.changes


@pytest.mark.asyncio
async def test_fresh_native_closed_loop_discovery_reapply_and_verify():
    import json
    from urllib.parse import parse_qs

    env = {
        f"HOMESERVER_{service.upper()}_URL": f"http://{service}:{port}"
        for service, port in {
            "qbit": 8080,
            "radarr": 7878,
            "sonarr": 8989,
            "prowlarr": 9696,
            "bazarr": 6767,
            "jellyfin": 8096,
            "seerr": 5055,
        }.items()
    }
    env.update(
        HOMESERVER_ADMIN_PASSWORD="fixture-password",
        HOMESERVER_QBIT_PASSWORD="fixture-password",
        HOMESERVER_JELLYFIN_API_KEY="dummy",
        HOMESERVER_SEERR_API_KEY="dummy",
        HOMESERVER_RADARR_API_KEY="fixture-radarr",
        HOMESERVER_SONARR_API_KEY="fixture-sonarr",
        HOMESERVER_MEDIA_RESOLUTIONS="1080",
        HOMESERVER_MEDIA_SOURCES="bluray",
        HOMESERVER_ARR_TOKEN="fixture-gateway",
        HOMESERVER_SUBTITLE_LANGUAGES="pt-BR,en-US",
        HOMESERVER_BAZARR_PROVIDERS="subdl",
    )
    state = {
        "qbit": {},
        "radarr": {},
        "sonarr": {},
        "prowlarr": {},
        "jellyfin": {"initialized": False, "libraries": []},
        "seerr": {
            "initialized": False,
            "connection": {"ip": "", "port": 8096, "urlBase": "", "useSsl": False},
            "libraries": [],
            "radarr": [],
            "sonarr": [],
        },
        "bazarr": {
            "profiles": [],
            "settings": {
                "auth": {"username": "admin", "password": "fixture-native-hash", "type": "form"},
                "general": {
                    "enabled_providers": [],
                    "serie_default_enabled": False,
                    "movie_default_enabled": False,
                    "serie_default_profile": "",
                    "movie_default_profile": "",
                    "use_sonarr": False,
                    "use_radarr": False,
                },
                **{
                    service: {"ip": "", "port": 0, "ssl": False, "base_url": "/", "apikey": ""}
                    for service in ["sonarr", "radarr"]
                },
            },
        },
    }
    mutations = []

    def handler(request):
        host, path, method = request.url.host, request.url.path, request.method
        body = (
            json.loads(request.content)
            if request.content and request.headers.get("content-type") == "application/json"
            else {}
        )
        if host in ("radarr", "sonarr", "prowlarr") and "/config/host" in path:
            if method == "PUT":
                state[host]["account"] = body | {"password": "fixture-native-hash"}
            return httpx.Response(
                200, json=state[host].get("account", {"id": 1, "username": "", "password": ""})
            )
        if (
            method != "GET"
            and not path.endswith("/auth/login")
            and not path.endswith("/auth/jellyfin")
            and not path.endswith("/AuthenticateByName")
        ):
            mutations.append((host, path))
        if host == "qbit":
            if path.endswith("/categories"):
                return httpx.Response(200, json={"radarr": {}, "sonarr": {}})
            if path.endswith("/auth/login"):
                return httpx.Response(200, text="Ok.")
            if path.endswith("/setPreferences"):
                state[host].update(json.loads(parse_qs(request.content.decode())["json"][0]))
            return httpx.Response(200, json=state[host])
        if host in ("radarr", "sonarr", "prowlarr"):
            endpoint = path.split("/")[3]
            if path.endswith("/schema"):
                if endpoint == "qualityprofile":
                    return httpx.Response(
                        200,
                        json={
                            "name": "Any",
                            "items": [
                                {
                                    "quality": {
                                        "id": 7,
                                        "name": "Bluray-1080p",
                                        "resolution": 1080,
                                        "source": "bluray",
                                    },
                                    "allowed": True,
                                }
                            ],
                            "cutoff": 7,
                        },
                    )
                if endpoint == "downloadclient":
                    category = "movieCategory" if host == "radarr" else "tvCategory"
                    return httpx.Response(
                        200,
                        json=[
                            {
                                "implementation": "QBittorrent",
                                "fields": [
                                    {"name": name, "value": ""}
                                    for name in [
                                        "host",
                                        "port",
                                        "useSsl",
                                        "urlBase",
                                        "username",
                                        "password",
                                        category,
                                    ]
                                ],
                            }
                        ],
                    )
                return httpx.Response(
                    200,
                    json=[
                        {
                            "implementation": service.title(),
                            "fields": [
                                {"name": name, "value": ""}
                                for name in ["prowlarrUrl", "baseUrl", "apiKey"]
                            ],
                        }
                        for service in ["radarr", "sonarr"]
                    ],
                )
            rows = state[host].setdefault(endpoint, [])
            if method == "POST":
                rows.append(body | {"id": len(rows) + 101})
            if method == "PUT":
                state[host][endpoint] = [
                    body if item["id"] == body["id"] else item for item in rows
                ]
            return httpx.Response(200, json=state[host][endpoint])
        if host == "jellyfin":
            if path == "/System/Info/Public":
                return httpx.Response(
                    200, json={"StartupWizardCompleted": state[host]["initialized"]}
                )
            if path == "/System/Info":
                return httpx.Response(
                    200 if request.headers.get("X-Emby-Token") == "actual-jellyfin" else 401,
                    json={},
                )
            if path == "/Users/AuthenticateByName":
                return httpx.Response(200, json={"AccessToken": "actual-jellyfin"})
            if path == "/Startup/Complete":
                state[host]["initialized"] = True
            if path.startswith("/Startup/"):
                return httpx.Response(204)
            if path == "/Users":
                return httpx.Response(200, json=[{"Id": "1", "Name": "admin"}])
            if method == "POST":
                assert request.url.params["refreshLibrary"] == "true"
                state[host]["libraries"].append(
                    {
                        "Name": request.url.params["name"],
                        "ItemId": str(len(state[host]["libraries"]) + 41),
                        "Locations": [item["Path"] for item in body["LibraryOptions"]["PathInfos"]],
                    }
                )
            return httpx.Response(200, json=state[host]["libraries"])
        if host == "bazarr":
            if path.endswith("/profiles"):
                return httpx.Response(200, json=state[host]["profiles"])
            if path.endswith("/languages"):
                return httpx.Response(
                    200, json=[{"code2": "pb", "enabled": True}, {"code2": "en", "enabled": True}]
                )
            if method == "POST":
                form = parse_qs(request.content.decode(), keep_blank_values=True)
                if "languages-profiles" in form:
                    state[host]["profiles"] = json.loads(form.pop("languages-profiles")[0])
                for key, values in form.items():
                    section, field = key.removeprefix("settings-").split("-", 1)
                    value = values if field == "enabled_providers" else values[0]
                    if value in ("true", "false"):
                        value = value == "true"
                    elif isinstance(value, str) and value.isdigit():
                        value = int(value)
                    state[host]["settings"][section][field] = value
            return httpx.Response(200, json=state[host]["settings"])
        if path.endswith("/public"):
            return httpx.Response(200, json={"initialized": state[host]["initialized"]})
        if path.endswith("/auth/jellyfin"):
            if not state[host]["connection"]["ip"]:
                if "hostname" not in body:
                    return httpx.Response(500)
                assert body["hostname"] == "jellyfin"
                state[host]["connection"].update(
                    ip=body["hostname"],
                    port=body["port"],
                    urlBase=body["urlBase"],
                    useSsl=body["useSsl"],
                )
            else:
                assert "hostname" not in body
            return httpx.Response(
                200, json={"id": 1}, headers={"set-cookie": "connect.sid=fixture; Path=/"}
            )
        if path.endswith("/settings/main"):
            if request.headers.get("X-Api-Key") != "actual-seerr" and not request.headers.get(
                "cookie"
            ):
                return httpx.Response(401)
            return httpx.Response(200, json={"apiKey": "actual-seerr"})
        if path.endswith("/initialize"):
            state[host]["initialized"] = True
            return httpx.Response(200, json={})
        if "/library" in path:
            if path.endswith("/sync"):
                state[host]["libraries"] = [
                    {"id": item["ItemId"], "name": item["Name"], "enabled": False}
                    for item in state["jellyfin"]["libraries"]
                ]
            elif method == "PUT":
                next(
                    item for item in state[host]["libraries"] if item["id"] == path.split("/")[-1]
                ).update(body)
            return httpx.Response(200, json=state[host]["libraries"])
        endpoint = path.split("/")[4]
        if endpoint in ("radarr", "sonarr"):
            if method == "POST":
                state[host][endpoint].append(body | {"id": 71})
            elif method == "PUT":
                state[host][endpoint][0] = body
            return httpx.Response(200, json=state[host][endpoint])
        if method == "POST":
            assert "hostname" not in body
            state[host]["connection"].update(body)
        return httpx.Response(
            200, json=state[host]["connection"] | {"libraries": state[host]["libraries"]}
        )

    transport = httpx.MockTransport(handler)
    first = await apply_native_settings(env, transport=transport)
    assert all(item.status == "verified" for item in first), first
    updates = await discover_native_credentials(env, transport=transport)
    env.update(updates)
    count = len(mutations)
    second = await apply_native_settings(env, transport=transport)
    assert all(item.status == "verified" and not item.changes for item in second), second
    assert len(mutations) == count
    verified = await verify_native_settings(env, transport=transport)
    assert all(item.status == "verified" for item in verified), verified
    assert await discover_native_credentials(env, transport=transport) == {}


@pytest.mark.asyncio
async def test_native_credential_discovery_authenticates_without_resetting_accounts():
    writes = []

    def handler(request):
        if request.method == "POST":
            writes.append(request.url.path)
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "fixture-jellyfin-token"})
        if request.url.path.endswith("/auth/jellyfin"):
            import json

            assert "hostname" not in json.loads(request.content)
            return httpx.Response(
                200, json={"id": 1}, headers={"set-cookie": "connect.sid=fixture; Path=/"}
            )
        assert request.url.path.endswith("/settings/main")
        assert request.headers["cookie"] == "connect.sid=fixture"
        return httpx.Response(200, json={"apiKey": "fixture-seerr-key"})

    result = await discover_native_credentials(
        {"HOMESERVER_ADMIN_USERNAME": "admin", "HOMESERVER_ADMIN_PASSWORD": "fixture-private"},
        transport=httpx.MockTransport(handler),
    )
    assert result == {
        "HOMESERVER_JELLYFIN_API_KEY": "fixture-jellyfin-token",
        "HOMESERVER_SEERR_API_KEY": "fixture-seerr-key",
    }
    assert writes == ["/Users/AuthenticateByName", "/api/v1/auth/jellyfin"]


@pytest.mark.asyncio
async def test_credential_discovery_keeps_valid_keys_without_reauth():
    def handler(request):
        assert request.method == "GET"
        if request.url.host == "jellyfin":
            assert request.headers["X-Emby-Token"] == "existing-token"
            return httpx.Response(200, json={"Id": "server"})
        assert request.headers["X-Api-Key"] == "existing-key"
        return httpx.Response(200, json={"apiKey": "existing-key"})

    result = await discover_native_credentials(
        {
            "HOMESERVER_JELLYFIN_API_KEY": "existing-token",
            "HOMESERVER_SEERR_API_KEY": "existing-key",
        },
        transport=httpx.MockTransport(handler),
    )
    assert result == {}


@pytest.mark.asyncio
async def test_seerr_bootstrap_plan_no_mutation_and_sync_preserves_ids():
    import json

    state = {"initialized": False, "ip": "", "port": 8096, "urlBase": "", "useSsl": False}
    libraries = [
        {"id": "42", "name": "Movies", "enabled": False},
        {"id": "66", "name": "Manual", "enabled": False},
    ]
    writes = []

    def handler(request):
        if request.method != "GET":
            writes.append(request.url.path)
        if request.url.path.endswith("/public"):
            return httpx.Response(200, json={"initialized": state["initialized"]})
        if request.url.path.endswith("/auth/jellyfin"):
            payload = json.loads(request.content)
            if "hostname" not in payload:
                return httpx.Response(500)
            assert payload["hostname"] == "jellyfin"
            assert payload["port"] == 8096
            assert payload["urlBase"] == ""
            assert payload["useSsl"] is False
            return httpx.Response(
                200, json={"id": 1}, headers={"set-cookie": "connect.sid=fixture; Path=/"}
            )
        if request.url.path.endswith("/initialize"):
            state["initialized"] = True
            return httpx.Response(200, json={})
        if "/library" in request.url.path:
            if request.method == "PUT":
                library = next(
                    item for item in libraries if item["id"] == request.url.path.split("/")[-1]
                )
                library.update(json.loads(request.content))
            return httpx.Response(200, json=libraries)
        if request.method == "POST":
            state.update(json.loads(request.content))
        return httpx.Response(200, json=state | {"libraries": libraries})

    env = {
        "HOMESERVER_ADMIN_USERNAME": "admin",
        "HOMESERVER_ADMIN_PASSWORD": "fixture-private",
        "HOMESERVER_JELLYFIN_URL": "http://jellyfin:8096",
    }
    async with httpx.AsyncClient(
        base_url="http://seerr", transport=httpx.MockTransport(handler)
    ) as client:
        plan = await bootstrap_seerr(env, client, "plan")
        assert plan.status == "planned"
        assert writes == []
        assert await bootstrap_seerr(env, client, "apply") is None
        result = await reconcile_media_server("seerr", env, client, "apply")
        assert result.status == "verified"
    assert libraries[0] == {"id": "42", "name": "Movies", "enabled": True}
    assert libraries[1] == {"id": "66", "name": "Manual", "enabled": False}
    assert "fixture-private" not in repr(plan)


@pytest.mark.asyncio
async def test_prowlarr_byparr_schema_and_indexer_adoption_keep_ids():
    import json

    state = {
        "tag": [{"id": 9, "label": "homeserver-byparr"}],
        "indexerProxy": [],
        "appprofile": [
            {
                "id": 7,
                "name": "HomeServer",
                "enableRss": False,
                "enableAutomaticSearch": False,
                "enableInteractiveSearch": True,
            }
        ],
        "indexer": [
            {
                "id": 54,
                "name": "Fixture",
                "implementation": "Cardigann",
                "fields": [{"name": "apiKey", "value": "fixture-private"}],
                "enable": True,
                "appProfileId": 7,
            }
        ],
    }

    def handler(request):
        endpoint = request.url.path.split("/")[3]
        if request.url.path.endswith("/schema"):
            if endpoint == "indexer":
                return httpx.Response(
                    200,
                    json=[
                        {key: value for key, value in state["indexer"][0].items() if key != "id"}
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {
                        "implementation": "FlareSolverr",
                        "configContract": "FlareSolverrSettings",
                        "fields": [{"name": "host", "value": ""}],
                    }
                ],
            )
        if request.method == "POST":
            row = json.loads(request.content)
            row["id"] = 55
            state[endpoint].append(row)
        elif request.method == "PUT":
            row = json.loads(request.content)
            current = next(item for item in state[endpoint] if item["id"] == row["id"])
            current.update(row)
        return httpx.Response(200, json=state[endpoint])

    env = {
        "HOMESERVER_BYPARR_ENABLED": "true",
        "HOMESERVER_BYPARR_URL": "http://byparr:8191",
        "HOMESERVER_PROWLARR_INDEXERS": json.dumps([state["indexer"][0]]),
    }
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_prowlarr(env, client, "apply")
        second = await reconcile_prowlarr(env, client, "apply")
    assert first.status == second.status == "verified"
    assert len(state["indexerProxy"]) == 1
    assert state["indexerProxy"][0]["tags"] == state["indexer"][0]["tags"] == [9]
    assert state["indexer"][0]["id"] == 54
    assert "enableRss" not in state["indexer"][0]
    assert not second.changes
    assert "fixture-private" not in repr(first)


@pytest.mark.asyncio
async def test_prowlarr_unknown_public_definition_is_explicitly_unsupported():
    import json

    async with httpx.AsyncClient(
        base_url="http://prowlarr",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[])),
    ) as client:
        result = await reconcile_prowlarr(
            {
                "HOMESERVER_PROWLARR_INDEXERS": json.dumps(
                    [
                        {
                            "name": "Public fixture",
                            "definitionName": "unsupported-definition",
                            "implementation": "Cardigann",
                        }
                    ]
                )
            },
            client,
            "apply",
        )
    assert result.status == "unsupported"
    assert "definition" in result.message.lower()


@pytest.mark.asyncio
async def test_arr_native_quality_schema_and_gateway_keep_ids_and_manual_fields():
    import json

    state = {
        "rootfolder": [{"id": 1, "path": "/data/media/movies"}],
        "qualityprofile": [{"id": 20, "name": "HomeServer", "manual": True, "items": []}],
        "downloadclient": [],
    }
    templates = {
        "qualityprofile": [
            {
                "name": "Any",
                "items": [
                    {
                        "quality": {
                            "id": 4,
                            "name": "HDTV-720p",
                            "resolution": 720,
                            "source": "television",
                        },
                        "allowed": True,
                    },
                    {
                        "quality": {
                            "id": 7,
                            "name": "Bluray-1080p",
                            "resolution": 1080,
                            "source": "bluray",
                        },
                        "allowed": True,
                    },
                ],
                "cutoff": 7,
            }
        ],
        "downloadclient": [
            {
                "implementation": "QBittorrent",
                "fields": [
                    {"name": name, "value": ""}
                    for name in [
                        "host",
                        "port",
                        "useSsl",
                        "urlBase",
                        "username",
                        "password",
                        "movieCategory",
                    ]
                ],
            }
        ],
    }

    def handler(request):
        endpoint = request.url.path.split("/")[3]
        if request.url.path.endswith("/schema"):
            return httpx.Response(
                200,
                json=templates[endpoint][0]
                if endpoint == "qualityprofile"
                else templates[endpoint],
            )
        if request.method in ("POST", "PUT"):
            item = json.loads(request.content)
            if request.method == "PUT":
                state[endpoint] = [
                    item if old["id"] == item["id"] else old for old in state[endpoint]
                ]
            else:
                item["id"] = 21
                state[endpoint].append(item)
            return httpx.Response(200, json=item)
        return httpx.Response(200, json=state[endpoint])

    settings = {
        "HOMESERVER_MEDIA_RESOLUTIONS": "1080",
        "HOMESERVER_MEDIA_SOURCES": "bluray",
        "HOMESERVER_ARR_TOKEN": "fixture-private",
        "HOMESERVER_QBIT_GATEWAY_URL": "http://download-gateway:8081",
    }
    async with httpx.AsyncClient(
        base_url="http://radarr", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_arr("radarr", settings, client, "apply")
        second = await reconcile_arr("radarr", settings, client, "apply")
    assert first.status == second.status == "verified"
    assert not second.changes
    assert state["qualityprofile"][0]["id"] == 20
    assert state["qualityprofile"][0]["manual"] is True
    assert state["qualityprofile"][0]["items"][0]["allowed"] is False
    fields = {item["name"]: item["value"] for item in state["downloadclient"][0]["fields"]}
    assert fields["host"] == "download-gateway"
    assert fields["port"] == 8081
    assert fields["password"] == "fixture-private"
    assert "fixture-private" not in repr(first)


@pytest.mark.asyncio
async def test_jellyfin_interrupted_fresh_apply_authenticates_invalid_placeholder_once():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/System/Info/Public":
            return httpx.Response(200, json={"StartupWizardCompleted": True})
        if request.url.path == "/System/Info":
            return httpx.Response(
                200 if request.headers.get("X-Emby-Token") == "fixture-valid" else 401,
                json={},
            )
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "fixture-valid"})
        raise AssertionError("must not reset initialized native account")

    settings = {
        "HOMESERVER_JELLYFIN_API_KEY": "placeholder",
        "HOMESERVER_ADMIN_PASSWORD": "private",
    }
    async with httpx.AsyncClient(
        base_url="http://jellyfin",
        headers={"X-Emby-Token": "placeholder"},
        transport=httpx.MockTransport(handler),
    ) as client:
        assert await bootstrap_jellyfin(settings, client, "apply") is None
        assert await bootstrap_jellyfin(settings, client, "apply") is None
        assert client.headers["X-Emby-Token"] == "fixture-valid"
    assert calls.count("/Users/AuthenticateByName") == 1


@pytest.mark.asyncio
async def test_jellyfin_startup_only_sets_password_once_and_authenticates_existing():
    initialized = False
    first_user_initialized = False
    writes = []

    def handler(request):
        nonlocal initialized, first_user_initialized
        if request.url.path == "/System/Info/Public":
            return httpx.Response(200, json={"StartupWizardCompleted": initialized})
        if request.url.path == "/Startup/Complete":
            initialized = True
        if request.url.path == "/Users/AuthenticateByName":
            return httpx.Response(200, json={"AccessToken": "fixture-private-token"})
        if request.url.path == "/Startup/User" and request.method == "GET":
            first_user_initialized = True
            return httpx.Response(200, json={"Name": ""})
        if request.url.path == "/Startup/User" and request.method == "POST":
            assert first_user_initialized
        writes.append(request.url.path)
        return httpx.Response(204)

    settings = {
        "HOMESERVER_ADMIN_USERNAME": "admin",
        "HOMESERVER_ADMIN_PASSWORD": "fixture-private",
    }
    async with httpx.AsyncClient(
        base_url="http://jellyfin", transport=httpx.MockTransport(handler)
    ) as client:
        planned = await bootstrap_jellyfin(settings, client, "plan")
        assert planned.status == "planned"
        assert writes == []
        assert await bootstrap_jellyfin(settings, client, "apply") is None
        assert await bootstrap_jellyfin(settings, client, "apply") is None
        assert client.headers["X-Emby-Token"] == "fixture-private-token"
    assert writes.count("/Startup/User") == 1
    assert "fixture-private" not in repr(planned)


@pytest.mark.asyncio
async def test_bazarr_language_profile_merges_all_ids_and_is_idempotent():
    import json
    from urllib.parse import parse_qs

    profiles = [
        {
            "profileId": 44,
            "name": "Manual",
            "items": [],
            "cutoff": None,
            "mustContain": [],
            "mustNotContain": [],
            "originalFormat": None,
        },
        {
            "profileId": 88,
            "name": "HomeServer",
            "items": [],
            "cutoff": None,
            "mustContain": [],
            "mustNotContain": [],
            "originalFormat": None,
        },
    ]
    settings = {
        "general": {
            "enabled_providers": ["subdl"],
            "serie_default_enabled": False,
            "movie_default_enabled": False,
            "serie_default_profile": "",
            "movie_default_profile": "",
        }
    }
    writes = []

    def handler(request):
        nonlocal profiles
        if request.url.path.endswith("/profiles"):
            return httpx.Response(200, json=profiles)
        if request.url.path.endswith("/languages"):
            return httpx.Response(
                200, json=[{"code2": "pb", "enabled": True}, {"code2": "en", "enabled": True}]
            )
        if request.method == "POST":
            body = parse_qs(request.content.decode(), keep_blank_values=True)
            if "languages-profiles" in body:
                profiles = json.loads(body.pop("languages-profiles")[0])
                writes.append(profiles)
            body.pop("languages-enabled", None)
            for key, values in body.items():
                section, field = key.removeprefix("settings-").split("-", 1)
                settings[section][field] = (
                    int(values[0]) if field.endswith("_profile") else values[0] == "true"
                )
            return httpx.Response(204)
        return httpx.Response(200, json=settings)

    env = {"HOMESERVER_BAZARR_PROVIDERS": "subdl", "HOMESERVER_SUBTITLE_LANGUAGES": "pt-BR,en-US"}
    async with httpx.AsyncClient(
        base_url="http://bazarr", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_bazarr(env, client, "apply")
        second = await reconcile_bazarr(env, client, "apply")
    assert first.status == second.status == "verified"
    assert len(writes) == 1
    assert profiles[0]["profileId"] == 44
    assert profiles[1]["profileId"] == 88
    assert profiles[1]["items"][0]["language"] == "pb"
    assert not second.changes


@pytest.mark.asyncio
async def test_seerr_arr_connection_uses_adopted_quality_id_and_no_autonomous_search():
    import json

    state = [{"id": 73, "name": "HomeServer Radarr", "manual": "keep", "activeProfileId": 2}]

    def handler(request):
        if request.url.host == "radarr":
            return httpx.Response(200, json=[{"id": 93, "name": "HomeServer"}])
        if request.url.path.endswith("/public"):
            return httpx.Response(200, json={"initialized": True})
        if request.url.path.endswith("/library") or request.url.path.endswith("/library/sync"):
            return httpx.Response(
                200, json=[{"id": "fixture-library", "name": "Movies", "enabled": True}]
            )
        if "/settings/radarr" in request.url.path:
            if request.method == "PUT":
                state[0] = json.loads(request.content) | {
                    "id": int(request.url.path.rsplit("/", 1)[-1])
                }
            return httpx.Response(200, json=state)
        return httpx.Response(
            200,
            json={
                "ip": "jellyfin",
                "port": 8096,
                "urlBase": "",
                "useSsl": False,
                "libraries": [{"id": "fixture-library", "name": "Movies", "enabled": True}],
            },
        )

    transport = httpx.MockTransport(handler)
    env = {
        "HOMESERVER_RADARR_URL": "http://radarr:7878",
        "HOMESERVER_RADARR_API_KEY": "fixture-private",
    }
    async with httpx.AsyncClient(base_url="http://seerr", transport=transport) as client:
        result = await reconcile_media_server("seerr", env, client, "apply", transport=transport)
    assert result.status == "verified"
    assert state[0]["id"] == 73
    assert state[0]["activeProfileId"] == 93
    assert state[0]["preventSearch"] is True
    assert state[0]["manual"] == "keep"
    assert "fixture-private" not in repr(result)


@pytest.mark.asyncio
async def test_prowlarr_native_application_connects_without_credentials_in_diff():
    import json

    state = []

    def handler(request):
        if request.url.path.endswith("/schema"):
            return httpx.Response(
                200,
                json=[
                    {
                        "implementation": "Radarr",
                        "configContract": "RadarrSettings",
                        "fields": [
                            {"name": name, "value": ""}
                            for name in ["prowlarrUrl", "baseUrl", "apiKey"]
                        ],
                    }
                ],
            )
        if request.method == "POST":
            row = json.loads(request.content)
            row["id"] = 24
            state.append(row)
        return httpx.Response(200, json=state)

    env = {
        "HOMESERVER_RADARR_API_KEY": "fixture-private",
        "HOMESERVER_RADARR_URL": "http://radarr:7878",
    }
    async with httpx.AsyncClient(
        base_url="http://prowlarr", transport=httpx.MockTransport(handler)
    ) as client:
        first = await reconcile_prowlarr(env, client, "apply")
        second = await reconcile_prowlarr(env, client, "apply")
    assert first.status == second.status == "verified"
    assert len(state) == 1
    assert state[0]["syncLevel"] == "addOnly"
    assert "fixture-private" not in repr(first)
    assert not second.changes
