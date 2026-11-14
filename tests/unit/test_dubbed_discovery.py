"""Discovery preserves Arr decisions and does not mistake failed searches for absence."""

import asyncio

import httpx
import pytest

from homeserver_control.worker.dubbed_discovery import DubbedDiscovery, search_queries
from homeserver_control.worker.release_quality import ReleasePolicy

MEDIA = {
    "id": 1,
    "tmdbId": 862,
    "title": "Toy Story",
    "originalTitle": "Toy Story",
    "year": 1995,
    "alternateTitles": [
        {"title": "Um Mundo de Aventuras", "language": {"name": "Portuguese (Brazil)"}}
    ],
}


def test_queries_include_brazilian_alias_and_audio_variants_without_episode_loss():
    queries = search_queries(MEDIA)
    assert "Um Mundo de Aventuras" in queries
    assert any("DUBLADO" in value for value in queries)
    assert any("PTBR" in value for value in queries)
    assert any("DUAL" in value for value in queries)
    assert all("S02" in value for value in search_queries(MEDIA, season=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["movie", "tv"])
async def test_worker_discovers_release_only_under_seerr_brazilian_title(
    tmp_path, monkeypatch, kind
):
    """Real worker wiring must supply the title missing from Arr's unlabelled aliases."""
    from copy import deepcopy

    import homeserver_control.worker.__main__ as worker

    tmdb_id = 519182 if kind == "movie" else 101
    localized = "Meu Malvado Favorito 4" if kind == "movie" else "O Programa"
    season = None if kind == "movie" else 2
    media = {
        "id": 204,
        "tmdbId": tmdb_id,
        "title": "Despicable Me 4" if kind == "movie" else "Fixture Show",
        "originalTitle": "Despicable Me 4" if kind == "movie" else "Original Show",
        "year": 2024,
        "originalLanguage": {"name": "English"},
        "alternateTitles": [{
            "id": 13, "title": localized, "sourceType": "tmdb", "movieMetadataId": 204,
        }],
    }
    original = deepcopy(media)
    candidate = release(title=localized + (
        ".2024.1080p.BluRay.PTBR.ENG" if season is None else ".S02E01.1080p.BluRay.PTBR.ENG"
    ))
    parse = parsed(media)
    parse["parsedMovieInfo"]["year"] = 2024
    if season is not None:
        media["tvdbId"] = 1234
        original = deepcopy(media)
        parse = {
            "series": media,
            "episodes": [{"id": 7, "seriesId": 204, "seasonNumber": 2, "episodeNumber": 1}],
            "parsedEpisodeInfo": {
                "seasonNumber": 2, "languages": [{"name": "Unknown"}],
                "quality": candidate["quality"],
            },
        }
    queries, metadata = [], []
    base = handler_for([candidate], parse=parse)

    def handler(request):
        if request.url.host == "seerr":
            assert request.url.path == f"/api/v1/{kind}/{tmdb_id}"
            assert request.url.params["language"] == "pt-BR"
            assert request.headers["X-Api-Key"] == "seerr-key"
            metadata.append(request)
            return httpx.Response(200, json={
                "id": tmdb_id, "title" if kind == "movie" else "name": localized,
            })
        assert request.headers.get("X-Api-Key") != "seerr-key"
        if request.url.path == "/api/v1/search":
            queries.append(request.url.params["query"])
            if localized not in request.url.params["query"]:
                return httpx.Response(200, json=[])
        return base(request)

    for key, value in {
        "SEERR_URL": "http://seerr:5055", "SEERR_API_KEY": "seerr-key",
        "RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "radarr-key",
        "SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": "sonarr-key",
        "PROWLARR_URL": "http://prowlarr:9696", "PROWLARR_API_KEY": "prowlarr-key",
        "CAPACITY_SNAPSHOT": str(tmp_path / "capacity.json"),
        "RECOVERY_MODE": str(tmp_path / "RECOVERY_MODE"),
    }.items():
        monkeypatch.setenv("HOMESERVER_" + key, value)
    clients = []
    original_client = httpx.AsyncClient

    def client_factory(**kwargs):
        client = original_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(worker.httpx, "AsyncClient", client_factory)
    try:
        cycle = worker._build_cycle(tmp_path / "control.sqlite")
        acquirer = cycle.acquirer if season is None else cycle.series_acquirer
        discovery = acquirer.dubbed_discovery
        discovery.interval_seconds = 0
        result = await discovery.enrich(
            media, [], arr_url="http://radarr:7878" if season is None else "http://sonarr:8989",
            arr_headers={"X-Api-Key": "arr-key"}, season=season,
        )
        assert localized + ("" if season is None else " S02") in queries
        assert len(result.releases) == 1
        result = await discovery.enrich(
            media, [], arr_url="http://radarr:7878" if season is None else "http://sonarr:8989",
            arr_headers={"X-Api-Key": "arr-key"}, season=season,
        )
        assert result.complete
        assert result.releases[0]["requiresNativeDecision"] is True
        assert result.releases[0]["audioEvidence"] == [
            {"source": "arr-parser", "languages": [{"name": "Unknown"}]},
        ]
        assert len(metadata) == 1
        assert len(queries) <= 14
        assert media == original
    finally:
        for client in clients:
            await client.aclose()


def release(**extra):
    return {
        "title": "Toy.Story.1995.1080p.BluRay.DUAL.PTBR.ENG",
        "guid": "row",
        "indexer": "UIndex",
        "indexerId": 6,
        "infoHash": "a" * 40,
        "size": 2_000_000_000,
        "seeders": 20,
        "protocol": "torrent",
        "downloadUrl": "http://prowlarr:9696/6/download?link=row",
        "publishDate": "2026-01-01T00:00:00Z",
        "rejected": False,
        "quality": {
            "quality": {
                "name": "Bluray-1080p",
                "source": "bluray",
                "resolution": 1080,
            }
        },
        **extra,
    }


def parsed(movie=MEDIA):
    return {
        "movie": movie,
        "languages": [{"name": "English"}],
        "parsedMovieInfo": {
            "year": 1995,
            "languages": [{"name": "Unknown"}],
            "quality": {
                "quality": {"name": "Bluray-1080p", "source": "bluray", "resolution": 1080}
            },
        },
    }


def handler_for(rows, *, blocked=False, parse=None):
    def handler(request):
        path = request.url.path
        if path == "/api/v1/indexer":
            return httpx.Response(
                200,
                json=[
                    {"id": 6, "name": "UIndex", "enable": True},
                    {"id": 5, "name": "1337x", "enable": True},
                ],
            )
        if path == "/api/v1/indexerstatus":
            return httpx.Response(
                200,
                json=[{"indexerId": 6, "disabledTill": "2099-01-01T00:00:00Z"}] if blocked else [],
            )
        if path == "/api/v1/search":
            return httpx.Response(200, json=rows if request.url.params["indexerIds"] == "6" else [])
        if path == "/api/v3/parse":
            return httpx.Response(200, json=parsed() if parse is None else parse)
        if path == "/api/v3/indexer":
            return httpx.Response(
                200,
                json=[
                    {"id": 16, "name": "UIndex (Prowlarr)"},
                    {"id": 15, "name": "1337x (Prowlarr)"},
                ],
            )
        raise AssertionError(str(request.url))

    return handler


def test_localized_queries_keep_native_original_and_hyphenated_audio_marker():
    media = {**MEDIA, "title": "Native Title", "originalTitle": "Original Title"}
    queries = search_queries(media, localized_title="  Brazilian Title  ", season=2)
    assert queries == [
        "Native Title S02", "Original Title S02", "Brazilian Title S02",
        "Native Title S02 DUAL", "Brazilian Title S02 DUBLADO",
        "Native Title S02 PTBR", "Native Title S02 PT-BR",
    ]


@pytest.mark.asyncio
async def test_query_budget_finishes_more_than_twelve_queries_using_cached_results():
    media = {**MEDIA, "title": "Native Title", "originalTitle": "Original Title"}
    searches = []
    base = handler_for([])

    def handler(request):
        if request.url.path == "/api/v1/search":
            searches.append((request.url.params["indexerIds"], request.url.params["query"]))
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696", prowlarr_api_key="private",
            interval_seconds=0, max_queries=12,
        )
        first = await discovery.enrich(media, [], arr_url="http://radarr:7878", arr_headers={})
        assert not first.complete
        assert len(searches) == 12
        second = await discovery.enrich(media, [], arr_url="http://radarr:7878", arr_headers={})
        assert second.complete
        assert len(searches) == len(set(searches)) == 14
        assert ("5", "Native Title PT-BR") in searches


@pytest.mark.asyncio
async def test_localized_metadata_cache_separates_types_and_reuses_across_seasons(monkeypatch):
    from homeserver_control.worker import dubbed_discovery

    clock = [1000.0]
    monkeypatch.setattr(dubbed_discovery.time, "monotonic", lambda: clock[0])
    metadata, queries = [], []
    media = {**MEDIA, "alternateTitles": []}
    base = handler_for([])

    def handler(request):
        if request.url.host == "seerr":
            metadata.append(request.url.path)
            return httpx.Response(200, json={
                "id": 862, "title": "O Filme", "name": "O Programa",
            })
        if request.url.path == "/api/v1/search":
            queries.append(request.url.params["query"])
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696", prowlarr_api_key="private",
            seerr_url="http://seerr:5055", seerr_api_key="seerr-key", interval_seconds=0,
        )
        for season in (None, 1, 2, None):
            result = await discovery.enrich(
                media, [], arr_url="http://arr", arr_headers={}, season=season,
            )
            assert result.complete
        assert metadata == ["/api/v1/movie/862", "/api/v1/tv/862"]
        assert "O Filme" in queries
        assert "O Programa S01" in queries and "O Programa S02" in queries
        clock[0] += 43201
        await discovery.enrich(media, [], arr_url="http://arr", arr_headers={})
        assert metadata == ["/api/v1/movie/862", "/api/v1/tv/862", "/api/v1/movie/862"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["http", "wrong_id", "bool_id", "empty_title", "wrong_field"])
async def test_localized_metadata_failure_is_incomplete_and_can_recover(failure):
    metadata, queries = [], []
    media = {**MEDIA, "alternateTitles": []}
    base = handler_for([])
    bad_payload = {
        "wrong_id": {"id": 863, "title": "Wrong Movie"},
        "bool_id": {"id": True, "title": "Wrong Movie"},
        "empty_title": {"id": 862, "title": "   "},
        "wrong_field": {"id": 862, "name": "Wrong Movie"},
    }

    def handler(request):
        if request.url.host == "seerr":
            metadata.append(request)
            if len(metadata) == 1:
                return httpx.Response(503) if failure == "http" else httpx.Response(
                    200, json=bad_payload[failure],
                )
            return httpx.Response(200, json={"id": 862, "title": "Toy Story"})
        if request.url.path == "/api/v1/search":
            queries.append(request.url.params["query"])
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696", prowlarr_api_key="private",
            seerr_url="http://seerr:5055", seerr_api_key="seerr-key", interval_seconds=0,
        )
        first = await discovery.enrich(media, [], arr_url="http://arr", arr_headers={})
        assert not first.complete
        assert "Toy Story" in queries
        second = await discovery.enrich(media, [], arr_url="http://arr", arr_headers={})
        assert second.complete
        assert len(metadata) == 2


@pytest.mark.asyncio
async def test_localized_title_equal_to_original_deduplicates_and_allows_original_fallback():
    media = {**MEDIA, "alternateTitles": []}
    native = release(title="Toy.Story.1995.1080p.BluRay.ENGLISH")
    base = handler_for([])
    queries = []

    def handler(request):
        if request.url.host == "seerr":
            return httpx.Response(200, json={"id": 862, "title": "  Toy Story  "})
        if request.url.path == "/api/v1/search":
            queries.append((request.url.params["indexerIds"], request.url.params["query"]))
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696", prowlarr_api_key="private",
            seerr_url="http://seerr:5055", seerr_api_key="seerr-key", interval_seconds=0,
        )
        result = await discovery.enrich(media, [native], arr_url="http://arr", arr_headers={})
    assert result.complete
    assert len(queries) == len(set(queries)) == 10
    assert result.releases[0]["guid"] == native["guid"]
    assert result.releases[0]["audioEvidence"] == [
        {"source": "arr-parser", "languages": [{"name": "Unknown"}]},
    ]


@pytest.mark.asyncio
async def test_localized_lookup_timeout_leaves_time_for_base_search():
    queries = []
    base = handler_for([])

    async def handler(request):
        if request.url.host == "seerr":
            await asyncio.Event().wait()
        if request.url.path == "/api/v1/search":
            queries.append(request.url.params["query"])
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696", prowlarr_api_key="private",
            seerr_url="http://seerr:5055", seerr_api_key="seerr-key", interval_seconds=0,
            localized_title_timeout_seconds=0.01, total_timeout_seconds=0.3,
        )
        result = await asyncio.wait_for(discovery.enrich(
            MEDIA, [], arr_url="http://arr", arr_headers={},
        ), timeout=0.5)
    assert not result.complete
    assert "Toy Story" in queries


@pytest.mark.asyncio
async def test_localized_lookup_is_bounded_by_the_shorter_total_deadline():
    metadata_timeouts = []
    base = handler_for([])

    async def handler(request):
        if request.url.host == "seerr":
            metadata_timeouts.append(request.extensions["timeout"]["read"])
            await asyncio.Event().wait()
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696", prowlarr_api_key="private",
            seerr_url="http://seerr:5055", seerr_api_key="seerr-key", interval_seconds=0,
            localized_title_timeout_seconds=1, total_timeout_seconds=0.03,
        )
        result = await asyncio.wait_for(discovery.enrich(
            MEDIA, [], arr_url="http://arr", arr_headers={},
        ), timeout=0.2)
    assert not result.complete
    assert len(metadata_timeouts) == 1 and 0 < metadata_timeouts[0] <= 0.02


@pytest.mark.asyncio
async def test_other_category_candidate_requires_native_decision_and_explicit_audio():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler_for([release(categories=[{"id": 8000}])]))
    ) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(
            MEDIA, [], arr_url="http://radarr:7878", arr_headers={"X-Api-Key": "radarr"}
        )
    assert result.complete
    assert len(result.releases) == 1
    row = result.releases[0]
    assert row["requiresNativeDecision"] is True
    assert row["indexerId"] == 16
    assert row["audioLanguagesInferred"] is True
    assert row["audioEvidence"] == [{"source": "arr-parser", "languages": [{"name": "Unknown"}]}]


@pytest.mark.asyncio
async def test_hash_dedup_cannot_override_native_rejection():
    native = release(rejected=True, rejections=["Release is blocklisted"])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler_for([release(guid="duplicate")]))
    ) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(
            MEDIA, [native], arr_url="http://radarr:7878", arr_headers={}
        )
    assert len(result.releases) == 1
    assert result.releases[0]["rejected"] is True
    assert result.releases[0]["rejections"] == ["Release is blocklisted"]


@pytest.mark.asyncio
async def test_wrong_movie_same_title_is_not_admitted():
    other = {**MEDIA, "id": 2, "tmdbId": 863}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler_for([release()], parse=parsed(other)))
    ) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
    assert result.releases == []


@pytest.mark.asyncio
async def test_indexer_backoff_is_incomplete_without_querying_blocked_source():
    calls = []
    base = handler_for([], blocked=True)

    def handler(request):
        if request.url.path == "/api/v1/search":
            calls.append(request.url.params["indexerIds"])
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
    assert not result.complete
    assert "6" not in calls


@pytest.mark.asyncio
async def test_repeated_search_uses_cached_queries_and_never_posts():
    searches = []
    base = handler_for([release()])

    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/api/v1/search":
            searches.append(str(request.url))
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        first = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
        count = len(searches)
        second = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
    assert first.releases == second.releases
    assert len(searches) == count


@pytest.mark.asyncio
async def test_description_audio_enriches_native_without_leaking_api_key():
    native = release(
        title="Toy.Story.1995.1080p.BluRay.MULTI",
        rejected=False,
        infoUrl="https://uindex.org/details.php?id=1",
    )
    base = handler_for([])

    def handler(request):
        if request.url.host == "uindex.org":
            assert "X-Api-Key" not in request.headers
            return httpx.Response(
                200, text="<p>Audio: Portuguese (Brazil) + English</p><p>Subtitles: French</p>"
            )
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(
            MEDIA, [native], arr_url="http://radarr:7878", arr_headers={"X-Api-Key": "radarr"}
        )
    assert result.releases[0]["audioEvidence"][-1] == {
        "source": "description",
        "languages": ["en", "pt-br"],
    }


@pytest.mark.asyncio
async def test_description_cannot_follow_redirect_to_private_service():
    calls = []
    base = handler_for([])

    def handler(request):
        if request.url.host == "uindex.org":
            calls.append(str(request.url))
            return httpx.Response(
                302, headers={"Location": "http://radarr:7878/api/v3/system/status"}
            )
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(
            MEDIA,
            [release(infoUrl="https://uindex.org/details.php?id=1")],
            arr_url="http://radarr:7878",
            arr_headers={},
        )
    assert len(calls) == 1
    assert not result.complete


@pytest.mark.asyncio
async def test_successful_empty_search_with_new_indexer_backoff_is_incomplete():
    searches = []
    base = handler_for([])

    def handler(request):
        if request.url.path == "/api/v1/search":
            searches.append(request.url.params["indexerIds"])
        if request.url.path == "/api/v1/indexerstatus" and "6" in searches:
            return httpx.Response(
                200,
                json=[
                    {
                        "indexerId": 6,
                        "disabledTill": "2099-01-01T00:00:00Z",
                    }
                ],
            )
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
    assert not result.complete
    assert searches.count("6") == 1


@pytest.mark.asyncio
async def test_byparr_cannot_supply_audio_from_redirected_private_page():
    base = handler_for([])

    def handler(request):
        if request.url.host == "uindex.org":
            return httpx.Response(403)
        if request.url.host == "byparr":
            return httpx.Response(
                200,
                json={
                    "solution": {
                        "status": 200,
                        "url": "http://radarr:7878/private",
                        "response": "Audio: Portuguese (Brazil) + English",
                    }
                },
            )
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
            byparr_url="http://byparr:8191",
        )
        result = await discovery.enrich(
            MEDIA,
            [release(infoUrl="https://uindex.org/details.php?id=1")],
            arr_url="http://radarr:7878",
            arr_headers={},
        )
    assert not result.complete
    assert all(item["source"] != "description" for item in result.releases[0]["audioEvidence"])


@pytest.mark.asyncio
async def test_explicit_brazilian_candidate_is_not_starved_by_many_multi_titles():
    native = [
        release(
            title=f"Toy.Story.1995.1080p.BluRay.MULTI-{number}",
            guid=str(number),
            infoHash=f"{number:040x}",
            seeders=500,
            rejected=False,
            downloadUrl=f"http://prowlarr:9696/6/download?link={number}",
        )
        for number in range(30)
    ]
    brazilian = release()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for([brazilian]))) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
            max_candidates=1,
        )
        result = await discovery.enrich(MEDIA, native, arr_url="http://radarr:7878", arr_headers={})
    assert any(row.get("infoHash") == "a" * 40 for row in result.releases)
    assert not result.complete


@pytest.mark.asyncio
async def test_parse_budget_progresses_over_cached_candidates_until_search_complete():
    native = [
        release(
            title=f"Toy.Story.1995.1080p.BluRay.ENGLISH-{number}",
            guid=str(number),
            infoHash=f"{number:040x}",
            rejected=False,
        )
        for number in range(2)
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler_for([]))) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
            max_candidates=1,
        )
        first = await discovery.enrich(MEDIA, native, arr_url="http://radarr:7878", arr_headers={})
        second = await discovery.enrich(MEDIA, native, arr_url="http://radarr:7878", arr_headers={})
    assert not first.complete
    assert second.complete


@pytest.mark.asyncio
async def test_empty_response_during_backoff_is_not_reused_after_recovery(monkeypatch):
    from homeserver_control.worker import dubbed_discovery

    clock = [1000.0]
    monkeypatch.setattr(dubbed_discovery.time, "monotonic", lambda: clock[0])
    failed = [True]
    queries = []
    base = handler_for([])

    def handler(request):
        if request.url.path == "/api/v1/indexerstatus":
            return httpx.Response(
                200,
                json=[
                    {
                        "indexerId": 6,
                        "disabledTill": "2099-01-01T00:00:00Z",
                    }
                ]
                if failed[0] and queries
                else [],
            )
        if request.url.path == "/api/v1/search" and request.url.params["indexerIds"] == "6":
            queries.append(request.url.params["query"])
            return httpx.Response(
                200,
                json=[release()]
                if not failed[0] and request.url.params["query"] == "Toy Story"
                else [],
            )
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        first = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
        failed[0] = False
        clock[0] += 31
        second = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
    assert not first.complete
    assert second.complete
    assert queries.count("Toy Story") == 2
    assert len(second.releases) == 1


@pytest.mark.asyncio
async def test_total_deadline_returns_partial_without_blocking_forever():
    base = handler_for([])

    async def handler(request):
        if request.url.path == "/api/v1/search":
            await asyncio.Event().wait()
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
            total_timeout_seconds=0.05,
            query_timeout_seconds=1,
        )
        result = await asyncio.wait_for(
            discovery.enrich(
                MEDIA, [release(rejected=False)], arr_url="http://radarr:7878", arr_headers={}
            ),
            timeout=0.5,
        )
    assert not result.complete
    assert len(result.releases) == 1
    assert result.releases[0]["audioLanguagesInferred"] is True


@pytest.mark.asyncio
async def test_slow_first_query_does_not_starve_later_queries_or_lose_cache():
    base = handler_for([release()])
    slow_queries = []

    async def handler(request):
        if (
            request.url.path == "/api/v1/search"
            and request.url.params["query"] == "Toy Story"
            and request.url.params["indexerIds"] == "6"
        ):
            slow_queries.append(str(request.url))
            await asyncio.Event().wait()
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
            total_timeout_seconds=1,
            query_timeout_seconds=0.01,
        )
        first = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
        second = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
    assert not first.complete and not second.complete
    assert len(first.releases) == len(second.releases) == 1
    assert len(slow_queries) == 1


@pytest.mark.asyncio
async def test_slow_description_preserves_parsed_audio_and_is_cooled_down():
    base = handler_for([])
    descriptions = []

    async def handler(request):
        if request.url.host == "uindex.org":
            descriptions.append(str(request.url))
            await asyncio.Event().wait()
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
            total_timeout_seconds=1,
            description_timeout_seconds=0.05,
            description_runtime_seconds=0.01,
        )
        native = [release(infoUrl="https://uindex.org/details.php?id=1", rejected=False)]
        first = await discovery.enrich(MEDIA, native, arr_url="http://radarr:7878", arr_headers={})
        second = await discovery.enrich(MEDIA, native, arr_url="http://radarr:7878", arr_headers={})
        await discovery.aclose()
    assert not first.complete and not second.complete
    assert first.releases[0]["audioEvidence"] == second.releases[0]["audioEvidence"]
    assert len(descriptions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ineligible",
    [
        {"rejected": True, "rejections": ["Release is blocklisted"]},
        {"quality": {"quality": {"source": "bluray", "resolution": 2160}}},
        {"quality": {"quality": {"source": "bluray", "resolution": 1080, "modifier": "remux"}}},
        {"seeders": 4},
        {"size": 0},
    ],
)
async def test_irrelevant_native_release_cannot_block_valid_original_fallback(ineligible):
    good = release(title="Toy.Story.1995.1080p.BluRay.ENGLISH", guid="good")
    bad = release(
        title="Toy.Story.1995.DUAL.PTBR.Bad",
        guid="bad",
        infoHash="b" * 40,
        downloadUrl="http://prowlarr:9696/6/download?link=bad",
        infoUrl="https://uindex.org/details.php?id=2",
        **ineligible,
    )
    base = handler_for([])
    inspected = []

    def handler(request):
        if request.url.path == "/api/v3/parse":
            inspected.append(request.url.params["title"])
            return httpx.Response(
                200,
                json={
                    **parsed(),
                    "parsedMovieInfo": {
                        **parsed()["parsedMovieInfo"],
                        "languages": [{"name": "English"}],
                    },
                },
            )
        if request.url.host == "uindex.org":
            return httpx.Response(403)
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
            max_candidates=1,
        )
        result = await discovery.enrich(
            MEDIA, [bad, good], arr_url="http://radarr:7878", arr_headers={}
        )
    assert result.complete
    assert inspected == [good["title"]]
    matching = next(row for row in result.releases if row["guid"] == "good")
    assert (
        ReleasePolicy.from_environment({})
        .for_profile("Dublado")
        .rank(
            matching,
            original_language="en",
        )[0]
        == 1
    )


@pytest.mark.asyncio
async def test_discovery_uses_configured_seed_floor_without_needing_audio_first():
    low = release(title="Toy.Story.1995.1080p.BluRay.DUAL.PTBR", seeders=20)
    valid = release(
        title="Toy.Story.1995.1080p.BluRay.Unknown.Audio",
        seeders=50,
        guid="other",
        infoHash="b" * 40,
        downloadUrl="http://prowlarr:9696/6/download?link=other",
    )
    base = handler_for([])
    inspected = []

    def handler(request):
        if request.url.path == "/api/v3/parse":
            inspected.append(request.url.params["title"])
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        policy = ReleasePolicy.from_environment({"HOMESERVER_RELEASE_MIN_SEEDERS": "50"})
        result = await discovery.enrich(
            MEDIA,
            [low, valid],
            arr_url="http://radarr:7878",
            arr_headers={},
            release_policy=policy.for_profile("Dublado"),
        )
    assert result.complete
    assert inspected == [valid["title"]]


@pytest.mark.asyncio
async def test_supplemental_wrong_quality_never_spends_description_budget():
    candidate = release(infoUrl="https://uindex.org/details.php?id=3")
    data = parsed()
    data["parsedMovieInfo"]["quality"] = {"quality": {"source": "bluray", "resolution": 2160}}
    base = handler_for([candidate], parse=data)
    descriptions = []

    def handler(request):
        if request.url.host == "uindex.org":
            descriptions.append(str(request.url))
            return httpx.Response(403)
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
    assert result.complete
    assert result.releases == []
    assert descriptions == []


@pytest.mark.asyncio
async def test_low_seed_supplemental_results_need_no_native_lookup_or_description():
    base = handler_for([release(seeders=0, infoUrl="https://uindex.org/details.php?id=3")])

    def handler(request):
        if request.url.host == "radarr":
            raise AssertionError("No native request is needed for an already ineligible source")
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            interval_seconds=0,
        )
        result = await discovery.enrich(MEDIA, [], arr_url="http://radarr:7878", arr_headers={})
    assert result.complete
    assert result.releases == []


@pytest.mark.asyncio
@pytest.mark.parametrize("aggregate_deadline", [False, True])
async def test_byparr_survives_discovery_deadline_and_reuses_completed_evidence(aggregate_deadline):
    base = handler_for([])
    finish = asyncio.Event()
    finished = asyncio.Event()
    cancelled = asyncio.Event()
    solver_calls = []

    async def handler(request):
        if request.url.host == "uindex.org":
            return httpx.Response(403)
        if request.url.host == "byparr":
            solver_calls.append(request)
            try:
                await finish.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            finally:
                finished.set()
            return httpx.Response(
                200,
                json={
                    "solution": {
                        "status": 200,
                        "url": "https://uindex.org/details.php?id=1",
                        "response": "Audio: Portuguese (Brazil) + English",
                    }
                },
            )
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            byparr_url="http://byparr:8191",
            interval_seconds=0,
            total_timeout_seconds=0.03 if aggregate_deadline else 1,
            description_timeout_seconds=1 if aggregate_deadline else 0.01,
        )
        try:
            native = [release(infoUrl="https://uindex.org/details.php?id=1")]
            first = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={}
            )
            assert not first.complete
            assert len(solver_calls) == 1
            assert not cancelled.is_set()
            assert ("description", native[0]["infoUrl"]) not in discovery._cooldowns
            second = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={}
            )
            assert not second.complete
            assert len(solver_calls) == 1
            finish.set()
            await asyncio.wait_for(finished.wait(), timeout=0.3)
            third = await discovery.enrich(
                MEDIA, native, arr_url="http://radarr:7878", arr_headers={}
            )
            assert third.complete
            assert len(solver_calls) == 1
            assert {"source": "description", "languages": ["en", "pt-br"]} in third.releases[0][
                "audioEvidence"
            ]
        finally:
            finish.set()
            if hasattr(discovery, "aclose"):
                await discovery.aclose()


@pytest.mark.asyncio
async def test_simultaneous_description_waiters_share_one_request():
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.02)
        return httpx.Response(200, text="Audio: English")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            description_timeout_seconds=0.2,
        )
        try:
            results = await asyncio.gather(
                *[discovery._description("https://uindex.org/details.php?id=1") for _ in range(2)]
            )
            assert results == [{"en"}, {"en"}]
            assert len(calls) == 1
        finally:
            if hasattr(discovery, "aclose"):
                await discovery.aclose()


@pytest.mark.asyncio
async def test_description_runtime_expiry_cancels_request_and_applies_cooldown():
    calls = []
    cancelled = asyncio.Event()

    async def handler(request):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            description_timeout_seconds=0.2,
            description_runtime_seconds=0.02,
        )
        try:
            for _ in range(2):
                with pytest.raises(ValueError, match="timed out|deferred"):
                    await discovery._description("https://uindex.org/details.php?id=1")
            assert cancelled.is_set()
            assert len(calls) == 1
            assert not discovery._description_tasks
        finally:
            await discovery.aclose()


@pytest.mark.asyncio
async def test_description_concurrency_is_bounded_and_shutdown_cancels_pending_work():
    calls = []
    cancelled = asyncio.Event()

    async def handler(request):
        calls.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            description_timeout_seconds=0.01,
        )
        try:
            for number in (1, 2):
                with pytest.raises(ValueError):
                    await discovery._description(f"https://uindex.org/details.php?id={number}")
            assert len(calls) == 1
            assert not cancelled.is_set()
            await discovery.aclose()
            await discovery.aclose()
            assert cancelled.is_set()
            assert not discovery._description_tasks
            with pytest.raises(ValueError):
                await discovery._description("https://uindex.org/details.php?id=3")
            assert len(calls) == 1
        finally:
            if hasattr(discovery, "aclose"):
                await discovery.aclose()


@pytest.mark.asyncio
async def test_background_description_failure_is_consumed_and_cooled_down():
    finish = asyncio.Event()
    calls = []

    async def handler(request):
        calls.append(request)
        await finish.wait()
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="private",
            description_timeout_seconds=0.01,
        )
        try:
            with pytest.raises(ValueError):
                await discovery._description("https://uindex.org/details.php?id=1")
            pending = list(discovery._description_tasks.values())
            assert len(pending) == 1
            finish.set()
            await asyncio.wait(pending, timeout=0.3)
            await asyncio.sleep(0)
            assert not discovery._description_tasks
            with pytest.raises(ValueError, match="deferred"):
                await discovery._description("https://uindex.org/details.php?id=1")
            assert len(calls) == 1
            assert all(not task._log_traceback for task in pending)
        finally:
            finish.set()
            if hasattr(discovery, "aclose"):
                await discovery.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", ["movie", "series", "both"])
async def test_worker_shutdown_finishes_shared_description_before_closing_clients(configured):
    from types import SimpleNamespace

    from homeserver_control.worker.__main__ import _run_forever
    from homeserver_control.worker.acquisition import MovieAcquirer
    from homeserver_control.worker.series_acquisition import SeriesAcquirer

    cancelled = asyncio.Event()

    async def handler(_request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class ObservedClient(httpx.AsyncClient):
        async def aclose(self):
            assert cancelled.is_set(), "Discovery must finish before its HTTP client closes"
            await super().aclose()

    clients = [ObservedClient(transport=httpx.MockTransport(handler))]
    discovery = DubbedDiscovery(
        client=clients[0],
        prowlarr_url="http://prowlarr:9696",
        prowlarr_api_key="private",
        description_timeout_seconds=0.01,
    )
    cycle = SimpleNamespace(source=object(), acquirer=None, series_acquirer=None)
    if configured in {"movie", "both"}:
        cycle.acquirer = object.__new__(MovieAcquirer)
        cycle.acquirer.client = clients[0]
        cycle.acquirer.dubbed_discovery = discovery
    if configured in {"series", "both"}:
        if configured == "both":
            clients.append(ObservedClient(transport=httpx.MockTransport(handler)))
        cycle.series_acquirer = object.__new__(SeriesAcquirer)
        cycle.series_acquirer.client = clients[-1]
        cycle.series_acquirer.dubbed_discovery = discovery
    try:
        with pytest.raises(ValueError):
            await discovery._description("https://uindex.org/details.php?id=1")
        await _run_forever(cycle, should_run=lambda: False, interval=1)
        assert all(client.is_closed for client in clients)
        assert not discovery._description_tasks
    finally:
        await discovery.aclose()
        for client in clients:
            await client.aclose()
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source,cap,expected",
    [("UIndex", 100, "Toy Story 1995"), ("Amigos Share Club", 96, "tt0114709")],
)
async def test_capped_search_uses_identity_narrow_query_without_false_offset_pagination(
    source, cap, expected
):
    queries = []
    base = handler_for([], parse=parsed())

    def handler(request):
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=[{"id": 6, "name": source, "enable": True}])
        if request.url.path == "/api/v1/search":
            query = request.url.params["query"]
            queries.append(query)
            assert "offset" not in request.url.params
            rows = (
                [release(guid=f"row-{i}", infoHash=f"{i:040x}", indexer=source) for i in range(cap)]
                if query == "Toy Story"
                else []
            )
            return httpx.Response(200, json=rows)
        if request.url.path == "/api/v3/indexer":
            return httpx.Response(200, json=[{"id": 16, "name": source + " (Prowlarr)"}])
        return base(request)

    policy = ReleasePolicy(
        profile_name="Dublado", indexer_priority=("amigosshare" if cap == 96 else "uindex",)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr",
            prowlarr_api_key="key",
            interval_seconds=0,
            max_candidates=1,
            max_queries=30,
        )
        result = await discovery.enrich(
            {**MEDIA, "imdbId": "tt0114709"},
            [],
            arr_url="http://radarr",
            arr_headers={},
            release_policy=policy,
        )
    assert expected in queries
    assert not result.complete
    assert result.truncated_queries


@pytest.mark.asyncio
async def test_capped_identity_narrow_query_remains_incomplete():
    base = handler_for([])

    def handler(request):
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=[{"id": 6, "name": "UIndex", "enable": True}])
        if request.url.path == "/api/v1/search":
            return httpx.Response(
                200, json=[release(guid=f"row-{i}", infoHash=f"{i:040x}") for i in range(100)]
            )
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr",
            prowlarr_api_key="key",
            interval_seconds=0,
            max_candidates=1,
            max_queries=30,
        )
        result = await discovery.enrich(
            MEDIA,
            [],
            arr_url="http://radarr",
            arr_headers={},
            release_policy=ReleasePolicy(profile_name="Dublado", indexer_priority=("uindex",)),
        )
    assert not result.complete
    assert result.truncated_queries
@pytest.mark.asyncio
async def test_uncapped_audio_tags_cannot_prove_absence_in_a_capped_broad_sample():
    base = handler_for([])
    queries = []

    def handler(request):
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=[{"id": 6, "name": "UIndex", "enable": True}])
        if request.url.path == "/api/v1/search":
            query = request.url.params["query"]
            queries.append(query)
            rows = (
                [release(guid=f"row-{i}", infoHash=f"{i:040x}") for i in range(100)]
                if query == "Toy Story"
                else []
            )
            return httpx.Response(200, json=rows)
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client,
            prowlarr_url="http://prowlarr",
            prowlarr_api_key="key",
            interval_seconds=0,
            max_candidates=1,
            max_queries=30,
        )
        result = await discovery.enrich(
            {**MEDIA, "year": None},
            [],
            arr_url="http://radarr",
            arr_headers={},
            release_policy=ReleasePolicy(profile_name="Dublado", indexer_priority=("uindex",)),
        )
    assert all("Toy Story " + token in queries for token in ("DUAL", "DUBLADO", "PTBR", "PT-BR"))
    assert not result.complete
