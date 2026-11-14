import json

import httpx
import pytest
from test_series_pack_acquisition import SeriesFixture
from test_series_season_packs import _torrent

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.source_health import SourceHealthStore


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'no_candidate', 'space', 'completed', 'cancelled'])
@pytest.mark.parametrize('absolute_names', [False, True])
async def test_manual_full_season_keeps_old_until_safe_candidate_and_rebinds_all_episodes(
    tmp_path, failure, absolute_names,
):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.pack_offers = [fixture.release('old-pack', [1, 2], seeds=20)]
    if absolute_names:
        fixture.pack_offers[0]['languages'] = ['English']
    paused, added = set(), []
    old = None

    def handler(request):
        if request.url.path == '/internal/torrent-health':
            source = fixture.permits.get(request.headers['X-Admission-Permit'])
            return httpx.Response(200, json={
                'hash': source.infohash, 'downloaded': 100,
                'amount_left': 0 if failure == 'completed' else source.budget_bytes - 100,
                'state': 'stalledDL', 'num_seeds': 0, 'dlspeed': 0,
                'progress': 1 if failure == 'completed' else .01,
                'name': 'Fixture S01 1080p WEB-DL',
            })
        if request.url.path == '/internal/source-state':
            body = json.loads(request.content)
            source = fixture.permits.get(body['permit_token'])
            (paused.add if body['action'] == 'stop' else paused.discard)(source.infohash)
        if request.url.path == '/1/download' and old is not None and failure == 'cancelled':
            with fixture.repo._connect() as connection:
                connection.execute("UPDATE requests SET state='cancel_requested'")
        if request.url.path == '/api/v2/torrents/add':
            source = fixture.permits.get(request.headers['X-Admission-Permit'])
            fixture.confirm(source)
            added.append(source.infohash)
            return httpx.Response(200, json={'accepted': True})
        response = fixture.handler(request)
        if absolute_names and request.url.path == '/api/v3/series':
            rows = response.json()
            rows[0].update(genres=['Animation'], originalLanguage='English')
            return httpx.Response(200, json=rows)
        if absolute_names and request.url.path == '/api/v3/episode':
            rows = response.json()
            for row in rows:
                row['absoluteEpisodeNumber'] = row['episodeNumber'] + 10
            return httpx.Response(200, json=rows)
        return response

    async def capacity():
        return CapacityEvidence(
            100 if failure == 'space' else fixture.free_bytes,
            fixture.remaining, paused_hashes=frozenset(paused),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = fixture.acquirer(
            client, prefer_season_pack=True, health_store=SourceHealthStore(fixture.repo.path),
        )
        assert await acquirer.acquire('season:tmdb:101:1', fixture.reservation) == 'grabbed'
        old = fixture.permits.get_for_reservation(fixture.reservation, scope_key='S01PACK')
        fixture.pack_offers = [fixture.release(
            'better-pack', [1, 2], seeds=0 if failure == 'no_candidate' else 100,
        )]
        if absolute_names:
            torrent = _torrent([
                (f'Fixture - {number:03d}{suffix}', size)
                for number in (11, 12)
                for suffix, size in ((' [1080p].mkv', 1_500_000_000), ('.pt-BR.srt', 1000))
            ])
            metadata = inspect_torrent(torrent)
            fixture.torrents['better-pack'] = torrent
            fixture.pack_offers[0].update(
                infoHash=metadata.infohash, size=metadata.total_bytes, languages=['English'],
            )
        acquirer.capacity_provider = capacity
        result = await acquirer.manual_replace(
            'season:tmdb:101:1', fixture.reservation, old.permit_id, scope_key='S01PACK',
        )
    if failure is None:
        assert result == 'replaced'
        new = fixture.permits.get_for_reservation(fixture.reservation, scope_key='S01PACK')
        assert new.permit_id != old.permit_id
        assert new.state == 'confirmed'
        assert len(added) == 1 and paused == {old.infohash}
        for scope in ('S01E01', 'S01E02'):
            child = fixture.permits.get_for_reservation(fixture.reservation, scope_key=scope)
            assert child.season_pack_parent_id == new.permit_id
            assert fixture.torrents['better-pack'] == acquirer.torrent_store.get(new)
    else:
        assert result in {
            'no_eligible_release', 'waiting_space', 'already_completed', 'reservation_inactive',
        }
        assert not added and not paused
        assert fixture.permits.get(old.token).state == 'confirmed'
