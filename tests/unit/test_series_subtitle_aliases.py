"""Only current, identity-checked Seerr names can identify localized pack subtitles."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.adapters.seerr import SeerrAdapter
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.series_identity import SeriesIdentityResolver

NATIVE = {
    "id": 124, "tmdbId": 0, "tvdbId": 293012, "title": "Jorel's Brother", "year": 2014,
}
REQUEST = {
    "kind": "season", "media_key": "season:tmdb:74606:1", "tvdb_id": 293012,
    "external_service_id": 124, "title": "Irmão do Jorel",
}
RELEASE = "Irmao.do.Jorel.S01E01.Capacete.Mega.Master.1080p.MAX.WEB-DL.H265.DDP2.0.PTBR"


def _pack_title(context, stem=RELEASE, *, pack_title=None):
    return SeriesFinalizer._pack_subtitle_title(
        SimpleNamespace(quality_rank=(1, 1080, 0, 0, 0, 0)), Path(stem + ".mkv"),
        context, season=1, number=1,
        pack_title=(pack_title or "Irmao.do.Jorel.S01.1080p.MAX.WEB-DL.H265.DDP2.0.PTBR"),
    )


def test_current_authenticated_localized_name_enables_subtitle_identity_without_native_mutation():
    resolver = SeriesIdentityResolver()
    resolver.update_requests([REQUEST, REQUEST | {"media_key": "season:tmdb:74606:2"}])
    native = deepcopy(NATIVE)
    resolved = resolver.resolve([native], 74606)
    titles = resolver.subtitle_titles([native], 74606)
    assert titles == ("Irmão do Jorel",)
    assert _pack_title(resolved | {"_homeserver_subtitle_titles": titles}) == RELEASE
    assert resolved == NATIVE | {"tmdbId": 74606}
    assert native == NATIVE


@pytest.mark.parametrize("fault", ["tmdb", "tvdb", "native", "duplicate"])
def test_localized_name_never_resolves_a_conflicting_or_ambiguous_native_identity(fault):
    resolver = SeriesIdentityResolver()
    resolver.update_requests([REQUEST])
    native = deepcopy(NATIVE)
    field = {"tmdb": "tmdbId", "tvdb": "tvdbId", "native": "id"}.get(fault)
    if field:
        native[field] = 999
    catalog = [native, native | {"id": 125}] if fault == "duplicate" else [native]
    assert resolver.subtitle_titles(catalog, 74606) == ()
    assert resolver.subtitle_titles([NATIVE], 999) == ()


@pytest.mark.parametrize("other", [
    REQUEST | {"title": "Another Series"}, REQUEST | {"title": None},
    REQUEST | {"title": " "}, REQUEST | {"title": True},
])
def test_conflicting_or_invalid_current_names_do_not_authorize_a_subtitle_alias(other):
    resolver = SeriesIdentityResolver()
    resolver.update_requests([REQUEST, other | {"media_key": "season:tmdb:74606:2"}])
    assert resolver.resolve([NATIVE], 74606) is not None
    assert resolver.subtitle_titles([NATIVE], 74606) == ()


def test_withdrawn_or_edited_snapshot_removes_old_localized_subtitle_identity():
    resolver = SeriesIdentityResolver()
    native = NATIVE | {"tmdbId": 74606}
    resolver.update_requests([REQUEST])
    assert resolver.subtitle_titles([native], 74606) == ("Irmão do Jorel",)
    resolver.update_requests([])
    assert resolver.resolve([native], 74606) is not None
    assert resolver.subtitle_titles([native], 74606) == ()
    resolver.update_requests([REQUEST | {"title": "Renamed Show"}])
    titles = resolver.subtitle_titles([native], 74606)
    assert titles == ("Renamed Show",)
    assert _pack_title(native | {"_homeserver_subtitle_titles": titles}) is None


@pytest.mark.parametrize("stem,expected", [
    (RELEASE, RELEASE),
    (RELEASE.replace("Jorel.S01", "Jorel.2014.S01"),
     RELEASE.replace("Jorel.S01", "Jorel.2014.S01")),
    (RELEASE.replace("Jorel.S01", "Jorel.2015.S01"), None),
    (RELEASE.replace("S01E01", "S01E02"), None),
    (RELEASE.replace("S01E01", "S02E01"), None),
    (RELEASE.replace("Irmao.do.Jorel", "Other.Show"), None),
    (RELEASE.replace("WEB-DL", "BluRay"), None),
])
def test_localized_subtitle_alias_preserves_native_year_episode_source_and_edition(stem, expected):
    context = NATIVE | {"_homeserver_subtitle_titles": ("Irmão do Jorel",)}
    assert _pack_title(context, stem) == expected


def test_localized_subtitle_alias_preserves_conflicting_editions_guard():
    context = NATIVE | {"_homeserver_subtitle_titles": ("Irmão do Jorel",)}
    assert _pack_title(
        context, RELEASE.replace("1080p", "EXTENDED.1080p"),
        pack_title="Irmao.do.Jorel.S01.UNRATED.1080p.WEB-DL",
    ) is None


def _approved_payload():
    return {"results": [{
        "id": 127, "status": 2,
        "media": {"tmdbId": 74606, "tvdbId": 293012, "externalServiceId": 124,
                  "mediaType": "tv"},
        "seasons": [{"seasonNumber": 1, "status": 2}, {"seasonNumber": 2, "status": 2}],
    }]}


@pytest.mark.asyncio
async def test_seerr_localized_title_is_authenticated_identity_checked_and_cached_across_seasons():
    metadata_reads = []

    def handle(request):
        if request.url.path == "/api/v1/request":
            return httpx.Response(200, json=_approved_payload())
        assert request.url.path == "/api/v1/tv/74606"
        assert request.url.params["language"] == "pt-BR"
        assert request.headers["X-Api-Key"] == "fixture"
        metadata_reads.append(request.url.path)
        return httpx.Response(200, json={"id": 74606, "name": "Irmão do Jorel"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client,
                              include_series_titles=True)
        first = await source.list_approved(1)
        assert await source.list_approved(1) == first
    assert [row["title"] for row in first] == ["Irmão do Jorel", "Irmão do Jorel"]
    assert metadata_reads == ["/api/v1/tv/74606"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status", [
    ({"id": 999, "name": "Other Show"}, 200),
    ({"id": "74606", "name": "Other Show"}, 200),
    ({"id": 74606.0, "name": "Other Show"}, 200),
    ({"id": True, "name": "Other Show"}, 200),
    ({"id": 74606, "name": None}, 200),
    ({"id": 74606, "name": " "}, 200),
    ({"id": 74606, "name": "Irmão do Jorel"}, 401),
    ({"id": 74606, "name": "Irmão do Jorel"}, 429),
    ({"id": 74606, "name": "Irmão do Jorel"}, 503),
])
async def test_unverified_or_unavailable_seerr_title_cannot_supply_an_alias(payload, status):
    metadata_reads = []

    def handle(request):
        if request.url.path == "/api/v1/request":
            return httpx.Response(200, json=_approved_payload())
        metadata_reads.append(request.url.path)
        return httpx.Response(status, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        source = SeerrAdapter(base_url="http://seerr", api_key="fixture", client=client,
                              include_series_titles=True)
        first = await source.list_approved(1)
        assert await source.list_approved(1) == first
    assert len(first) == 2 and all("title" not in row for row in first)
    assert metadata_reads == ["/api/v1/tv/74606"]
