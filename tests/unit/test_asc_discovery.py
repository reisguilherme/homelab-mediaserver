"""Configured private searches keep native identity, fallback and HTTP boundaries intact."""

from copy import deepcopy

import httpx
import pytest

from homeserver_control.worker.dubbed_discovery import DubbedDiscovery
from homeserver_control.worker.release_quality import ReleasePolicy

MEDIA = {
    "id": 401,
    "tmdbId": 990001,
    "title": "Fixture Feature",
    "originalTitle": "Fixture Feature",
    "originalLanguage": {"name": "English"},
    "year": 2025,
}
QUALITY = {
    "quality": {"name": "WEBDL-1080p", "source": "webdl", "resolution": 1080}
}
PUBLIC_INDEXERS = [
    {"id": 6, "name": "UIndex", "enable": True},
    {"id": 5, "name": "1337x", "enable": True},
]
ASC_INDEXER = {"id": 9, "name": "Amigos Share Club", "enable": True}


def _policy(sources="amigosshare,uindex,1337x"):
    return ReleasePolicy.from_environment({
        "HOMESERVER_RELEASE_INDEXER_PRIORITY": sources,
    }).for_profile("Dublado")


def _release(*, marker="Dual-Audio Brazilian Original", **extra):
    return {
        "title": f"Fixture.Feature.2025.1080p.WEB-DL {marker}",
        "guid": "asc-fixture",
        "indexer": "Amigos Share Club",
        "indexerId": 9,
        "infoHash": "a" * 40,
        "size": 2_000_000_000,
        "seeders": 20,
        "protocol": "torrent",
        "downloadUrl": "http://prowlarr:9696/9/download?link=fixture",
        "infoUrl": "https://amigos-share.club/details.php?id=fixture",
        "publishDate": "2026-01-01T00:00:00Z",
        "quality": deepcopy(QUALITY),
        "rejected": False,
        "rejections": [],
        **extra,
    }


class _FixtureAPI:
    """Only HTTP is replaced: discovery, parsing validation and ranking remain real."""

    def __init__(
        self, *, indexers=None, rows=None, statuses=None, season=None, matched=None,
        parse_year=2025,
    ):
        self.indexers = (
            [*PUBLIC_INDEXERS, ASC_INDEXER] if indexers is None else indexers
        )
        self.rows = rows or {}
        self.statuses = statuses or []
        self.season = season
        self.matched = MEDIA if matched is None else matched
        self.parse_year = parse_year
        self.searches = []
        self.parses = []
        self.details = []

    def __call__(self, request):
        assert request.method == "GET"
        path = request.url.path
        if request.url.host == "prowlarr":
            assert request.headers["X-Api-Key"] == "fixture-prowlarr-key"
            if path == "/api/v1/indexer":
                return httpx.Response(200, json=self.indexers)
            if path == "/api/v1/indexerstatus":
                return httpx.Response(200, json=self.statuses)
            if path == "/api/v1/search":
                indexer = request.url.params["indexerIds"]
                self.searches.append((indexer, request.url.params["query"]))
                return httpx.Response(200, json=self.rows.get(indexer, []))
        if request.url.host == "radarr":
            assert request.headers["X-Api-Key"] == "fixture-arr-key"
            if path == "/api/v3/indexer":
                return httpx.Response(200, json=[
                    {"id": 16, "name": "UIndex (Prowlarr)"},
                    {"id": 15, "name": "1337x (Prowlarr)"},
                    {"id": 19, "name": "Amigos Share Club (Prowlarr)"},
                ])
            if path == "/api/v3/parse":
                title = request.url.params["title"]
                self.parses.append(title)
                detail = {
                    "quality": QUALITY,
                    "languages": [{"name": "English" if "ENGLISH" in title else "Unknown"}],
                }
                if self.season is None:
                    return httpx.Response(200, json={
                        "movie": self.matched,
                        "languages": [{"name": "English"}],
                        "parsedMovieInfo": {**detail, "year": self.parse_year},
                    })
                return httpx.Response(200, json={
                    "series": self.matched,
                    "episodes": [{
                        "id": 77, "seriesId": 401, "seasonNumber": self.season,
                        "episodeNumber": 1,
                    }],
                    "parsedEpisodeInfo": {**detail, "seasonNumber": self.season},
                })
        if request.url.host == "uindex.org":
            assert "X-Api-Key" not in request.headers
            assert "Authorization" not in request.headers
            assert "Cookie" not in request.headers
            self.details.append(str(request.url))
            return httpx.Response(200, text="Audio: English + Portuguese (Brazil)")
        raise AssertionError(f"Unexpected or private detail request: {request.url}")


async def _enrich(discovery, native=(), *, policy=None, media=None, season=None):
    return await discovery.enrich(
        MEDIA if media is None else media, list(native), arr_url="http://radarr:7878",
        arr_headers={"X-Api-Key": "fixture-arr-key"}, season=season,
        release_policy=_policy() if policy is None else policy,
    )


def _discovery(client, **options):
    return DubbedDiscovery(
        client=client, prowlarr_url="http://prowlarr:9696",
        prowlarr_api_key="fixture-prowlarr-key", interval_seconds=0,
        **options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("season", [None, 2])
@pytest.mark.parametrize("marker,tier,languages", [
    ("Dual-Audio Brazilian Original", 3, ["en", "pt-br"]),
    ("Dublado Brazilian", None, ["pt-br"]),
])
async def test_configured_asc_candidates_keep_native_identity_without_private_details(
    season, marker, tier, languages,
):
    candidate = _release(marker=marker)
    media = {**MEDIA, "tvdbId": 1234} if season is not None else MEDIA
    api = _FixtureAPI(rows={"9": [candidate]}, season=season, matched=media)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        client.cookies.set("asc_session", "fixture-cookie", domain="amigos-share.club")
        discovery = _discovery(client, max_queries=30, max_descriptions=0)
        result = await _enrich(discovery, media=media, season=season)
    assert result.complete
    assert len(result.releases) == 1
    selected = result.releases[0]
    assert selected["indexerId"] == 19
    assert selected["indexer"] == "Amigos Share Club (Prowlarr)"
    assert selected["requiresNativeDecision"] is True
    assert {"source": "title", "languages": languages} in selected["audioEvidence"]
    rank = _policy().rank(selected, original_language="en")
    if tier is None:
        assert rank is None
    else:
        assert rank[0] == tier
    if season is None:
        assert selected["movieId"] == 401
    else:
        assert selected["seriesId"] == 401
        assert selected["seasonNumber"] == 2
        assert selected["episodeIds"] == [77]
    assert api.searches[0][0] == "9"
    assert {indexer for indexer, _ in api.searches} == {"9", "6", "5"}
    assert api.details == []


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", ["missing", "disabled", "blocked"])
async def test_configured_unavailable_asc_prevents_original_fallback(unavailable):
    indexers = list(PUBLIC_INDEXERS)
    if unavailable != "missing":
        indexers.append({**ASC_INDEXER, "enable": unavailable != "disabled"})
    statuses = [{"indexerId": 9, "disabledTill": "2099-01-01T00:00:00Z"}]
    api = _FixtureAPI(indexers=indexers, statuses=statuses if unavailable == "blocked" else [])
    original = _release(
        marker="ENGLISH", indexer="UIndex (Prowlarr)", indexerId=16,
        infoUrl=None, guid="original",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        result = await _enrich(_discovery(client, max_queries=30), [original])
    assert not result.complete
    assert {indexer for indexer, _ in api.searches} == {"6", "5"}
    assert _policy().rank(result.releases[0], original_language="en")[0] == 1


@pytest.mark.asyncio
async def test_unconfigured_asc_is_ignored_and_public_search_still_completes():
    api = _FixtureAPI(rows={"9": [_release()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        result = await _enrich(
            _discovery(client, max_queries=30), policy=_policy("uindex,1337x"),
        )
    assert result.complete
    assert result.releases == []
    assert {indexer for indexer, _ in api.searches} == {"6", "5"}
    assert api.parses == []


@pytest.mark.asyncio
async def test_only_configured_public_source_is_required_to_finish_search():
    api = _FixtureAPI(indexers=[PUBLIC_INDEXERS[0], ASC_INDEXER])
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        result = await _enrich(_discovery(client), policy=_policy("uindex"))
    assert result.complete
    assert result.releases == []
    assert {indexer for indexer, _ in api.searches} == {"6"}


@pytest.mark.asyncio
async def test_budget_rotation_finishes_all_configured_searches_before_original_fallback():
    api = _FixtureAPI()
    original = _release(
        marker="ENGLISH", indexer="UIndex (Prowlarr)", indexerId=16,
        infoUrl=None, guid="original",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        discovery = _discovery(client, max_queries=3)
        first = await _enrich(discovery, [original])
        assert not first.complete
        assert len(api.searches) == 3
        assert api.searches[0][0] == "9"
        for _ in range(6):
            result = await _enrich(discovery, [original])
            if result.complete:
                break
    assert result.complete
    assert len(api.searches) == len(set(api.searches)) == 15
    assert {indexer for indexer, _ in api.searches} == {"9", "6", "5"}
    assert _policy().rank(result.releases[0], original_language="en")[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["id", "tmdbId", "year"])
async def test_supplemental_asc_cannot_bypass_strict_media_identity(mismatch):
    matched = {**MEDIA, **{mismatch: MEDIA[mismatch] + 1}} if mismatch != "year" else MEDIA
    api = _FixtureAPI(
        rows={"9": [_release()]}, matched=matched,
        parse_year=2024 if mismatch == "year" else 2025,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        result = await _enrich(_discovery(client, max_queries=30))
    assert result.complete
    assert result.releases == []
    assert len(api.parses) == 1
    assert api.details == []


@pytest.mark.asyncio
async def test_private_candidates_preserve_public_description_budget_and_credentials():
    private = _release()
    public = _release(
        marker="Unknown Audio", indexer="UIndex", indexerId=6, guid="public",
        infoHash="b" * 40, downloadUrl="http://prowlarr:9696/6/download?link=public",
        infoUrl="https://uindex.org/details.php?id=fixture",
    )
    api = _FixtureAPI(rows={"9": [private], "6": [public]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        client.cookies.set("asc_session", "fixture-cookie", domain="amigos-share.club")
        result = await _enrich(_discovery(client, max_queries=30, max_descriptions=1))
    assert result.complete
    assert len(result.releases) == 2
    public_result = next(row for row in result.releases if row["guid"] == "public")
    assert {"source": "description", "languages": ["en", "pt-br"]} in public_result[
        "audioEvidence"
    ]
    assert api.details == ["https://uindex.org/details.php?id=fixture"]


@pytest.mark.asyncio
async def test_supplemental_asc_duplicate_cannot_erase_native_rejection():
    candidate = _release()
    native = {**candidate, "rejected": True, "rejections": ["Release is blocklisted"]}
    api = _FixtureAPI(rows={"9": [candidate]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        result = await _enrich(_discovery(client, max_queries=30), [native])
    assert result.complete
    assert len(result.releases) == 1
    assert result.releases[0]["rejected"] is True
    assert result.releases[0]["rejections"] == ["Release is blocklisted"]
    assert "requiresNativeDecision" not in result.releases[0]
    assert api.parses == []
