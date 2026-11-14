from dataclasses import replace

import httpx
import pytest
from test_indexer_selection import _choose_indexer
from test_series_pack_acquisition import SeriesFixture

from homeserver_control.worker.release_quality import ReleasePolicy

ENVIRONMENT = {
    "HOMESERVER_RELEASE_FALLBACK_INDEXERS": "1337x",
    "HOMESERVER_RELEASE_MIN_SEEDERS": "1",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("deny_both", [False, True])
async def test_fresh_capacity_denial_exhausts_primary_alternatives_before_fallback(
    tmp_path, deny_both,
):
    status, posted, inspected = await _choose_indexer(
        tmp_path, kind="movie", fallback_threshold=5, environment_overrides=ENVIRONMENT,
        offers=[
            ("primary1", "UIndex", 1080, 20, 2_000_000_000),
            ("primary2", "UIndex", 1080, 10, 2_000_000_000),
            ("fallback", "1337x", 1080, 100, 2_000_000_000),
        ],
        capacity_free_bytes=[
            50_000_000_000, 0, 50_000_000_000,
            0 if deny_both else 50_000_000_000, 50_000_000_000, 50_000_000_000,
        ],
    )
    assert status == "grabbed"
    assert posted == (["fallback"] if deny_both else ["primary2"])
    assert inspected == (["primary1", "primary2", "fallback"] if deny_both else
                         ["primary1", "primary2"])


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["movie", "series"])
@pytest.mark.parametrize("failure", [None, "quality", "capacity"])
async def test_real_admission_keeps_1337x_for_after_eligible_primary(tmp_path, kind, failure):
    status, posted, inspected = await _choose_indexer(
        tmp_path, kind=kind, fallback_threshold=5, environment_overrides=ENVIRONMENT,
        offers=[
            ("primary", "UIndex", 1080, 1,
             400_000_000 if failure == "quality" else
             60_000_000_000 if failure == "capacity" else 2_000_000_000),
            ("fallback", "1337x", 1080, 100, 2_000_000_000),
        ],
    )
    assert status == "grabbed"
    assert posted == (["fallback"] if failure else ["primary"])
    assert inspected == (["primary", "fallback"] if failure else ["primary"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "incomplete", "capacity", "all-incomplete"])
async def test_completed_season_fallback_preserves_complete_pack_admission(tmp_path, failure):
    fixture = SeriesFixture(tmp_path, episode_count=2, free_bytes=10_000_000_000)
    primary = fixture.release(
        "primary", [1] if failure in {"incomplete", "all-incomplete"} else [1, 2],
        seeds=1, video_bytes=6_000_000_000 if failure == "capacity" else 1_500_000_000,
    )
    fallback = fixture.release(
        "fallback", [1] if failure == "all-incomplete" else [1, 2], seeds=100,
    )
    fallback["indexer"] = "1337x"
    fixture.pack_offers = [primary, fallback]
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        acquirer = fixture.acquirer(client, prefer_season_pack=True)
        acquirer.release_policy = replace(ReleasePolicy.from_environment({
            **ENVIRONMENT, "HOMESERVER_DOWNLOAD_DEFAULT_PROFILE": "Standard",
            "HOMESERVER_INDEXER_FALLBACK_MIN_SEEDERS": "1",
        }, media_kind="series"), profile_name=None)
        status = await acquirer.acquire("season:tmdb:101:1", fixture.reservation)
    if failure == "all-incomplete":
        assert status == "waiting_complete_season_pack"
        assert fixture.posts == []
    else:
        assert status == "grabbed"
        assert fixture.posts == (["fallback"] if failure else ["primary"])
        parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
        assert parent is not None and len(parent.selected_files) == 4
    assert fixture.metadata == (["primary", "fallback"] if failure else ["primary"])
    assert fixture.searched == ["pack"]
