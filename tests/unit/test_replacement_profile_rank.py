from contextlib import aclosing
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.source_health import TorrentHealth


def _torrent(name):
    filename = f"{name}.mkv".encode()
    return (
        b"d4:infod6:lengthi1000e4:name" + str(len(filename)).encode() + b":" + filename
        + b"12:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"ee"
    )


def _acquirer(tmp_path, client, policy):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservation = repo.reserve(
        request_id="fixture", source_id="fixture", media_key="movie:tmdb:1",
        filesystem_id="fixture", budget_bytes=10_000, free_bytes=100_000,
        total_bytes=200_000,
    )
    permits = PermitRegistry(repo.path)
    metadata = _torrent("old")
    parsed = inspect_torrent(metadata)
    old = permits.issue(
        infohash=parsed.infohash, metadata_sha256=parsed.metadata_sha256,
        destination="/data/torrents",
        category="radarr", selected_files=("old.mkv",), budget_bytes=10_000,
        reservation_id=reservation.reservation_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    torrents = TorrentArtifactStore(repo.path)
    torrents.put(old, metadata)
    acquirer = MovieAcquirer(
        repository=repo, permits=permits, client=client, radarr_url="http://radarr:7878",
        radarr_api_key="fixture", prowlarr_url="http://prowlarr:9696", release_policy=policy,
        torrent_store=torrents,
    )
    return acquirer, old


def _candidate(name, *, title="Fixture", languages=("en",)):
    release = {
        "guid": name, "title": title, "seeders": 20, "languages": languages,
        "quality": {"quality": {
            "source": "bluray", "modifier": "none", "resolution": 1080,
        }},
    }
    torrent = _torrent(name)
    parsed = inspect_torrent(torrent)
    return release, (
        parsed.infohash, parsed.metadata_sha256, (f"{name}.mkv",), parsed.total_bytes,
    ), None, torrent


async def _candidates(*candidates):
    for candidate in candidates:
        yield candidate


def _health(old):
    return TorrentHealth(old.infohash, 0, old.budget_bytes, 0, 0, "stalledDL", 0.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("measured", [(4,), (5, 4), (1, 2, 3, 4, 8)])
async def test_live_seed_measurement_rechecks_named_profile_minimum(tmp_path, measured):
    async with httpx.AsyncClient() as client:
        acquirer, old = _acquirer(tmp_path, client, ReleasePolicy.from_environment({}))
        candidates = [_candidate(chr(ord("b") + index)) for index in range(len(measured))]
        measurements = {candidate[1][0]: seeds for candidate, seeds in zip(
            candidates, measured, strict=True,
        )}

        async def probe(_torrent, infohash):
            return measurements[infohash]

        acquirer.availability_probe = probe
        selected = [candidate async for candidate in acquirer._prioritize_replacements(
            _candidates(*candidates), old=old, reason="stalled", health=_health(old),
            rank=acquirer._rank,
        )]
        assert [candidate[0]["seeders"] for candidate in selected] == [
            seeds for seeds in sorted(measured, reverse=True) if seeds >= 5
        ]


@pytest.mark.asyncio
async def test_live_probe_keeps_selected_dublado_original_language_context(tmp_path):
    async with httpx.AsyncClient() as client:
        acquirer, old = _acquirer(tmp_path, client, ReleasePolicy.from_environment({}))
        token = acquirer._selected_release_policy.set(
            acquirer.release_policy.for_profile("Dublado")
        )

        async def probe(_torrent, _infohash):
            return 5

        acquirer.availability_probe = probe
        try:
            selected = [candidate async for candidate in acquirer._prioritize_replacements(
                _candidates(_candidate("b")), old=old, reason="stalled", health=_health(old),
                rank=lambda release: acquirer._rank(release, original_language="en"),
            )]
            assert [candidate[0]["guid"] for candidate in selected] == ["b"]
        finally:
            acquirer._selected_release_policy.reset(token)
        assert acquirer.release_policy.profile_name == "Standard"


@pytest.mark.asyncio
@pytest.mark.parametrize("live_probes", [False, True])
@pytest.mark.parametrize("profile", ["Standard", "Max Quality", "Dublado"])
async def test_manual_replacement_uses_selected_profile_instead_of_old_uhd_floor(
    tmp_path, live_probes, profile,
):
    policy = ReleasePolicy.from_environment({}).for_profile(profile)
    async with httpx.AsyncClient() as client:
        acquirer, old = _acquirer(tmp_path, client, policy)
        old.quality_rank = (3, 2160, 0, 0, 0, 0)
        acquirer._manual_permit_id = old.permit_id
        acquirer.live_source_probes = live_probes
        selected = [candidate async for candidate in acquirer._prioritize_replacements(
            _candidates(_candidate("b")), old=old, reason="stalled", health=_health(old),
            rank=lambda release: acquirer._rank(release, original_language="en"),
        )]
        assert [candidate[0]["guid"] for candidate in selected] == ["b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy,manual,title", [
    (True, True, "Fixture"), (False, False, "Fixture"),
    (False, True, "Fixture Extended"),
])
async def test_manual_profile_exception_preserves_other_replacement_guards(
    tmp_path, legacy, manual, title,
):
    policy = ReleasePolicy() if legacy else ReleasePolicy.from_environment({})
    async with httpx.AsyncClient() as client:
        acquirer, old = _acquirer(tmp_path, client, policy)
        old.quality_rank = (3, 2160, 0, 0, 0, 0)
        if manual:
            acquirer._manual_permit_id = old.permit_id
        acquirer.live_source_probes = True
        assert [candidate async for candidate in acquirer._prioritize_replacements(
            _candidates(_candidate("b", title=title)),
            old=old, reason="stalled", health=_health(old), rank=acquirer._rank,
        )] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_seeds", [None, 0, 5])
async def test_opt_in_replacement_probe_does_not_pull_fallback_before_primary_exhaustion(
    tmp_path, primary_seeds,
):
    policy = ReleasePolicy.from_environment({"HOMESERVER_RELEASE_FALLBACK_INDEXERS": "1337x"})
    async with httpx.AsyncClient() as client:
        acquirer, old = _acquirer(tmp_path, client, policy)
        primary, fallback = _candidate("primary"), _candidate("fallback")
        primary[0].update(indexer="UIndex", seeders=5)
        fallback[0].update(indexer="1337x", seeders=100)
        inspected, probed = [], []

        async def eligible(group):
            for release in group:
                inspected.append(release["guid"])
                yield primary if release["guid"] == "primary" else fallback

        async def probe(_torrent, infohash):
            name = "primary" if infohash == primary[1][0] else "fallback"
            probed.append(name)
            return primary_seeds if name == "primary" else 100

        acquirer.availability_probe = probe
        async with aclosing(acquirer._preferred_indexer_candidates(
            [fallback[0], primary[0]], eligible=eligible, rank=acquirer._rank,
            transform=lambda candidates: acquirer._prioritize_replacements(
                candidates, old=old, reason="stalled", health=_health(old), rank=acquirer._rank,
            ),
        )) as candidates:
            first = await anext(candidates)
            if primary_seeds == 0:
                assert first[0]["guid"] == "fallback"
                assert inspected == probed == ["primary", "fallback"]
            else:
                assert first[0]["guid"] == "primary"
                assert inspected == probed == ["primary"]
                # A consumer may decline this source, then request the fallback.
                assert (await anext(candidates))[0]["guid"] == "fallback"
                assert inspected == probed == ["primary", "fallback"]
