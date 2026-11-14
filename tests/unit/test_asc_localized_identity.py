"""Localized ASC names need native identity proof without altering pushed titles."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.worker.dubbed_discovery import DubbedDiscovery, _identity
from homeserver_control.worker.release_quality import ReleasePolicy

MEDIA = {
    "id": 401, "tmdbId": 990001, "tvdbId": 880001, "imdbId": "tt9900001",
    "title": "Fixture's Brother", "year": 2014,
    "originalLanguage": {"name": "Portuguese (Brazil)"},
}
LOCALIZED = "Irmão da Amostra"
QUALITY = {"quality": {"name": "WEBDL-1080p", "source": "webdl", "resolution": 1080}}
POLICY = ReleasePolicy.from_environment({
    "HOMESERVER_RELEASE_INDEXER_PRIORITY": "amigosshare",
}).for_profile("Dublado")


class LocalizedAPI:
    """Replace external HTTP only; all discovery and identity validation run normally."""

    def __init__(self, season=2, *, prefix=LOCALIZED, native_tmdb=0):
        self.season = season
        self.year = {1: 2014, 2: 2016, 3: 2018, 4: 2021, 5: 2024}[season]
        self.original = f"{prefix} {self.year} S{season:02d} 1080p WEB-DL H.265 Nacional"
        self.canonical = (
            f"Fixture's Brother S{season:02d} 1080p WEB-DL H.265 Nacional"
        )
        self.media = deepcopy(MEDIA)
        self.resolved = {**MEDIA, "tmdbId": native_tmdb}
        self.episodes = [
            {"id": season * 100 + number, "seriesId": 401,
             "seasonNumber": season, "episodeNumber": number}
            for number in (1, 2)
        ]
        self.detail = {
            "seasonNumber": season, "seasonNumbers": [season], "episodeNumbers": [],
            "fullSeason": True, "isPartialSeason": False, "isMultiSeason": False,
            "isSeasonExtra": False, "quality": QUALITY,
            "languages": [{"name": "Portuguese"}], "releaseGroup": "fixture",
        }
        self.canonical_changes = {}
        self.canonical_episodes = None
        self.original_series = None
        self.original_episodes = []
        self.metadata = {"id": 990001, "name": LOCALIZED}
        self.searches = []
        self.parses = []
        self.catalog_reads = 0
        self.only_imdb = False
        self.candidate = {
            "title": self.original, "guid": "localized-fixture", "indexerId": 9,
            "indexer": "Amigos Share Club", "seeders": 15, "size": 2_000_000_000,
            "infoHash": "a" * 40, "protocol": "torrent",
            "downloadUrl": "http://prowlarr:9696/9/download?link=fixture",
            "publishDate": "2026-01-01T00:00:00Z",
        }

    def __call__(self, request):
        assert request.method == "GET"
        path = request.url.path
        if request.url.host == "seerr":
            assert request.headers["X-Api-Key"] == "fixture-seerr"
            assert path == "/api/v1/tv/990001"
            return httpx.Response(200, json=self.metadata)
        if request.url.host == "prowlarr":
            assert request.headers["X-Api-Key"] == "fixture-prowlarr"
            if path == "/api/v1/indexer":
                return httpx.Response(200, json=[
                    {"id": 9, "name": "Amigos Share Club", "enable": True},
                ])
            if path == "/api/v1/indexerstatus":
                return httpx.Response(200, json=[])
            if path == "/api/v1/search":
                query = request.url.params["query"]
                self.searches.append(query)
                rows = [] if self.only_imdb and query != "tt9900001" else [self.candidate]
                return httpx.Response(200, json=rows)
        if request.url.host == "sonarr":
            assert request.headers["X-Api-Key"] == "fixture-sonarr"
            if path == "/api/v3/indexer":
                return httpx.Response(200, json=[
                    {"id": 19, "name": "Amigos Share Club (Prowlarr)"},
                ])
            if path == "/api/v3/episode":
                assert request.url.params["seriesId"] == "401"
                self.catalog_reads += 1
                return httpx.Response(200, json=self.episodes)
            if path == "/api/v3/release":
                return httpx.Response(200, json=[])
            if path == "/api/v3/parse":
                title = request.url.params["title"]
                self.parses.append(title)
                canonical = title == self.canonical
                return httpx.Response(200, json={
                    "series": self.resolved if canonical else self.original_series,
                    "episodes": (
                        self.episodes if self.canonical_episodes is None
                        else self.canonical_episodes
                    ) if canonical else self.original_episodes,
                    "parsedEpisodeInfo": {
                        **self.detail,
                        "releaseTitle": title,
                        "seriesTitle": MEDIA["title"] if canonical else LOCALIZED,
                        **(self.canonical_changes if canonical else {}),
                    },
                })
        raise AssertionError(str(request.url))


async def discover(api, *, max_queries=30, max_candidates=30, native=()):
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="fixture-prowlarr", seerr_url="http://seerr:5055",
            seerr_api_key="fixture-seerr", interval_seconds=0,
            max_queries=max_queries, max_candidates=max_candidates,
        )
        return await discovery.enrich(
            api.media, list(native), arr_url="http://sonarr:8989",
            arr_headers={"X-Api-Key": "fixture-sonarr"}, season=api.season,
            release_policy=POLICY, require_complete_season_pack=True,
        )


async def production_wrapper(
    api, tmp_path, *, profile="Standard", complete=True, early_cache=False, tier_iterator=False,
    native=(),
):
    from homeserver_control.gateway.permits import PermitRegistry
    from homeserver_control.persistence.db import ReservationRepository
    from homeserver_control.worker.series_acquisition import SeriesAcquirer

    repository = ReservationRepository(tmp_path / "control.sqlite")
    repository.initialize()
    policy = ReleasePolicy.from_environment({
        "HOMESERVER_RELEASE_INDEXER_PRIORITY": "uindex,amigosshare,1337x",
        "HOMESERVER_RELEASE_FALLBACK_INDEXERS": "1337x",
    }, media_kind="series").for_profile(profile)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as client:
        discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr:9696",
            prowlarr_api_key="fixture-prowlarr", seerr_url="http://seerr:5055",
            seerr_api_key="fixture-seerr", interval_seconds=0, max_queries=30,
        )
        acquirer = SeriesAcquirer(
            repository=repository, permits=PermitRegistry(repository.path),
            sonarr_url="http://sonarr:8989", sonarr_api_key="fixture-sonarr",
            prowlarr_url="http://prowlarr:9696", client=client,
            release_policy=policy, dubbed_discovery=discovery,
        )
        token = acquirer._dubbed_media.set(api.media)
        try:
            if early_cache:
                # Early discovery can cache supplemental rows for later pack selection.
                acquirer.release_policy = ReleasePolicy.from_environment({
                    "HOMESERVER_RELEASE_INDEXER_PRIORITY": "amigosshare",
                }, media_kind="series").for_profile(profile)
                rows = await acquirer._season_releases(401, api.season)
                cached = await acquirer._season_releases(401, api.season)
                assert cached == rows
            elif tier_iterator:
                def rank(row):
                    return policy.rank(row, original_language=api.media["originalLanguage"])

                async def inspected(rows):
                    for row in rows:
                        if rank(row) is not None and row.get("rejected") is False:
                            yield (row,)

                rows = [item[0] async for item in acquirer._preferred_indexer_candidates(
                    list(native), eligible=inspected, rank=rank, season=api.season,
                    complete_season_pack=True,
                )]
            else:
                rows = await acquirer._discover_dubbed_releases(
                    api.media, list(native), season=api.season, indexers=("uindex", "amigosshare"),
                    complete_season_pack=complete,
                )
            assert acquirer.release_policy.profile_name == profile
            return rows
        finally:
            acquirer._dubbed_media.reset(token)


def native_unknown_series(api, **changes):
    return {
        **api.candidate, "indexerId": 19, "indexer": "Amigos Share Club (Prowlarr)",
        "quality": QUALITY, "seriesId": None, "mappedSeriesId": None,
        "episodeIds": None, "episodeNumbers": [], "seasonNumber": api.season,
        "fullSeason": True, "languages": [{"name": "Unknown"}],
        "rejected": True, "rejections": ["Unknown Series"], **changes,
    }


@pytest.mark.asyncio
async def test_actual_native_unknown_series_context_gets_separate_alias_proof(tmp_path):
    api = LocalizedAPI()
    api.media["originalLanguage"] = {"name": "Portuguese"}
    api.detail["languages"] = [{"name": "Unknown"}]
    native = native_unknown_series(api)
    original = deepcopy(native)
    rows = await production_wrapper(api, tmp_path, native=[native])
    proven = [row for row in rows if row.get("requiresNativeDecision")]
    assert len(proven) == 1
    assert proven[0]["title"] == api.original
    assert proven[0]["tvdbId"] == 880001
    assert proven[0]["episodeIds"] == [201, 202]
    assert [row["rejections"] for row in rows if row.get("rejected") is True] == [
        ["Unknown Series"],
    ]
    assert native == original
    assert api.catalog_reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"rejections": ["Unknown Series", "Release is blocklisted"]},
    {"rejections": ["Release is blocklisted"]}, {"rejections": []},
    {"seriesId": 402}, {"seriesId": False}, {"mappedSeriesId": 402},
    {"episodeIds": [701]}, {"episodeNumbers": [1]},
    {"mappedEpisodeNumbers": [1]}, {"mappedEpisodeInfo": [{"id": 701}]},
    {"rejections": "Unknown Series"},
    {"quality": {"quality": {"source": "webdl", "resolution": 720}}},
])
async def test_native_identity_retry_preserves_other_vetoes_and_resolved_bindings(
    tmp_path, changes,
):
    api = LocalizedAPI()
    native = native_unknown_series(api, **changes)
    rows = await production_wrapper(api, tmp_path, native=[native])
    assert not any(row.get("requiresNativeDecision") for row in rows)
    assert rows[0]["rejected"] is True
    assert rows[0]["rejections"] == native["rejections"]
    assert api.parses == []


@pytest.mark.asyncio
async def test_duplicate_native_nonidentity_veto_blocks_an_alias_retry(tmp_path):
    api = LocalizedAPI()
    native = native_unknown_series(api)
    blocked = native_unknown_series(api, rejections=["Release is blocklisted"])
    rows = await production_wrapper(api, tmp_path, native=[native, blocked])
    assert not any(row.get("requiresNativeDecision") for row in rows)
    assert [row["rejections"] for row in rows] == [
        ["Unknown Series"], ["Release is blocklisted"],
    ]
    assert api.parses == []


@pytest.mark.asyncio
async def test_native_unknown_series_retry_still_requires_complete_alias_catalog_proof(tmp_path):
    api = LocalizedAPI()
    api.canonical_episodes = [api.episodes[0]]
    rows = await production_wrapper(api, tmp_path, native=[native_unknown_series(api)])
    assert not any(row.get("requiresNativeDecision") for row in rows)
    assert rows[0]["rejections"] == ["Unknown Series"]
    assert api.catalog_reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tier_iterator", [False, True])
async def test_standard_profile_discovers_only_configured_asc_pack_tier(tmp_path, tier_iterator):
    api = LocalizedAPI()
    api.media["originalLanguage"] = {"name": "Portuguese"}
    api.detail["languages"] = [{"name": "Unknown"}]
    rows = await production_wrapper(api, tmp_path, tier_iterator=tier_iterator)
    assert len(rows) == 1
    assert rows[0]["title"] == api.original
    assert rows[0]["requiresNativeDecision"] is True
    assert api.catalog_reads == 1
    assert {"source": "title", "languages": ["pt"]} in rows[0]["audioEvidence"]
    assert POLICY.for_profile("Standard").rank(
        rows[0], original_language=api.media["originalLanguage"],
    ) is not None


@pytest.mark.parametrize("indexer,title,original,expected", [
    ("ASC", "Fixture S02 1080p WEB-DL H.265 Nacional", "pt", {"pt"}),
    ("ASC", "Fixture S02 1080p WEB-DL H.265 Nacional", {"name": "Portuguese"}, {"pt"}),
    ("UIndex", "Fixture S02 1080p WEB-DL H.265 Nacional", "pt", set()),
    ("ASC", "Fixture S02 1080p WEB-DL H.265 Nacional", "en", set()),
    ("ASC", "Fixture S02 1080p WEB-DL H.265 Nacional", None, set()),
    ("ASC", "Nacional Fixture S02 1080p WEB-DL H.265", "pt", set()),
    ("ASC", "Fixture 2016 Nacional", "pt", set()),
    ("ASC", "Fixture Nacional", "pt", set()),
    ("ASC", "Fixture S02 1080p WEB-DL [SUBS Nacional]", "pt", set()),
    ("ASC", "Fixture S02 1080p WEB-DL Nacional Subtitles", "pt", set()),
])
def test_asc_nacional_claim_proves_only_generic_original_portuguese(
    indexer, title, original, expected,
):
    from homeserver_control.worker.audio_evidence import release_audio_languages

    assert release_audio_languages({
        "indexer": indexer, "title": title, "audioEvidence": [],
    }, original_language=original) == expected


def test_observed_audio_overrides_asc_nacional_claim():
    from homeserver_control.worker.audio_evidence import release_audio_languages

    assert release_audio_languages({
        "indexer": "ASC", "title": "Fixture S02 1080p WEB-DL H.265 Nacional",
        "audioEvidence": [{"source": "ffprobe", "languages": ["en"]}],
    }, original_language="pt") == {"en"}


@pytest.mark.asyncio
@pytest.mark.parametrize("early_cache", [False, True])
@pytest.mark.parametrize("invalid", ["partial_episodes", "partial_flag", "multiple_seasons"])
async def test_production_wrapper_never_caches_an_incomplete_localized_pack(
    tmp_path, early_cache, invalid,
):
    api = LocalizedAPI()
    if invalid == "partial_episodes":
        api.canonical_episodes = [api.episodes[0]]
    elif invalid == "partial_flag":
        api.detail["isPartialSeason"] = True
    else:
        api.detail["seasonNumbers"] = [2, 3]
    rows = await production_wrapper(api, tmp_path, profile="Dublado", early_cache=early_cache)
    assert rows == []
    assert api.catalog_reads == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("season", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("native_tmdb", [0, None, 990001])
async def test_localized_pack_keeps_original_metadata_and_requires_native_push(season, native_tmdb):
    # Removing the alias retry or requiring Sonarr's missing TMDB loses valid packs.
    api = LocalizedAPI(season, native_tmdb=native_tmdb)
    original = deepcopy(api.candidate)
    result = await discover(api)
    assert result.complete
    assert len(result.releases) == 1
    release = result.releases[0]
    assert release["title"] == original["title"]
    assert release["downloadUrl"] == original["downloadUrl"]
    assert release["guid"] == "localized-fixture"
    assert release["infoHash"] == "a" * 40
    assert release["publishDate"] == "2026-01-01T00:00:00Z"
    assert release["quality"] == QUALITY
    assert release["seriesId"] == 401
    assert release["tvdbId"] == 880001
    assert release["imdbId"] == "tt9900001"
    assert release["episodeIds"] == [season * 100 + 1, season * 100 + 2]
    assert release["episodeNumbers"] == [1, 2]
    assert release["fullSeason"] is True
    assert release["requiresNativeDecision"] is True
    assert release["audioEvidence"][0] == {
        "source": "arr-parser", "languages": [{"name": "Portuguese"}],
    }
    assert api.parses == [api.original, api.canonical]
    assert api.candidate == original


@pytest.mark.asyncio
async def test_asc_imdb_query_finds_localized_pack_within_single_search_budget():
    # IMDb discovery must not wait for a capped English-title search to reveal it.
    api = LocalizedAPI()
    api.only_imdb = True
    result = await discover(api, max_queries=1)
    assert len(result.releases) == 1
    assert api.searches == ["tt9900001"]
    assert not result.complete


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [
    "Outro Irmão da Amostra", "Irmão da Amostra Outra Série", "Irmão da Amostra2",
])
async def test_spoofed_localized_prefix_never_supplies_a_native_identity(prefix):
    api = LocalizedAPI(prefix=prefix)
    result = await discover(api)
    assert result.releases == []
    assert api.parses == [api.original]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("id", 402), ("tvdbId", 880002), ("tvdbId", 0),
    ("tmdbId", 990002), ("tmdbId", "0"), ("tmdbId", False),
])
async def test_alias_cannot_override_conflicting_native_identity(field, value):
    api = LocalizedAPI()
    api.resolved[field] = value
    result = await discover(api)
    assert result.releases == []


@pytest.mark.parametrize("field,value", [("id", True), ("tmdbId", True), ("tmdbId", "990001")])
def test_missing_native_tmdb_requires_positive_canonical_integer_ids(field, value):
    api = LocalizedAPI()
    api.media[field] = value
    if field == "id":
        api.resolved["id"] = 1
        for episode in api.episodes:
            episode["seriesId"] = 1
    assert not _identity({
        "series": api.resolved, "episodes": api.episodes, "parsedEpisodeInfo": api.detail,
    }, api.media, season=2)


@pytest.mark.asyncio
async def test_alias_never_rebinds_an_original_parse_that_resolved_to_another_series():
    api = LocalizedAPI()
    api.original_series = {**MEDIA, "id": 402, "tvdbId": 880002, "tmdbId": 990002}
    result = await discover(api)
    assert result.releases == []
    assert api.parses == [api.original]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"seasonNumber": 3, "seasonNumbers": [3]}, {"seasonNumbers": [2, 3]},
    {"fullSeason": False}, {"isPartialSeason": True}, {"isMultiSeason": True},
    {"languages": [{"name": "English"}]},
    {"quality": {"quality": {"source": "webdl", "resolution": 720}}},
    {"releaseGroup": "changed"},
])
async def test_alias_retry_must_preserve_native_release_facts(changes):
    api = LocalizedAPI()
    api.canonical_changes = changes
    result = await discover(api)
    assert result.releases == []


@pytest.mark.asyncio
async def test_alias_pack_requires_every_native_episode_exactly_once():
    api = LocalizedAPI()
    api.episodes.append(deepcopy(api.episodes[0]))
    result = await discover(api)
    assert result.releases == []


@pytest.mark.asyncio
async def test_alias_pack_rejects_partial_native_episode_mapping():
    api = LocalizedAPI()
    api.canonical_episodes = [api.episodes[0]]
    result = await discover(api)
    assert result.releases == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("tvdbId", 880002), ("tmdbId", 990002), ("imdbId", "tt9900002"),
])
async def test_alias_cannot_replace_conflicting_identifiers_on_the_source_release(field, value):
    api = LocalizedAPI()
    api.candidate[field] = value
    result = await discover(api)
    assert result.releases == []
    assert api.parses == [api.original]


@pytest.mark.asyncio
async def test_wrong_season_in_original_title_cannot_acquire_requested_pack():
    api = LocalizedAPI()
    api.candidate["title"] = api.original.replace("S02", "S03")
    result = await discover(api)
    assert result.releases == []
    assert api.parses == [api.candidate["title"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"fullSeason": False}, {"isPartialSeason": True}, {"isMultiSeason": True},
    {"seasonNumbers": [2, 3]},
])
async def test_alias_cannot_turn_an_original_partial_or_ambiguous_pack_into_complete(changes):
    api = LocalizedAPI()
    api.detail.update(changes)
    result = await discover(api)
    assert result.releases == []


@pytest.mark.asyncio
async def test_localized_name_from_wrong_seerr_identity_cannot_supply_an_alias():
    api = LocalizedAPI()
    api.metadata["id"] = 990002
    result = await discover(api)
    assert result.releases == []
    assert not result.complete
    assert api.parses == [api.original]


@pytest.mark.asyncio
async def test_alias_retry_consumes_the_existing_native_parse_budget():
    api = LocalizedAPI()
    result = await discover(api, max_candidates=1)
    assert result.releases == []
    assert not result.complete
    assert api.parses == [api.original]


@pytest.mark.asyncio
async def test_duplicate_native_rejection_survives_localized_discovery():
    api = LocalizedAPI()
    rejected = api.candidate | {"rejected": True, "rejections": ["Release is blocklisted"]}
    result = await discover(api, native=[rejected])
    assert len(result.releases) == 1
    assert result.releases[0]["rejected"] is True
    assert result.releases[0]["rejections"] == ["Release is blocklisted"]
    assert api.parses == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rejected", [False, True, "invalid"])
@pytest.mark.parametrize("native_context", [False, True])
async def test_localized_source_is_pushed_unchanged_and_native_rejection_cancels_permit(
    tmp_path, rejected, native_context,
):
    from homeserver_control.domain.torrent_bytes import inspect_torrent
    from homeserver_control.gateway.permits import PermitRegistry
    from homeserver_control.persistence.db import ReservationRepository
    from homeserver_control.worker.capacity_evidence import CapacityEvidence
    from homeserver_control.worker.season_packs import inspect_season_pack
    from homeserver_control.worker.series_acquisition import SeriesAcquirer

    def encode(value):
        if isinstance(value, bytes):
            return str(len(value)).encode() + b":" + value
        if isinstance(value, int):
            return b"i" + str(value).encode() + b"e"
        if isinstance(value, list):
            return b"l" + b"".join(encode(item) for item in value) + b"e"
        return b"d" + b"".join(encode(key) + encode(item)
                              for key, item in sorted(value.items())) + b"e"

    torrent = encode({b"info": {
        b"name": b"Fixture", b"piece length": 16_777_216, b"pieces": b"a" * 40,
        b"files": [
            {b"path": [f"Fixture.S02E{number:02d}.1080p.{extension}".encode()],
             b"length": 10_000_000 if extension == "mkv" else 1000}
            for number in (1, 2) for extension in ("mkv", "pt-BR.srt")
        ],
    }})
    inspected = inspect_torrent(torrent)
    api = LocalizedAPI()
    api.media["originalLanguage"] = {"name": "Portuguese"}
    api.detail["languages"] = [{"name": "Unknown"}]
    api.candidate.update(infoHash=inspected.infohash, size=inspected.total_bytes)
    if native_context:
        rows = await production_wrapper(
            api, tmp_path, native=[native_unknown_series(api)],
        )
    else:
        rows = (await discover(api)).releases
    proven = [row for row in rows if row.get("requiresNativeDecision")]
    assert len(proven) == 1
    selected = proven[0]
    manifest = inspect_season_pack(
        torrent, season=2, episode_runtimes={1: 11, 2: 11}, release=selected,
        release_policy=POLICY.for_profile("Standard"),
        original_language=api.media["originalLanguage"], episode_catalog=api.episodes,
    )
    assert manifest is not None
    repository = ReservationRepository(tmp_path / "control.sqlite")
    repository.initialize()
    reservation = repository.reserve(
        request_id="fixture", source_id="fixture", media_key="season:tmdb:990001:2",
        filesystem_id="fixture", budget_bytes=0, free_bytes=50_000_000_000,
        total_bytes=100_000_000_000,
    )
    permits = PermitRegistry(repository.path)
    permit = permits.issue(
        infohash=manifest.infohash, metadata_sha256=manifest.metadata_sha256,
        selected_files=manifest.selected_files, budget_bytes=manifest.budget_bytes,
        destination="/data/torrents", category="sonarr",
        reservation_id=reservation.reservation_id, scope_key="S02PACK",
        capacity=CapacityEvidence(free_bytes=50_000_000_000, remaining_by_hash={}),
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    permits.bind_season_pack(
        permit.token, episode_files=manifest.episode_files, episode_catalog=api.episodes,
    )
    pushed = []

    def native_push(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v3/release/push"
        pushed.append(json.loads(request.content))
        assert permits.get(permit.token).state == "authorized"
        if rejected == "invalid":
            return httpx.Response(400, json=[{"errorMessage": "Unable to parse"}])
        if not rejected:
            permits.authorize(
                token=permit.token, infohash=permit.infohash, destination=permit.destination,
                metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
            )
        return httpx.Response(200, json=[{
            "rejected": rejected, "rejections": ["Blocklisted"] if rejected else [],
        }])

    async with httpx.AsyncClient(transport=httpx.MockTransport(native_push)) as client:
        acquirer = SeriesAcquirer(
            repository=repository, permits=permits, sonarr_url="http://sonarr:8989",
            sonarr_api_key="fixture-sonarr", prowlarr_url="http://prowlarr:9696", client=client,
        )
        accepted = await acquirer._dispatch_release(selected, permit)
    assert accepted is (not rejected)
    assert len(pushed) == 1
    assert pushed[0]["title"] == api.original
    assert pushed[0]["downloadUrl"] == api.candidate["downloadUrl"]
    assert pushed[0]["tvdbId"] == 880001
    assert pushed[0]["imdbId"] == "tt9900001"
    assert "requiresNativeDecision" not in pushed[0]
    if rejected:
        assert permits.get(permit.token) is None
    else:
        assert permits.get(permit.token).state == "confirmed"
