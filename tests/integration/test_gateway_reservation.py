from datetime import UTC, datetime, timedelta

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository


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
