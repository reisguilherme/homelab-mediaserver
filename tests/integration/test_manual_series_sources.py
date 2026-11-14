import json

import httpx
import pytest
from test_series_pack_acquisition import SeriesFixture

from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.source_health import SourceHealthStore


@pytest.mark.asyncio
@pytest.mark.parametrize('missing_episode', [False, True])
async def test_manual_episode_targets_only_selected_source_even_outside_download_window(
    tmp_path, missing_episode,
):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.ongoing = True
    first = fixture.existing(1)
    old = fixture.existing(2)
    fixture.remaining[first.infohash] = first.budget_bytes
    fixture.remaining[old.infohash] = old.budget_bytes
    fixture.episode_offers[2] = [fixture.release('alternative-2', [2], seeds=50)]
    paused, added = set(), []

    def handler(request):
        if request.url.path == '/api/v3/episode' and missing_episode:
            payload = fixture.handler(request).json()
            return httpx.Response(200, json=payload[:1])
        if request.url.path == '/internal/torrent-health':
            source = fixture.permits.get(request.headers['X-Admission-Permit'])
            return httpx.Response(200, json={
                'hash': source.infohash, 'downloaded': 10,
                'amount_left': source.budget_bytes - 10, 'state': 'stalledDL',
                'num_seeds': 0, 'dlspeed': 0, 'progress': .01,
            })
        if request.url.path == '/internal/source-state':
            body = json.loads(request.content)
            source = fixture.permits.get(body['permit_token'])
            if body['action'] == 'stop':
                paused.add(source.infohash)
        if request.url.path == '/api/v2/torrents/add':
            source = fixture.permits.get(request.headers['X-Admission-Permit'])
            fixture.confirm(source)
            added.append(source.infohash)
            return httpx.Response(200, json={'accepted': True})
        return fixture.handler(request)

    async def capacity():
        return CapacityEvidence(
            fixture.free_bytes, fixture.remaining, paused_hashes=frozenset(paused),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = fixture.acquirer(
            client, download_window=1, health_store=SourceHealthStore(fixture.repo.path),
        )
        acquirer.capacity_provider = capacity
        result = await acquirer.manual_replace(
            'season:tmdb:101:1', fixture.reservation, old.permit_id, scope_key='S01E02',
        )
    assert fixture.permits.get(first.token).state == 'confirmed'
    assert first.infohash not in paused
    if missing_episode:
        assert result == 'source_changed'
        assert not added and not paused
    else:
        assert result == 'replaced'
        assert len(added) == 1
        assert paused == {old.infohash}
        assert fixture.permits.get(old.token).state == 'superseded'
