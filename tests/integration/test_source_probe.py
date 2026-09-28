from datetime import UTC, datetime, timedelta

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.source_health import SourceHealthStore, TorrentHealth


def _source(tmp_path):
    path = tmp_path / "control.sqlite"
    repo = ReservationRepository(path)
    repo.initialize()
    result = repo.reserve(
        request_id="seer:1",
        source_id="1",
        media_key="movie:tmdb:1",
        filesystem_id="fixture",
        budget_bytes=10_000,
        free_bytes=100_000,
        total_bytes=200_000,
    )
    registry = PermitRegistry(path)
    source = registry.issue(
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
        token=source.token,
        infohash=source.infohash,
        metadata_sha256=source.metadata_sha256,
        destination=source.destination,
        effect=lambda _: {"accepted": True, "infohash": source.infohash},
    )
    registry.set_quality(source.token, (1, 1080))
    return repo, registry, registry.get(source.token)


def _probe(registry, source, *, free=100_000):
    return registry.issue_probe(
        source.token,
        infohash="b" * 40,
        metadata_sha256="2" * 64,
        selected_files=("new/movie.mkv",),
        quality_rank=(1, 1080),
        budget_bytes=20_000,
        capacity=CapacityEvidence(free_bytes=free, remaining_by_hash={source.infohash: 8000}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def test_probe_keeps_current_primary_and_counts_both_remaining_sizes(tmp_path):
    _, registry, source = _source(tmp_path)
    with pytest.raises(PermissionError, match="waiting_space"):
        _probe(registry, source, free=27_999)
    probe = _probe(registry, source)
    registry = PermitRegistry(tmp_path / "control.sqlite")
    assert registry.get_for_reservation(source.reservation_id).permit_id == source.permit_id
    assert registry.get_probe(source.permit_id).permit_id == probe.permit_id
    assert (
        registry.pending_bytes(
            CapacityEvidence(free_bytes=100_000, remaining_by_hash={source.infohash: 8000})
        )
        == 28_000
    )
    with pytest.raises(PermissionError, match="probe_already_active"):
        _probe(registry, source)


def test_promotion_requires_confirmed_candidate_and_preserves_history(tmp_path):
    _, registry, source = _source(tmp_path)
    probe = _probe(registry, source)
    with pytest.raises(PermissionError, match="confirmed_probe_required"):
        registry.promote_probe(probe.token)
    registry.authorize(
        token=probe.token,
        infohash=probe.infohash,
        metadata_sha256=probe.metadata_sha256,
        destination=probe.destination,
        effect=lambda _: {"accepted": True, "infohash": probe.infohash},
    )
    assert len(registry.list_active_confirmed_movies()) == 1
    promoted = registry.promote_probe(probe.token)
    assert promoted.probe_parent_id is None
    assert registry.get_for_reservation(source.reservation_id).infohash == probe.infohash
    assert registry.get(source.token).state == "superseded"
    assert registry.get_probe(source.permit_id) is None


def test_cancelled_reservation_cannot_promote_or_create_probe(tmp_path):
    repo, registry, source = _source(tmp_path)
    probe = _probe(registry, source)
    registry.authorize(
        token=probe.token,
        infohash=probe.infohash,
        metadata_sha256=probe.metadata_sha256,
        destination=probe.destination,
        effect=lambda _: {"accepted": True, "infohash": probe.infohash},
    )
    with repo._connect() as connection:
        connection.execute(
            "UPDATE reservations SET state = 'released' WHERE id = ?", (source.reservation_id,)
        )
    with pytest.raises(PermissionError, match="reservation_required"):
        registry.promote_probe(probe.token)
    assert registry.get(source.token).state == "confirmed"


def _health(infohash, left, speed=1):
    return TorrentHealth(infohash, 0, left, 5, speed, "downloading", 0.2)


def test_probe_measures_transfer_and_eta_instead_of_reported_seeds(tmp_path):
    path = tmp_path / "health.sqlite"
    store = SourceHealthStore(path, slow_seconds=300)
    assert (
        store.probe_decision(
            "old", "new", _health("a" * 40, 200_000_000), _health("b" * 40, 400_000_000), now=1000
        )
        == "observing"
    )
    # Candidate is twice as large and twice as fast: no ETA improvement.
    assert (
        SourceHealthStore(path).probe_decision(
            "old", "new", _health("a" * 40, 185_000_000), _health("b" * 40, 370_000_000), now=1060
        )
        == "reject"
    )
    assert (
        store.probe_decision(
            "old",
            "faster",
            _health("a" * 40, 185_000_000),
            _health("c" * 40, 300_000_000),
            now=1070,
        )
        == "observing"
    )
    assert (
        store.probe_decision(
            "old",
            "faster",
            _health("a" * 40, 170_000_000),
            _health("c" * 40, 210_000_000),
            now=1130,
        )
        == "promote"
    )


def test_current_completion_wins_and_exception_survives_restart(tmp_path):
    path = tmp_path / "health.sqlite"
    store = SourceHealthStore(path)
    store.protect_source("a" * 40)
    store = SourceHealthStore(path)
    assert store.is_protected("a" * 40)
    assert not store.is_protected("b" * 40)
    assert (
        store.probe_decision("old", "new", _health("a" * 40, 0), _health("b" * 40, 1), now=1000)
        == "reject"
    )


@pytest.mark.parametrize("block", ["cancel", "tombstone"])
def test_probe_admission_rechecks_cancellation_and_tombstones(tmp_path, block):
    repo, registry, source = _source(tmp_path)
    with repo._connect() as connection:
        if block == "cancel":
            connection.execute("UPDATE requests SET state='cancel_requested'")
        else:
            connection.execute("INSERT INTO tombstones VALUES ('movie:tmdb:1', 'now', 'test')")
    with pytest.raises(PermissionError, match="reservation_required|media_tombstoned"):
        _probe(registry, source)


def test_probe_old_pause_recovery_and_worker_gap_are_not_speed_proof(tmp_path):
    from dataclasses import replace

    store = SourceHealthStore(tmp_path / "health.sqlite")
    old, new = _health("a" * 40, 10000000), _health("b" * 40, 20000000)
    assert store.probe_decision("o", "n", old, new, now=1000) == "observing"
    assert store.probe_decision("o", "n", replace(old, state="pausedDL"), new, now=1060) == "reject"
    assert (
        store.probe_decision("o", "n", replace(old, dlspeed=2 * 1024 * 1024), new, now=1060)
        == "reject"
    )
    assert (
        store.probe_decision(
            "o", "n", replace(old, amount_left=9000000), replace(new, amount_left=1000000), now=1300
        )
        == "observing"
    )


def test_all_supported_edition_markers_are_distinct():
    from homeserver_control.worker.source_probe import edition_identity

    for marker in ("Uncut", "Special.Edition", "Final.Cut", "Ultimate.Cut", "Extended"):
        assert edition_identity("Movie." + marker + ".1080p.WEB-DL") != "standard"
