"""Explicit Seerr choices reach existing native titles exactly once."""

import json
from copy import deepcopy

import httpx
import pytest

from homeserver_control.adapters.seerr import SeerrAdapter
from homeserver_control.persistence.db import ReservationResult
from homeserver_control.worker.runtime import WorkerCycle


class NativeSelection:
    def __init__(self, service="radarr"):
        self.service = service
        self.resource = "movie" if service == "radarr" else "series"
        self.port = 7878 if service == "radarr" else 8989
        self.media = {
            "id": 6,
            "tmdbId": 123,
            "qualityProfileId": 22,
            "monitored": False,
            "path": "/media/fixture",
            "tags": [2],
            "seasons": [{"seasonNumber": 1, "monitored": False}],
        }
        self.profiles = [
            {"id": 21, "name": "Max Quality"},
            {"id": 22, "name": "Standard"},
            {"id": 23, "name": "Dublado"},
        ]
        self.servers = [
            {
                "id": 0,
                "hostname": service,
                "port": self.port,
                "baseUrl": "",
                "useSsl": False,
                "is4k": False,
                "isDefault": True,
            }
        ]
        self.writes = []
        self.accept = True
        self.timeout_after_write = False
        self.approved = []

    def request(self, request):
        if request.url.host == "seerr":
            assert request.method == "GET"
            if request.url.path == f"/api/v1/settings/{self.service}":
                return httpx.Response(200, json=self.servers)
            if request.url.path == "/api/v1/request":
                return httpx.Response(200, json={"results": self.approved})
            raise AssertionError(request.url.path)
        assert request.url.host == self.service
        if request.url.path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=self.profiles)
        if request.url.path == f"/api/v3/{self.resource}":
            assert request.method == "GET"
            return httpx.Response(200, json=[self.media])
        assert request.url.path == f"/api/v3/{self.resource}/6"
        if request.method == "PUT":
            assert not request.url.query
            payload = json.loads(request.content)
            self.writes.append(payload)
            if self.accept:
                self.media = payload
            if self.timeout_after_write:
                self.timeout_after_write = False
                raise httpx.ReadTimeout("fixture", request=request)
        else:
            assert request.method == "GET"
        return httpx.Response(200, json=self.media)


def choice(identifier=10, profile=21, *, kind="movie", season=1):
    return {
        "source_id": str(identifier) if kind == "movie" else f"{identifier}:{season}",
        "media_key": "movie:tmdb:123" if kind == "movie" else f"season:tmdb:123:{season}",
        "kind": kind,
        "profile_id": profile,
        "server_id": 0,
        "is_4k": False,
    }


def synchronizer(tmp_path, source, native):
    from homeserver_control.worker.request_profiles import RequestProfileSynchronizer

    return RequestProfileSynchronizer(
        database=tmp_path / "control.sqlite",
        source=source,
        services={native.service: (f"http://{native.service}:{native.port}", "fixture")},
    )


@pytest.mark.asyncio
async def test_seerr_binding_applies_series_profile_without_rewriting_missing_native_tmdb(tmp_path):
    native = NativeSelection("sonarr")
    native.media.update(tmdbId=0, tvdbId=383203)
    request = choice(kind="season") | {
        "tvdb_id": 383203, "external_service_id": 6, "title": "Localized Fixture",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        assert await synchronizer(tmp_path, source, native).reconcile([request]) == set()
    assert native.media["qualityProfileId"] == 21
    assert native.media["tmdbId"] == 0
    assert native.writes[0]["tmdbId"] == 0
    assert "title" not in native.writes[0]
    assert "_homeserver_subtitle_titles" not in native.writes[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["positive_tmdb", "native_id", "tvdb", "ambiguous", "detail"])
async def test_profile_binding_rejects_conflicts_before_any_native_write(tmp_path, fault):
    native = NativeSelection("sonarr")
    native.media.update(tmdbId=0, tvdbId=383203)
    if fault == "positive_tmdb":
        native.media["tmdbId"] = 999
    elif fault == "tvdb":
        native.media["tvdbId"] = 999
    request = choice(kind="season") | {
        "tvdb_id": 383203, "external_service_id": 7 if fault == "native_id" else 6,
    }

    def handle(request):
        if request.url.path == "/api/v3/series" and fault == "ambiguous":
            return httpx.Response(200, json=[native.media, native.media | {"id": 7}])
        if request.url.path == "/api/v3/series/6" and fault == "detail":
            return httpx.Response(200, json=native.media | {"tvdbId": 999})
        return native.request(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        assert await synchronizer(tmp_path, source, native).reconcile([request]) == {
            "season:tmdb:123:1",
        }
    assert native.writes == []


@pytest.mark.asyncio
async def test_already_applied_profile_refreshes_shared_identity_after_restart(tmp_path):
    from homeserver_control.worker.request_profiles import RequestProfileSynchronizer
    from homeserver_control.worker.series_identity import SeriesIdentityResolver

    native = NativeSelection("sonarr")
    native.media.update(tmdbId=0, tvdbId=383203)
    request = choice(kind="season") | {"tvdb_id": 383203, "external_service_id": 6}
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        assert await synchronizer(tmp_path, source, native).reconcile([request]) == set()
        resolver = SeriesIdentityResolver()
        restarted = RequestProfileSynchronizer(
            database=tmp_path / "control.sqlite", source=source,
            services={"sonarr": ("http://sonarr:8989", "fixture")}, series_identity=resolver,
        )
        assert resolver.resolve([native.media], 123) is None
        assert await restarted.reconcile([request]) == set()
        assert resolver.resolve([native.media], 123)["tmdbId"] == 123
        assert await restarted.reconcile([]) == set()
        assert resolver.resolve([native.media], 123) is None
    assert len(native.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('service,kind', [('radarr', 'movie'), ('sonarr', 'season')])
async def test_fixed_integration_proxy_keeps_explicit_profile_native_identity(
    tmp_path, service, kind,
):
    native = NativeSelection(service)
    native.servers[0].update(hostname='integration-proxy', port=8787, baseUrl=f'/{service}')
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url='http://seerr', api_key='fixture', client=client)
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile([choice(kind=kind)]) == set()
    assert native.media['qualityProfileId'] == 21
    assert len(native.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('connection', [
    {'baseUrl': '/sonarr'}, {'hostname': 'other-proxy'}, {'port': 8788},
    {'useSsl': True}, {'baseUrl': '/radarr/../sonarr'},
])
async def test_integration_proxy_profile_identity_rejects_changed_destination(tmp_path, connection):
    native = NativeSelection('radarr')
    native.servers[0].update(hostname='integration-proxy', port=8787, baseUrl='/radarr')
    native.servers[0].update(connection)
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url='http://seerr', api_key='fixture', client=client)
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile([choice()]) == {'movie:tmdb:123'}
    assert not native.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("service,kind", [("radarr", "movie"), ("sonarr", "season")])
async def test_explicit_profile_updates_existing_title_once_and_preserves_manual_edits(
    tmp_path,
    service,
    kind,
):
    native = NativeSelection(service)
    before = deepcopy(native.media)
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        request = choice(kind=kind)
        assert await sync.reconcile([request]) == set()
        assert native.media == before | {"qualityProfileId": 21}
        native.media["qualityProfileId"] = 23  # Later manual native choice wins.
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile([request]) == set()
    assert native.media["qualityProfileId"] == 23
    assert len(native.writes) == 1


@pytest.mark.asyncio
async def test_latest_series_request_wins_across_seasons_and_older_never_replays(tmp_path):
    native = NativeSelection("sonarr")
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        older = choice(10, 21, kind="season", season=1)
        latest = choice(15, 23, kind="season", season=2)
        assert await sync.reconcile([latest, older]) == set()
        assert native.media["qualityProfileId"] == 23
        native.media["qualityProfileId"] = 22
        assert await sync.reconcile([older]) == set()
    assert native.media["qualityProfileId"] == 22
    assert len(native.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["foreign", "unknown_profile", "ambiguous", "readback"])
async def test_unverified_native_selection_blocks_search_without_media_side_effects(
    tmp_path, fault
):
    native = NativeSelection()
    if fault == "foreign":
        native.servers[0]["hostname"] = "foreign"
    elif fault == "unknown_profile":
        native.profiles = [row for row in native.profiles if row["id"] != 21]
    elif fault == "ambiguous":
        native.profiles.append({"id": 24, "name": "Max Quality"})
    else:
        native.accept = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile([choice()]) == {"movie:tmdb:123"}
    assert native.media["qualityProfileId"] == 22
    if fault != "readback":
        assert not native.writes


@pytest.mark.asyncio
async def test_lost_write_response_is_verified_and_not_replayed(tmp_path):
    native = NativeSelection()
    native.timeout_after_write = True
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile([choice()]) == set()
        assert await synchronizer(tmp_path, source, native).reconcile([choice()]) == set()
    assert len(native.writes) == 1


@pytest.mark.asyncio
async def test_seerr_normalizes_selected_profile_and_server_zero_for_each_season():
    native = NativeSelection("sonarr")
    native.approved = [
        {
            "id": 10,
            "status": 2,
            "serverId": 0,
            "profileId": 23,
            "is4k": False,
            "media": {"tmdbId": 123, "mediaType": "tv"},
            "seasons": [{"seasonNumber": 1, "status": 2}, {"seasonNumber": 2, "status": 2}],
        }
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        assert await source.list_approved(1) == [
            choice(10, 23, kind="season", season=1),
            choice(10, 23, kind="season", season=2),
        ]


@pytest.mark.asyncio
async def test_worker_profile_failure_defers_acquisition_but_still_finalizes():
    calls = []

    class Source:
        async def list_approved(self, page):
            return [choice()] if page == 1 else []

    class Scheduler:
        def admit(self, candidate):
            return ReservationResult(True, reservation_id="r1")

    class Sync:
        async def reconcile(self, requests):
            calls.append("profile")
            return {"movie:tmdb:123"}

    class Acquirer:
        async def acquire(self, *_args):
            calls.append("acquire")
            return "grabbed"

    class Finalizer:
        async def finalize(self, *_args):
            calls.append("finalize")
            return "complete"

    cycle = WorkerCycle(
        source=Source(),
        scheduler=Scheduler(),
        acquirer=Acquirer(),
        finalizer=Finalizer(),
        request_profiles=Sync(),
    )
    result = await cycle.run_once()
    assert calls == ["profile", "finalize"]
    assert result.grabbed == 0
    assert result.deferred == 1


@pytest.mark.asyncio
async def test_worker_uses_latest_profile_across_pages_before_all_searches(tmp_path):
    native = NativeSelection("sonarr")
    observed = []

    class Scheduler:
        def admit(self, candidate):
            return ReservationResult(True, reservation_id=candidate.source_id)

    class Acquirer:
        async def acquire(self, *_args):
            observed.append(native.media["qualityProfileId"])
            return "no_release"

    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)

        async def pages(page):
            return {
                1: [choice(10, 21, kind="season")],
                2: [choice(15, 23, kind="season", season=2)],
            }.get(page, [])

        source.list_approved = pages
        cycle = WorkerCycle(
            source=source,
            scheduler=Scheduler(),
            series_acquirer=Acquirer(),
            request_profiles=synchronizer(tmp_path, source, native),
        )
        await cycle.run_once()
    assert observed == [23, 23]
    assert len(native.writes) == 1


@pytest.mark.asyncio
async def test_profile_sync_precedes_manual_source_searches():
    calls = []

    class Source:
        async def list_approved(self, page):
            return [choice()] if page == 1 else []

    class Scheduler:
        def admit(self, candidate):
            return ReservationResult(True, reservation_id="r1")

    class Sync:
        async def reconcile(self, requests):
            calls.append("profile")
            return set()

    class Manual:
        async def run_once(self):
            calls.append("manual_search")
            return "no_release"

    cycle = WorkerCycle(
        source=Source(),
        scheduler=Scheduler(),
        request_profiles=Sync(),
        manual_source_replacements=Manual(),
    )
    await cycle.run_once()
    assert calls == ["profile", "manual_search"]


@pytest.mark.asyncio
async def test_raw_fixture_without_profile_choice_performs_no_http(tmp_path):
    native = NativeSelection()

    def forbidden(request):
        raise AssertionError("no explicit choice must not query native services")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        assert (
            await sync.reconcile(
                [{"source_id": "10", "media_key": "movie:tmdb:123", "kind": "movie"}]
            )
            == set()
        )
    assert not native.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("first_accepted", [True, False])
async def test_edited_profile_on_same_request_is_a_new_choice(tmp_path, first_accepted):
    native = NativeSelection()
    native.accept = first_accepted
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        await sync.reconcile([choice(10, 21)])
        native.accept = True
        assert await sync.reconcile([choice(10, 23)]) == set()
        assert native.media["qualityProfileId"] == 23
        native.media["qualityProfileId"] = 22
        assert await sync.reconcile([choice(10, 23)]) == set()
    assert native.media["qualityProfileId"] == 22
    assert len(native.writes) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_field", ["qualityCutoffNotMet", "id", "relativePath", "size"])
async def test_request_choice_allows_only_derived_movie_file_cutoff_drift(tmp_path, changed_field):
    native = NativeSelection()
    native.media["movieFile"] = {
        "id": 31,
        "relativePath": "Fixture/film.mkv",
        "size": 1000000,
        "qualityCutoffNotMet": False,
    }

    def handle(request):
        response = native.request(request)
        if request.method == "PUT":
            native.media["movieFile"][changed_field] = {
                "qualityCutoffNotMet": True,
                "id": 999,
                "relativePath": "Other/film.mkv",
                "size": 42,
            }[changed_field]
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        blocked = await sync.reconcile([choice()])
    assert blocked == (set() if changed_field == "qualityCutoffNotMet" else {"movie:tmdb:123"})


@pytest.mark.asyncio
async def test_audited_series_profile_conflict_blocks_new_season_without_writing(tmp_path):
    native = NativeSelection("sonarr")
    before = deepcopy(native.media)
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        request = choice(15, 21, kind="season", season=2)
        assert await sync.reconcile([request], preserve_native_series=frozenset({123})) == {
            "season:tmdb:123:2"
        }
        assert sync.blocked_reasons == {
            "season:tmdb:123:2": "audit_native_profile_conflict"
        }
    assert native.media == before
    assert not native.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_applied", [False, True])
async def test_audited_series_matching_profile_rechecks_drift_on_later_cycle(
    tmp_path, previously_applied
):
    native = NativeSelection("sonarr")
    request = choice(15, 22, kind="season", season=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        if previously_applied:
            assert await sync.reconcile([request]) == set()
        assert await sync.reconcile([request], preserve_native_series=frozenset({123})) == set()
        native.media["qualityProfileId"] = 23
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile([request], preserve_native_series=frozenset({123})) == {
            "season:tmdb:123:2"
        }
        assert sync.blocked_reasons == {
            "season:tmdb:123:2": "audit_native_profile_conflict"
        }
        native.media["qualityProfileId"] = 22
        assert await sync.reconcile([request], preserve_native_series=frozenset({123})) == set()
        assert sync.blocked_reasons == {}
    assert not native.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_present", [False, True])
async def test_audited_series_without_choice_verifies_native_profile(tmp_path, profile_present):
    native = NativeSelection("sonarr")
    observed = []

    def handle(request):
        observed.append((request.method, request.url.path))
        return native.request(request)

    request = choice(15, None, kind="season", season=2)
    request.pop("server_id")
    if not profile_present:
        request.pop("profile_id")
    before = deepcopy(native.media)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile([request], preserve_native_series=frozenset({123})) == set()
    assert ("GET", "/api/v3/series/6") in observed
    assert native.media == before
    assert not native.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_present", [False, True])
@pytest.mark.parametrize("fault", ["missing_title", "identity", "unknown_profile", "ambiguous"])
async def test_audited_series_without_choice_blocks_unverified_native_title(
    tmp_path, profile_present, fault
):
    native = NativeSelection("sonarr")
    if fault == "unknown_profile":
        native.media["qualityProfileId"] = 999
    elif fault == "ambiguous":
        native.profiles.append({"id": 24, "name": "Standard"})

    def handle(request):
        if request.url.path == "/api/v3/series" and fault == "missing_title":
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v3/series/6" and fault == "identity":
            return httpx.Response(200, json=native.media | {"tmdbId": 999})
        return native.request(request)

    request = choice(15, None, kind="season", season=2)
    request.pop("server_id")
    if not profile_present:
        request.pop("profile_id")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile([request], preserve_native_series=frozenset({123})) == {
            "season:tmdb:123:2"
        }
    assert not native.writes


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["foreign", "unknown_profile", "ambiguous"])
async def test_audited_series_matching_choice_still_requires_verified_selection(tmp_path, fault):
    native = NativeSelection("sonarr")
    if fault == "foreign":
        native.servers[0]["hostname"] = "foreign"
    elif fault == "unknown_profile":
        native.profiles = [row for row in native.profiles if row["id"] != 22]
    else:
        native.profiles.append({"id": 24, "name": "Standard"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(native.request)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client)
        sync = synchronizer(tmp_path, source, native)
        assert await sync.reconcile(
            [choice(15, 22, kind="season", season=2)],
            preserve_native_series=frozenset({123}),
        ) == {"season:tmdb:123:2"}
    assert not native.writes
