import json
import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
from test_series_pack_acquisition import SeriesFixture

from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.source_health import SourceHealthStore, TorrentHealth


@pytest.mark.asyncio
async def test_pending_probe_does_not_block_admission_of_later_episode(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    first = fixture.existing(1)
    fixture.remaining[first.infohash] = first.budget_bytes
    fixture.episode_offers[2] = [fixture.release("second", [2])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        acquirer = fixture.acquirer(client)
        acquirer._monitor_probe = AsyncMock(return_value="probing")
        assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
    assert fixture.posts == ["second"]
    assert fixture.permits.get(first.token).state == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "recovered", "space", "deleted"])
async def test_complete_pack_recovers_only_stalled_individual_with_fresh_guards(tmp_path, failure):
    fixture = SeriesFixture(tmp_path, episode_count=3)
    sources = [fixture.existing(n) for n in (1, 2, 3)]
    for source in sources:
        fixture.permits.set_quality(source.token, (1, 1080, 0, 0, 0, 0))
    fixture.remaining[sources[0].infohash] = sources[0].budget_bytes
    fixture.remaining[sources[2].infohash] = sources[2].budget_bytes
    fixture.pack_offers = [fixture.release("pack", [1, 2, 3], seeds=40)]
    paused, added = set(), []
    health_store = SourceHealthStore(fixture.repo.path, slow_replacement_enabled=False)
    deleted = False

    def handler(request):
        nonlocal deleted
        if request.url.path == "/internal/source-state":
            body = json.loads(request.content)
            old = fixture.permits.get(body['permit_token'])
            (paused.add if body['action'] == 'stop' else paused.discard)(old.infohash)
            deleted |= failure == "deleted" and body['action'] == 'stop'
        if request.url.path == "/api/v2/torrents/add":
            parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
            assert parent is not None
            fixture.confirm(parent)
            added.append(parent.infohash)
            return httpx.Response(200, json={"accepted": True})
        return fixture.handler(request)

    async def capacity():
        return CapacityEvidence(
            free_bytes=100 if failure == "space" else fixture.free_bytes,
            remaining_by_hash=fixture.remaining, paused_hashes=frozenset(paused),
        )

    def health(source, *, refreshed=False):
        active = source.infohash == sources[2].infohash or (refreshed and failure == "recovered")
        return TorrentHealth(
            infohash=source.infohash, state="downloading" if active else "stalledDL",
            progress=0.1, amount_left=fixture.remaining[source.infohash],
            downloaded=100, dlspeed=1000 if active else 0, num_seeds=3 if active else 0,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = fixture.acquirer(client, prefer_season_pack=True, health_store=health_store)
        acquirer.capacity_provider = capacity
        acquirer.is_tombstoned = lambda _: deleted
        acquirer.availability_probe = AsyncMock(return_value=15)
        acquirer._source_status = AsyncMock(side_effect=lambda p: (
            "stalled" if p.infohash == sources[0].infohash else None, health(p),
        ))
        acquirer._read_source_health = AsyncMock(
            side_effect=lambda p: (health(p, refreshed=True), {}),
        )
        try:
            await acquirer._acquire_season_pack(
                series={"id": 1, "tmdbId": 101, "runtime": 45}, season=1,
                reservation_id=fixture.reservation,
                episode_rows=fixture.handler(httpx.Request("GET", "http://sonarr/api/v3/episode")).json(),
                imported_episode_ids=set(),
            )
        except PermissionError:
            assert failure is not None
    assert fixture.permits.get(sources[1].token).state == "confirmed"
    assert fixture.permits.get(sources[2].token).state == "confirmed"
    if failure:
        assert not added
        assert fixture.permits.get(sources[0].token).state == "confirmed"
        assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None
        assert (sources[0].infohash in paused) == (failure == "deleted")
    else:
        assert len(added) == 1
        parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
        assert fixture.permits.get(sources[0].token).state == "superseded"
        assert fixture.permits.get_for_reservation(
            fixture.reservation, scope_key="S01E01",
        ).season_pack_parent_id == parent.permit_id
        assert paused == {sources[0].infohash}


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_bound_pack_recovers_after_worker_restart_even_when_authorization_expired(
    tmp_path, expired,
):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    source = fixture.existing(1)
    fixture.permits.set_quality(source.token, (1, 1080, 0, 0, 0, 0))
    fixture.remaining[source.infohash] = source.budget_bytes
    fixture.pack_offers = [fixture.release("pack", [1, 2], seeds=40)]
    paused = set()
    healthy = SourceHealthStore(fixture.repo.path)
    health = TorrentHealth(
        infohash=source.infohash, state="stalledDL", progress=.1,
        amount_left=source.budget_bytes, downloaded=0, dlspeed=0, num_seeds=0,
    )

    async def capacity():
        return CapacityEvidence(fixture.free_bytes, fixture.remaining,
                                paused_hashes=frozenset(paused))

    def handler(request):
        if request.url.path == "/internal/source-state":
            if json.loads(request.content)['action'] == 'stop':
                paused.add(source.infohash)
        if request.url.path == "/api/v2/torrents/add":
            parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
            fixture.confirm(parent)
            return httpx.Response(200, json={"accepted": True})
        return fixture.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = fixture.acquirer(client, prefer_season_pack=True, health_store=healthy)
        acquirer.capacity_provider = capacity
        acquirer.availability_probe = AsyncMock(return_value=15)
        acquirer._source_status = AsyncMock(return_value=("stalled", health))
        acquirer._read_source_health = AsyncMock(return_value=(health, {}))
        acquirer._add_verified_torrent = AsyncMock(side_effect=httpx.ReadTimeout("fixture"))
        args = dict(
            series={"id": 1, "tmdbId": 101, "runtime": 45}, season=1,
            reservation_id=fixture.reservation,
            episode_rows=fixture.handler(httpx.Request("GET", "http://sonarr/api/v3/episode")).json(),
            imported_episode_ids=set(),
        )
        with pytest.raises(httpx.ReadTimeout):
            await acquirer._acquire_season_pack(**args)
        assert fixture.permits.get(source.token).state == "superseded"
        parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
        assert acquirer.torrent_store.get(parent) is not None
        if expired:
            with sqlite3.connect(fixture.repo.path) as connection:
                connection.execute("UPDATE gateway_permits SET expires_at=? WHERE token=?",
                                   ("2000-01-01T00:00:00+00:00", parent.token))
        restarted = fixture.acquirer(client, prefer_season_pack=True)
        restarted.capacity_provider = capacity
        assert await restarted._acquire_season_pack(**args) == "grabbed"
        parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
        assert parent.state == "confirmed"
        assert fixture.permits.get_for_reservation(
            fixture.reservation, scope_key="S01E01",
        ).season_pack_parent_id == parent.permit_id
        assert fixture.permits.get(source.token).state == "superseded"
