"""Season wording supplements only the opted-in fallback, through native review."""

import json

import httpx
import pytest
from test_series_pack_acquisition import SeriesFixture

from homeserver_control.worker.dubbed_discovery import DubbedDiscovery
from homeserver_control.worker.release_quality import ReleasePolicy


async def _acquire_pack(
    tmp_path, *, profile="Standard", failure=None, primary=False, opt_in=True, audio="original",
):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    media = {
        "id": 1, "tmdbId": 101, "title": "Fixture", "originalTitle": "Fixture",
        "alternateTitles": [{"title": " fixture "}, {"title": "Alias Fixture"}],
        "qualityProfileId": 3, "status": "ended", "monitored": True,
        "runtime": 45, "originalLanguage": {"name": "English"},
    }
    candidate = fixture.release(
        "fallback-pack", [1, 2], seeds=17, extra_files=("Featurettes/interview.mkv",),
    )
    candidate.update(
        indexer="1337x", indexerId=3, title="Fixture Season 1 S01 1080p BluRay EN DE",
        protocol="torrent", publishDate="2026-01-01T00:00:00Z",
        infoUrl="https://x1337x.ws/torrent/123/Fixture-Season-1/",
        quality={"quality": {"source": "bluray", "modifier": "none", "resolution": 1080}},
    )
    queries, parsed, pushed, descriptions = [], [], [], []
    if primary:
        native = fixture.release("primary-pack", [1, 2], seeds=1)
        native.update(quality=candidate["quality"], languages=["en", "de"])
        fixture.pack_offers = [native]
    elif failure == "native_rejected":
        fixture.pack_offers = [{
            **candidate, "indexer": "1337x (Prowlarr)", "rejected": True,
            "rejections": ["Blocklisted"],
        }]
    if failure == "capacity":
        fixture.free_bytes = 1

    def handler(request):
        path = request.url.path
        if request.url.host == "x1337x.ws":
            assert "X-Api-Key" not in request.headers
            descriptions.append(path)
            return httpx.Response(200, text=(
                '<div id="descrBody">Audio #1: English<br>'
                + ('Audio #2: Portuguese [Brazilian]' if audio == "dual" else 'Audio #2: German')
                + '</div>'
            ))
        if path == "/api/v3/series":
            return httpx.Response(200, json=[media])
        if path == "/api/v3/qualityprofile":
            return httpx.Response(200, json=[{"id": 3, "name": profile}])
        if path == "/api/v1/indexer":
            return httpx.Response(200, json=[
                {"id": 1, "name": "UIndex", "enable": True},
                {"id": 3, "name": "1337x", "enable": True},
            ])
        if path == "/api/v1/indexerstatus":
            return httpx.Response(200, json=[])
        if path == "/api/v3/indexer":
            return httpx.Response(200, json=[{"id": 3, "name": "1337x (Prowlarr)"}])
        if path == "/api/v1/search":
            query, indexer = request.url.params["query"], request.url.params["indexerIds"]
            queries.append((indexer, query))
            if failure == "incomplete" and indexer == "3" and query == "Fixture S01":
                return httpx.Response(500)
            return httpx.Response(
                200, json=[candidate] if indexer == "3" and query == "Fixture Season 1" else [],
            )
        if path == "/api/v3/parse":
            parsed.append(request.url.params["title"])
            matched = {**media, "id": 999} if failure == "wrong_show" else media
            season = 2 if failure == "wrong_season" else 1
            episodes = [
                {"id": number, "seriesId": matched["id"], "seasonNumber": season,
                 "episodeNumber": number}
                for number in (1, 2)
            ]
            if failure == "missing_id":
                episodes = episodes[:1]
            elif failure == "duplicate_id":
                episodes = [episodes[0], episodes[0]]
            return httpx.Response(200, json={
                "series": matched, "episodes": episodes,
                "parsedEpisodeInfo": {
                    "seasonNumber": season, "fullSeason": failure != "not_full_season",
                    "languages": [{"name": "English"}, {"name": "German"}],
                    "quality": candidate["quality"],
                },
            })
        if path == "/api/v3/release/push":
            body = json.loads(request.content)
            pushed.append(body)
            assert body["seriesId"] == 1 and body["seasonNumber"] == 1
            assert body["fullSeason"] is True and body["episodeIds"] == [1, 2]
            parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
            assert parent is not None
            if failure == "push_http":
                return httpx.Response(400, json=[{"errorMessage": "Unable to parse"}])
            if failure == "push_denied":
                return httpx.Response(200, json=[{"rejected": True, "rejections": ["Blocklisted"]}])
            fixture.confirm(parent)
            fixture.posts.append(body["guid"])
            return httpx.Response(200, json=[{"rejected": False}])
        return fixture.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696", prowlarr_api_key="fixture",
            interval_seconds=0,
        )
        acquirer = fixture.acquirer(client, prefer_season_pack=True, dubbed_discovery=discovery)
        acquirer.release_policy = ReleasePolicy.from_environment({
            "HOMESERVER_DOWNLOAD_DEFAULT_PROFILE": profile,
            "HOMESERVER_RELEASE_MIN_SEEDERS": "1",
            "HOMESERVER_RELEASE_FALLBACK_INDEXERS": "1337x" if opt_in else "",
        })
        try:
            status = await acquirer.acquire("season:tmdb:101:1", fixture.reservation)
        finally:
            await discovery.aclose()
    return status, fixture, queries, parsed, pushed, descriptions


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["Standard", "Max Quality", "Dublado"])
async def test_season_wording_finds_complete_original_pack_when_sxx_is_empty(tmp_path, profile):
    status, fixture, queries, parsed, pushed, descriptions = await _acquire_pack(
        tmp_path, profile=profile,
    )
    assert status == "grabbed"
    assert fixture.posts == fixture.metadata == ["fallback-pack"]
    assert fixture.searched == ["pack"]
    assert [query for indexer, query in queries if indexer == "3"] == [
        "Fixture Season 1", "Alias Fixture Season 1", "Fixture S01", "Alias Fixture S01",
    ]
    assert parsed == ["Fixture Season 1 S01 1080p BluRay EN DE"]
    assert descriptions == ["/torrent/123/Fixture-Season-1/"]
    assert len(pushed) == 1
    parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
    assert parent is not None and parent.state == "confirmed"
    assert parent.budget_bytes == 3_000_003_000
    assert len(parent.selected_files) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "wrong_show", "wrong_season", "not_full_season", "missing_id", "duplicate_id",
    "native_rejected",
])
async def test_season_wording_preserves_native_identity_and_rejections(tmp_path, failure):
    status, fixture, _queries, _parsed, pushed, _descriptions = await _acquire_pack(
        tmp_path, failure=failure,
    )
    assert status == "waiting_complete_season_pack"
    assert fixture.metadata == fixture.posts == pushed == []
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["push_denied", "push_http", "capacity"])
async def test_season_wording_never_keeps_a_claim_after_native_or_capacity_denial(
    tmp_path, failure,
):
    status, fixture, _queries, _parsed, pushed, _descriptions = await _acquire_pack(
        tmp_path, failure=failure,
    )
    assert status == "waiting_complete_season_pack"
    assert fixture.metadata == ["fallback-pack"]
    assert fixture.posts == []
    assert len(pushed) == (0 if failure == "capacity" else 1)
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None
    assert fixture.permits.pending_bytes(await fixture.capacity()) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,audio,expected", [
    ("Standard", "original", "grabbed"), ("Max Quality", "original", "grabbed"),
    ("Dublado", "original", "waiting_complete_season_pack"), ("Dublado", "dual", "grabbed"),
])
async def test_partial_discovery_keeps_profile_audio_rules(tmp_path, profile, audio, expected):
    status, fixture, _queries, _parsed, _pushed, descriptions = await _acquire_pack(
        tmp_path, profile=profile, audio=audio, failure="incomplete",
    )
    assert status == expected
    assert fixture.posts == (["fallback-pack"] if expected == "grabbed" else [])
    assert descriptions == ["/torrent/123/Fixture-Season-1/"]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["Standard", "Max Quality", "Dublado"])
async def test_healthy_primary_does_not_consult_the_season_fallback(tmp_path, profile):
    status, fixture, queries, _parsed, pushed, descriptions = await _acquire_pack(
        tmp_path, profile=profile, primary=True,
    )
    assert status == "grabbed"
    assert fixture.posts == fixture.metadata == ["primary-pack"]
    assert not any(indexer == "3" for indexer, _query in queries)
    assert pushed == descriptions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["Standard", "Max Quality", "Dublado"])
async def test_season_wording_is_disabled_without_fallback_opt_in(tmp_path, profile):
    status, fixture, queries, _parsed, pushed, _descriptions = await _acquire_pack(
        tmp_path, profile=profile, opt_in=False,
    )
    assert status == "waiting_complete_season_pack"
    assert fixture.metadata == fixture.posts == pushed == []
    assert not any("Season 1" in query for _indexer, query in queries)
