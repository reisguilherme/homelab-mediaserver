from contextlib import aclosing
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_common.env import load_settings
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.dubbed_discovery import DubbedDiscovery
from homeserver_control.worker.release_quality import ReleasePolicy


def _policy(**environment):
    return ReleasePolicy.from_environment({
        "HOMESERVER_RELEASE_INDEXER_PRIORITY": "uindex,1337x,amigosshare",
        "HOMESERVER_RELEASE_FALLBACK_INDEXERS": "1337x",
        "HOMESERVER_RELEASE_MIN_SEEDERS": "1",
        **environment,
    })


def _release(name, indexer, seeds):
    return {
        "guid": name, "indexer": indexer, "seeders": seeds,
        "title": "Fixture BluRay", "languages": ["en", "pt-BR"],
        "quality": {"quality": {
            "source": "bluray", "modifier": "none", "resolution": 1080,
        }},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,expected", [
    ("Standard", ["u2", "asc", "u1", "fallback"]),
    ("Dublado", ["asc", "u2", "u1", "fallback"]),
])
async def test_opt_in_exhausts_primary_indexers_before_inspecting_fallback(profile, expected):
    acquirer = object.__new__(MovieAcquirer)
    acquirer.release_policy = _policy(HOMESERVER_DOWNLOAD_DEFAULT_PROFILE=profile)
    releases = [
        _release("u1", "UIndex", 1), _release("u2", "UIndex", 10),
        _release("asc", "Amigos Share Club", 5), _release("fallback", "1337x", 100),
    ]
    inspected, closed = [], []

    def rank(release):
        return acquirer._rank(release, original_language="en")

    async def eligible(group):
        try:
            for release in group:
                inspected.append(release["guid"])
                yield release, ("hash", "digest", ("film.mkv",), 1000), None, b"metadata"
        finally:
            closed.append([release["guid"] for release in group])

    async with aclosing(acquirer._preferred_indexer_candidates(
        sorted(releases, key=rank, reverse=True), eligible=eligible, rank=rank,
    )) as candidates:
        for name in expected:
            assert (await anext(candidates))[0]["guid"] == name
            if name != "fallback":
                assert "fallback" not in inspected
    assert sorted(name for group in closed for name in group) == ["asc", "fallback", "u1", "u2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_eligible", [False, True])
async def test_opt_in_closes_active_iterators_on_early_exit(primary_eligible):
    acquirer = object.__new__(MovieAcquirer)
    acquirer.release_policy = _policy()
    inspected, closed = [], []

    async def eligible(group):
        try:
            for release in group:
                inspected.append(release["guid"])
                if primary_eligible or release["guid"] == "fallback":
                    yield release, (), None, b"metadata"
        finally:
            closed.append([release["guid"] for release in group])

    async with aclosing(acquirer._preferred_indexer_candidates(
        [_release("primary", "UIndex", 1), _release("fallback", "1337x", 100)],
        eligible=eligible, rank=acquirer._rank,
    )) as candidates:
        assert (await anext(candidates))[0]["guid"] == (
            "primary" if primary_eligible else "fallback"
        )
    assert inspected == (["primary"] if primary_eligible else ["primary", "fallback"])
    assert sorted(name for group in closed for name in group) == sorted(inspected)


@pytest.mark.parametrize("fallback,priority", [
    ("unknown", "uindex,1337x"), ("1337x,1337x", "uindex,1337x"),
    ("amigosshare", "uindex,1337x"), ("uindex,1337x", "uindex,1337x"),
])
def test_invalid_fallback_policy_is_rejected(fallback, priority):
    with pytest.raises(ValueError, match="HOMESERVER_RELEASE_FALLBACK_INDEXERS"):
        _policy(HOMESERVER_RELEASE_FALLBACK_INDEXERS=fallback,
                HOMESERVER_RELEASE_INDEXER_PRIORITY=priority)


def test_settings_roundtrip_optional_fallback_and_worker_policy(tmp_path):
    path = tmp_path / "fixture.env"
    path.write_text("")
    assert load_settings(path).as_environment()["HOMESERVER_RELEASE_FALLBACK_INDEXERS"] == ""
    path.write_text("HOMESERVER_RELEASE_FALLBACK_INDEXERS=1337x\n")
    settings = load_settings(path)
    assert settings.release_fallback_indexers == ("1337x",)
    assert ReleasePolicy.from_environment(settings.as_environment()).fallback_indexers == ("1337x",)
    path.write_text("HOMESERVER_RELEASE_FALLBACK_INDEXERS=unknown\n")
    with pytest.raises(ValueError, match="HOMESERVER_RELEASE_FALLBACK_INDEXERS"):
        load_settings(path)


@pytest.mark.asyncio
@pytest.mark.parametrize("season", [None, 1])
@pytest.mark.parametrize("primary_eligible", [False, True])
async def test_dubbed_discovery_inspects_fallback_only_after_primary_is_exhausted(
    season, primary_eligible,
):
    policy = _policy(HOMESERVER_DOWNLOAD_DEFAULT_PROFILE="Dublado",
                     HOMESERVER_RELEASE_INDEXER_PRIORITY="uindex,1337x")
    media = {"id": 1, "title": "Fixture", "year": 2024, "originalLanguage": "en"}
    primary = _release("primary", "UIndex", 1 if primary_eligible else 0)
    fallback = _release("fallback", "1337x", 100)
    for release in (primary, fallback):
        release.update(size=1000, rejected=False, languages=["en"])
    searched, parsed, inspected = [], [], []
    fallback["title"] = "Fallback Fixture BluRay"

    def handler(request):
        if request.url.path == "/api/v1/indexer":
            return httpx.Response(200, json=[
                {"id": 1, "name": "UIndex", "enable": True},
                {"id": 2, "name": "1337x", "enable": True},
            ])
        if request.url.path == "/api/v1/indexerstatus":
            return httpx.Response(200, json=[{
                "indexerId": 2, "disabledTill": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            }] if primary_eligible else [])
        if request.url.path == "/api/v1/search":
            searched.append(request.url.params["indexerIds"])
            return httpx.Response(200, json=[])
        if request.url.path == "/api/v3/parse":
            parsed.append(request.url.params["title"])
            return httpx.Response(200, json={
                "parsedMovieInfo" if season is None else "parsedEpisodeInfo": {
                    "languages": ["en"], "quality": primary["quality"],
                },
            })
        raise AssertionError(str(request.url))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = object.__new__(MovieAcquirer)
        acquirer.release_policy = policy
        acquirer._manual_permit_id = None
        acquirer._dubbed_media = ContextVar("fixture_media", default=media)
        acquirer._dubbed_search_incomplete = ContextVar("fixture_search", default=False)
        acquirer.radarr_url, acquirer.headers = "http://arr", {}
        acquirer.dubbed_discovery = DubbedDiscovery(
            client=client, prowlarr_url="http://prowlarr", prowlarr_api_key="fixture",
            interval_seconds=0,
        )

        def rank(release):
            return acquirer._rank(release, original_language="en")

        async def eligible(group):
            for release in group:
                if rank(release) is not None:
                    inspected.append(release["guid"])
                    yield release, (), None, b"metadata"

        releases = await acquirer._discover_dubbed_releases(
            media, [primary, fallback], season=season,
        )
        async with aclosing(acquirer._preferred_indexer_candidates(
            sorted(releases, key=lambda row: rank(row) or (0,) * 8, reverse=True),
            eligible=eligible, rank=rank,
            **({"season": season} if season is not None else {}),
        )) as candidates:
            selected = await anext(candidates, None)
            assert selected is not None and selected[0]["guid"] == (
                "primary" if primary_eligible else "fallback"
            )
        assert inspected == (["primary"] if primary_eligible else ["fallback"])
        assert searched and set(searched) == ({"1"} if primary_eligible else {"1", "2"})
        assert parsed == (["Fixture BluRay"] if primary_eligible else ["Fallback Fixture BluRay"])
        assert acquirer._dubbed_search_incomplete.get() is False
