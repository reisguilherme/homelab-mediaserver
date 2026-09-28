import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.source_health import SourceHealthStore, TorrentHealth


def fixture_trial(tmp_path):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    result = repo.reserve(
        request_id="seerr:1",
        source_id="1",
        media_key="movie:tmdb:1",
        filesystem_id="fixture",
        budget_bytes=10_000,
        free_bytes=100_000,
        total_bytes=200_000,
    )
    registry = PermitRegistry(database)
    old = registry.issue(
        infohash="a" * 40,
        metadata_sha256="1" * 64,
        destination="/data/torrents",
        category="radarr",
        selected_files=("old/movie.mkv",),
        budget_bytes=10_000,
        reservation_id=result.reservation_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    registry.authorize(
        token=old.token,
        infohash=old.infohash,
        metadata_sha256=old.metadata_sha256,
        destination=old.destination,
        effect=lambda _: {"accepted": True, "infohash": old.infohash},
    )
    registry.set_quality(old.token, (1, 1080))
    probe = registry.issue_probe(
        old.token,
        infohash="b" * 40,
        metadata_sha256="2" * 64,
        selected_files=("new/movie.mkv",),
        quality_rank=(1, 1080),
        budget_bytes=20_000,
        capacity=CapacityEvidence(100_000, {old.infohash: 8000}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    registry.authorize(
        token=probe.token,
        infohash=probe.infohash,
        metadata_sha256=probe.metadata_sha256,
        destination=probe.destination,
        effect=lambda _: {"accepted": True, "infohash": probe.infohash},
    )
    health = SourceHealthStore(database)
    measured = time.time()

    def observation(torrent, left):
        return TorrentHealth(torrent.infohash, 30_000 - left, left, 5, 100, "downloading", 0.2)

    assert (
        health.probe_decision(
            old.permit_id,
            probe.permit_id,
            observation(old, 8000),
            observation(probe, 20_000),
            now=measured - 60,
        )
        == "observing"
    )
    assert (
        health.probe_decision(
            old.permit_id,
            probe.permit_id,
            observation(old, 7400),
            observation(probe, 2000),
            now=measured,
        )
        == "promote"
    )
    return database, repo, registry, old, probe, health


class Upstream:
    def __init__(self, old, probe):
        self.entries = {
            permit.infohash: {
                "hash": permit.infohash,
                "category": "radarr",
                "save_path": "/data/torrents",
                "amount_left": left,
                "progress": 0.2,
                "downloaded": 30_000 - left,
                "dlspeed": 100,
                "num_seeds": 5,
                "state": "downloading",
            }
            for permit, left in ((old, 7400), (probe, 2000))
        }
        self.fail_stop = False
        self.stops = []

    def read(self, path, params=None):
        return [self.entries[params["hashes"]]] if params else list(self.entries.values())

    def set_running(self, infohash, *, running):
        assert not running
        self.stops.append(infohash)
        if self.fail_stop:
            raise RuntimeError("simulated stop uncertainty")
        self.entries[infohash]["state"] = "stoppedDL"


@pytest.mark.asyncio
async def test_cancelled_pending_handover_stops_both_without_resume_and_releases_parent(tmp_path):
    _, repo, registry, old, probe, _ = fixture_trial(tmp_path)
    registry.promote_probe(probe.token)
    upstream = Upstream(old, probe)
    evidence = CapacityEvidence(
        100_000,
        {old.infohash: 7400, probe.infohash: 2000},
        paused_hashes=frozenset({old.infohash, probe.infohash}),
    )
    with repo._connect() as connection:
        connection.execute("UPDATE requests SET state='cancel_requested'")
    app = create_app(
        permits=registry, upstream=upstream, arr_token="secret", capacity_provider=lambda: evidence
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/internal/probe-decision",
            headers={"X-Arr-Token": "secret"},
            json={"permit_token": probe.token, "decision": "promote"},
        )
    assert response.json() == {"state": "cancelled"}
    assert set(upstream.stops) == {old.infohash, probe.infohash}
    assert registry.pending_handover(probe.token) is None
    assert registry.pending_bytes(evidence) == 0


@pytest.mark.asyncio
async def test_gateway_uncertain_handover_keeps_parent_capacity_and_paused_candidate_keeps_old(
    tmp_path,
):
    database, repo, registry, old, probe, health = fixture_trial(tmp_path)
    upstream = Upstream(old, probe)
    evidence = CapacityEvidence(100_000, {old.infohash: 7400, probe.infohash: 2000})
    app = create_app(
        permits=registry, upstream=upstream, arr_token="secret", capacity_provider=lambda: evidence
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        upstream.fail_stop = True
        with pytest.raises(RuntimeError, match="uncertainty"):
            await client.post(
                "/internal/probe-decision",
                headers={"X-Arr-Token": "secret"},
                json={"permit_token": probe.token, "decision": "promote"},
            )
        current = registry.get_for_reservation(old.reservation_id)
        assert current.infohash == probe.infohash
        assert registry.pending_bytes(evidence) == 9400
        assert registry.is_admitted(old.infohash)
        assert registry.pending_handover(current.token).permit_id == old.permit_id
        upstream.entries[probe.infohash]["state"] = "pausedDL"
        upstream.fail_stop = False
        stops_before = len(upstream.stops)

        async def capacity():
            return evidence

        acquirer = MovieAcquirer(
            repository=repo,
            permits=registry,
            radarr_url="http://radarr",
            radarr_api_key="secret",
            prowlarr_url="http://prowlarr",
            client=client,
            capacity_provider=capacity,
            health_store=health,
            gateway_url="http://gateway",
            arr_token="secret",
            torrent_store=TorrentArtifactStore(database),
            live_source_probes=True,
        )
        with pytest.raises(httpx.HTTPStatusError):
            await acquirer._monitor_probe(current)
        assert len(upstream.stops) == stops_before
        assert upstream.entries[old.infohash]["state"] == "downloading"
        upstream.entries[probe.infohash]["state"] = "downloading"
        assert await acquirer._monitor_probe(current) == "promoted"
        assert registry.pending_bytes(evidence) == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["cancel", "tombstone", "space", "mount"])
async def test_gateway_cannot_promote_with_invalidated_admission(tmp_path, block):
    _, repo, registry, old, probe, _ = fixture_trial(tmp_path)
    upstream = Upstream(old, probe)
    with repo._connect() as connection:
        if block == "cancel":
            connection.execute("UPDATE requests SET state='cancel_requested'")
        elif block == "tombstone":
            connection.execute("INSERT INTO tombstones VALUES ('movie:tmdb:1', 'now', 'test')")

    def capacity():
        if block == "mount":
            raise ValueError("wrong filesystem")
        return CapacityEvidence(
            0 if block == "space" else 100_000, {old.infohash: 7400, probe.infohash: 2000}
        )

    app = create_app(
        permits=registry, upstream=upstream, arr_token="secret", capacity_provider=capacity
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.post(
            "/internal/probe-decision",
            headers={"X-Arr-Token": "secret"},
            json={"permit_token": probe.token, "decision": "promote"},
        )
    assert response.status_code in (409, 503)
    assert old.infohash not in upstream.stops
    assert registry.get(old.token).state == "confirmed"
