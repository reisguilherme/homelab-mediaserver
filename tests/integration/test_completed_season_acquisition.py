import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_series_pack_acquisition import SeriesFixture


def metadata_handler(fixture, *, state="ended", future=False, unmonitored=False):
    def handler(request):
        response = fixture.handler(request)
        if request.url.path == "/api/v3/series":
            rows = response.json()
            rows[0]["status"] = state
            return httpx.Response(200, json=rows)
        if request.url.path == "/api/v3/episode":
            rows = response.json()
            if future:
                rows[-1]["airDateUtc"] = (datetime.now(UTC) + timedelta(days=7)).isoformat()
            if unmonitored:
                rows[-1]["monitored"] = False
            return httpx.Response(200, json=rows)
        return response

    return handler


@pytest.mark.asyncio
async def test_completed_season_never_falls_back_to_single_or_preference_flag(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metadata_handler(fixture))
    ) as client:
        result = await fixture.acquirer(client, prefer_season_pack=False).acquire(
            "season:tmdb:101:1",
            fixture.reservation,
        )
    assert result == "waiting_complete_season_pack"
    assert fixture.posts == []
    assert fixture.searched == ["pack"]


@pytest.mark.asyncio
async def test_completed_season_retires_expired_single_before_pack_selection(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    expired = fixture.existing(1, state="authorized")
    preserved = fixture.existing(2)
    with sqlite3.connect(fixture.repo.path) as connection:
        connection.execute(
            "UPDATE gateway_permits SET expires_at=? WHERE token=?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), expired.token),
        )
    fixture.pack_offers = [fixture.release("whole", [1, 2])]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metadata_handler(fixture))
    ) as client:
        result = await fixture.acquirer(client).acquire(
            "season:tmdb:101:1", fixture.reservation,
        )
    assert result == "grabbed"
    first = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01E01")
    second = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01E02")
    assert first.season_pack_parent_id is not None
    assert second.permit_id == preserved.permit_id
    assert fixture.permits.get(expired.token) is None
    assert fixture.searched == ["pack"]
    assert fixture.posts == ["whole"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["ended", "continuing"])
async def test_existing_single_probe_is_finished_before_season_strategy(tmp_path, status):
    from homeserver_control.worker.capacity_evidence import CapacityEvidence
    from homeserver_control.worker.source_health import SourceHealthStore

    fixture = SeriesFixture(tmp_path, episode_count=2)
    source = fixture.existing(1)
    fixture.remaining[source.infohash] = source.budget_bytes
    fixture.permits.set_quality(source.token, (1, 1080))
    probe = fixture.permits.issue_probe(
        source.token, infohash="b" * 40, metadata_sha256="c" * 64,
        selected_files=("candidate/Fixture.S01E01.mkv",), budget_bytes=2_000_000_000,
        capacity=CapacityEvidence(
            free_bytes=fixture.free_bytes, remaining_by_hash=fixture.remaining,
        ),
        expires_at=datetime.now(UTC) + timedelta(minutes=30), quality_rank=(1, 1080),
    )
    with sqlite3.connect(fixture.repo.path) as connection:
        connection.execute(
            "UPDATE gateway_permits SET expires_at=? WHERE token=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), probe.token),
        )
    base = metadata_handler(fixture, state=status)

    def handler(request):
        if request.url.path == "/internal/probe-decision":
            assert json.loads(request.content) == {
                "permit_token": probe.token, "decision": "reject",
            }
            fixture.permits.reject_probe(probe.token)
            return httpx.Response(200, json={"state": "rejected"})
        return base(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fixture.acquirer(
            client, live_source_probes=True, health_store=SourceHealthStore(fixture.repo.path),
        ).acquire(
            "season:tmdb:101:1", fixture.reservation,
        )
    assert result == "rejected"
    assert fixture.permits.get_probe(source.permit_id) is None
    assert fixture.permits.get_for_reservation(
        fixture.reservation, scope_key="S01E01",
    ).permit_id == source.permit_id
    assert fixture.searched == []
    assert fixture.posts == []


@pytest.mark.asyncio
async def test_complete_pack_covers_unmonitored_canonical_episode(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.pack_offers = [fixture.release("whole", [1, 2])]
    handler = metadata_handler(fixture, unmonitored=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1",
            fixture.reservation,
        )
    assert result == "grabbed"
    assert fixture.posts == ["whole"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "future, expected", [(True, "grabbed"), (False, "waiting_season_metadata")]
)
async def test_ongoing_allows_released_single_unknown_closure_waits(tmp_path, future, expected):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    fixture.pack_offers = [fixture.release("partial-season", [1])]
    handler = metadata_handler(fixture, state="continuing", future=future)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1",
            fixture.reservation,
        )
    assert result == expected
    assert "partial-season" not in fixture.metadata
    assert fixture.posts == (["single"] if future else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token, native_ids, expected",
    [
        ("SE01.EP01", [1], "grabbed"),
        ("EP01", [1], "grabbed"),
        ("EP01", [99], "no_eligible_release"),
    ],
)
async def test_ongoing_ep_se_files_require_native_and_canonical_identity(
    tmp_path,
    token,
    native_ids,
    expected,
):
    from test_series_season_packs import _torrent

    from homeserver_control.domain.torrent_bytes import inspect_torrent

    fixture = SeriesFixture(tmp_path, episode_count=2)
    release = fixture.release("single", [1])
    torrent = _torrent(
        [(f"Series.{token}.1080p.mkv", 1_500_000_000), (f"Series.{token}.pt-BR.srt", 1000)]
    )
    parsed = inspect_torrent(torrent)
    fixture.torrents["single"] = torrent
    release.update(
        title=f"Series.{token}.1080p.WEB-DL",
        episodeIds=native_ids,
        infoHash=parsed.infohash,
        size=parsed.total_bytes,
    )
    fixture.episode_offers[1] = [release]
    handler = metadata_handler(fixture, state="continuing", future=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (
            await fixture.acquirer(client).acquire(
                "season:tmdb:101:1",
                fixture.reservation,
            )
            == expected
        )
