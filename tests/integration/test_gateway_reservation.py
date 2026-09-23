from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import CapacityEvidence


def test_headerless_arr_permit_requires_live_reservation(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    identity = {
        "infohash": "a" * 40,
        "metadata_sha256": "b" * 64,
        "destination": "/data/torrents",
        "category": "sonarr",
    }
    expires = datetime.now(UTC) + timedelta(minutes=5)
    permits.issue(**identity, expires_at=expires, budget_bytes=123)
    with pytest.raises(PermissionError):
        permits.find_for_metadata(**identity)

    reservation = repository.reserve(
        request_id="request-1",
        source_id="1",
        media_key="episode:1",
        filesystem_id="test-uuid",
        budget_bytes=1000,
        free_bytes=100_000_000_000,
        total_bytes=200_000_000_000,
    )
    assert reservation.accepted and reservation.reservation_id
    permit = permits.issue(
        **identity,
        expires_at=expires,
        reservation_id=reservation.reservation_id,
        budget_bytes=123,
    )
    assert permits.find_for_metadata(**identity).permit_id == permit.permit_id
    with pytest.raises(PermissionError):
        permits.find_for_metadata(**{**identity, "category": "radarr"})


def test_magnet_permit_requires_inspected_files_and_live_reservation(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    identity = {
        "infohash": "a" * 40,
        "destination": "/data/torrents",
        "category": "radarr",
    }
    expires = datetime.now(UTC) + timedelta(minutes=5)
    permits.issue(
        **identity, expires_at=expires, metadata_sha256="b" * 64,
        selected_files=("film.mkv",), budget_bytes=123,
    )
    with pytest.raises(PermissionError):
        permits.find_for_magnet(**identity)

    reservation = repository.reserve(
        request_id="request-2", source_id="2", media_key="movie:tmdb:10",
        filesystem_id="test-uuid", budget_bytes=1000,
        free_bytes=100_000_000_000, total_bytes=200_000_000_000,
    )
    assert reservation.accepted and reservation.reservation_id
    permit = permits.issue(
        **identity, expires_at=expires, reservation_id=reservation.reservation_id,
        metadata_sha256="b" * 64, selected_files=("film.mkv",), budget_bytes=123,
    )
    assert permits.find_for_magnet(**identity).permit_id == permit.permit_id
    with pytest.raises(PermissionError):
        permits.find_for_magnet(**{**identity, "category": "sonarr"})


def test_season_reservation_authorizes_distinct_episodes_with_aggregate_cap(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="seerr:3:4", source_id="3:4",
        media_key="season:tmdb:97546:4", filesystem_id="test-uuid",
        budget_bytes=1000, free_bytes=100_000_000_000,
        total_bytes=200_000_000_000,
    )
    assert reservation.reservation_id
    common = {
        "reservation_id": reservation.reservation_id,
        "destination": "/data/torrents", "category": "sonarr",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "metadata_sha256": "b" * 64, "selected_files": ("episode.mkv",),
    }
    first = permits.issue(
        **common, scope_key="S04E01", infohash="a" * 40, budget_bytes=400
    )
    second = permits.issue(
        **common, scope_key="S04E02", infohash="c" * 40, budget_bytes=500
    )
    assert permits.get_for_reservation(reservation.reservation_id, scope_key="S04E01") == first
    assert permits.get_for_reservation(reservation.reservation_id, scope_key="S04E02") == second
    with pytest.raises(PermissionError, match="season_budget_exceeded"):
        permits.issue(
            **common, scope_key="S04E03", infohash="d" * 40, budget_bytes=200
        )


def test_exact_byte_claim_ignores_old_speculative_reservations(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    for number, budget in ((1, 80_000_000_000), (2, 0)):
        result = repository.reserve(
            request_id=f"request-{number}", source_id=str(number),
            media_key=f"movie:tmdb:{number}", filesystem_id="test-uuid",
            budget_bytes=budget, free_bytes=200_000_000_000,
            total_bytes=300_000_000_000,
        )
        assert result.accepted
        if number == 2:
            reservation_id = result.reservation_id
    assert reservation_id
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reservation_id, selected_files=("film.mkv",),
        budget_bytes=2_000_000_000,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=3_000_000_000, remaining_by_hash={}),
    )
    assert permit.budget_bytes == 2_000_000_000
    assert permits.find_for_metadata(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
    ).permit_id == permit.permit_id


def test_exact_claim_counts_pending_and_stale_snapshot(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    common = {
        "destination": "/data/torrents", "category": "sonarr",
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "metadata_sha256": "b" * 64, "selected_files": ("episode.mkv",),
    }
    result = repository.reserve(
        request_id="season", source_id="season", media_key="season:tmdb:2:1",
        filesystem_id="test-uuid", budget_bytes=0,
        free_bytes=10_000_000_000, total_bytes=20_000_000_000,
    )
    assert result.accepted and result.reservation_id
    first = permits.issue(
        **common, reservation_id=result.reservation_id, scope_key="S01E01",
        infohash="a" * 40, budget_bytes=2_000_000_000,
        capacity=CapacityEvidence(free_bytes=3_000_000_000, remaining_by_hash={}),
    )
    with pytest.raises(PermissionError, match="waiting_space"):
        permits.issue(
            **common, reservation_id=result.reservation_id, scope_key="S01E02",
            infohash="c" * 40, budget_bytes=2_000_000_000,
            capacity=CapacityEvidence(free_bytes=3_000_000_000, remaining_by_hash={}),
        )
    assert permits.get_for_reservation(result.reservation_id, scope_key="S01E02") is None
    permits.issue(
        **common, reservation_id=result.reservation_id, scope_key="S01E02",
        infohash="c" * 40, budget_bytes=2_000_000_000,
        capacity=CapacityEvidence(free_bytes=3_000_000_000,
                                  remaining_by_hash={first.infohash: 0}),
    )


def test_unmanaged_queue_bytes_are_claimed_before_new_permit(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="movie", source_id="movie", media_key="movie:tmdb:3",
        filesystem_id="test-uuid", budget_bytes=0,
        free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    with pytest.raises(PermissionError, match="waiting_space"):
        permits.issue(
            infohash="a" * 40, destination="/data/torrents", category="radarr",
            reservation_id=reservation.reservation_id, budget_bytes=2000,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            capacity=CapacityEvidence(free_bytes=3000, remaining_by_hash={},
                                      other_pending_bytes=1500),
        )


def test_old_speculative_budgets_normalize_to_existing_permits(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservations = []
    for number in (1, 2):
        result = repository.reserve(
            request_id=str(number), source_id=str(number),
            media_key=f"movie:tmdb:{number}", filesystem_id="test-uuid",
            budget_bytes=80_000_000_000, free_bytes=200_000_000_000,
            total_bytes=300_000_000_000,
        )
        assert result.reservation_id
        reservations.append(result.reservation_id)
    permits.issue(
        infohash="a" * 40, destination="/data/torrents", category="radarr",
        reservation_id=reservations[1], budget_bytes=2_000_000_000,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    repository.normalize_verified_budgets()
    assert repository.active_reservation(reservations[0])["budget_bytes"] == 0
    assert repository.active_reservation(reservations[1])["budget_bytes"] == 2_000_000_000


def test_expired_unstarted_permit_releases_claim(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="movie", source_id="movie", media_key="movie:tmdb:4",
        filesystem_id="test-uuid", budget_bytes=0,
        free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    permits.issue(
        infohash="a" * 40, destination="/data/torrents", category="radarr",
        reservation_id=reservation.reservation_id, budget_bytes=2000,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        capacity=CapacityEvidence(free_bytes=3000, remaining_by_hash={}),
    )
    assert permits.retire_expired_authorized(reservation.reservation_id) == 1
    assert permits.get_for_reservation(reservation.reservation_id) is None
    assert repository.active_reservation(reservation.reservation_id)["budget_bytes"] == 0


def test_concurrent_exact_claims_cannot_spend_same_free_bytes(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    ids = []
    for number in (1, 2):
        reservation = repository.reserve(
            request_id=str(number), source_id=str(number),
            media_key=f"movie:tmdb:{number}", filesystem_id="test-uuid",
            budget_bytes=0, free_bytes=3000, total_bytes=6000,
        )
        assert reservation.reservation_id
        ids.append(reservation.reservation_id)

    def claim(number):
        try:
            permits.issue(
                infohash=str(number) * 40, destination="/data/torrents",
                category="radarr", reservation_id=ids[number - 1],
                budget_bytes=2000, expires_at=datetime.now(UTC) + timedelta(minutes=5),
                capacity=CapacityEvidence(free_bytes=3000, remaining_by_hash={}),
            )
            return True
        except PermissionError as error:
            assert str(error) == "waiting_space"
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, (1, 2))) == [False, True]
