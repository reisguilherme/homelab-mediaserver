import json
import sqlite3
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


def test_new_exact_claim_ignores_paused_confirmed_future_but_counts_pending(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="season", source_id="season", media_key="season:tmdb:2:1",
        filesystem_id="test-uuid", budget_bytes=0,
        free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    common = {
        "destination": "/data/torrents", "category": "sonarr",
        "reservation_id": reservation.reservation_id,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    first = permits.issue(
        **common, scope_key="S01E01", infohash="a" * 40, budget_bytes=2000,
        capacity=CapacityEvidence(free_bytes=3000, remaining_by_hash={}),
    )
    permits.authorize(
        token=first.token, infohash=first.infohash, destination=first.destination,
        effect=lambda _: {"accepted": True},
    )
    paused_evidence = CapacityEvidence(
        free_bytes=3000, remaining_by_hash={first.infohash: 1500},
        paused_hashes=frozenset({first.infohash}),
    )
    assert permits.pending_bytes(paused_evidence) == 0
    assert permits.pending_bytes(paused_evidence, include_infohash=first.infohash) == 1500
    second = permits.issue(
        **common, scope_key="S01E02", infohash="b" * 40, budget_bytes=2000,
        capacity=paused_evidence,
    )
    assert second.budget_bytes == 2000
    with pytest.raises(PermissionError, match="waiting_space"):
        permits.issue(
            **common, scope_key="S01E03", infohash="c" * 40, budget_bytes=2000,
            capacity=paused_evidence,
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


def test_confirmed_source_replacement_preserves_old_permit_and_exact_capacity(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="seerr:replace", source_id="replace",
        media_key="season:tmdb:2:1", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    old = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="sonarr",
        reservation_id=reservation.reservation_id, scope_key="S01E01",
        selected_files=("old.mkv",), budget_bytes=2_000,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    stopped = CapacityEvidence(
        free_bytes=3_000, remaining_by_hash={old.infohash: 1_500},
        paused_hashes=frozenset({old.infohash}),
    )
    replacement = permits.replace_confirmed(
        old.token, infohash="c" * 40, metadata_sha256="d" * 64,
        selected_files=("new.mkv",), budget_bytes=2_500,
        capacity=stopped, expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert replacement.state == "authorized"
    assert replacement.reservation_id == old.reservation_id
    assert replacement.scope_key == old.scope_key
    assert replacement.category == old.category
    assert replacement.destination == old.destination
    assert PermitRegistry(database).get(old.token).state == "superseded"
    assert PermitRegistry(database).had_superseded(
        reservation.reservation_id, scope_key="S01E01"
    )
    assert permits.get_for_reservation(
        reservation.reservation_id, scope_key="S01E01"
    ) == replacement
    assert not permits.is_admitted(old.infohash)
    assert repository.active_reservation(reservation.reservation_id)["budget_bytes"] == 2_500
    repository.normalize_verified_budgets()
    assert repository.active_reservation(reservation.reservation_id)["budget_bytes"] == 2_500
    with pytest.raises(PermissionError, match="permit_superseded"):
        permits.authorize(
            token=old.token, infohash=old.infohash, destination=old.destination,
            metadata_sha256=old.metadata_sha256,
            effect=lambda _: {"accepted": True},
        )
    with pytest.raises(PermissionError):
        permits.find_for_metadata(
            infohash=old.infohash, metadata_sha256=old.metadata_sha256,
            destination=old.destination, category=old.category,
        )
    permits.authorize(
        token=replacement.token, infohash=replacement.infohash,
        destination=replacement.destination, metadata_sha256=replacement.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    with pytest.raises(PermissionError, match="replacement_limit"):
        permits.replace_confirmed(
            replacement.token, infohash="e" * 40, metadata_sha256="f" * 64,
            selected_files=("third.mkv",), budget_bytes=2_500,
            capacity=CapacityEvidence(
                free_bytes=3_000, remaining_by_hash={replacement.infohash: 2_000},
                paused_hashes=frozenset({replacement.infohash}),
            ), expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )


def test_source_replacement_rejects_unstopped_reused_or_unaffordable_candidate(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="seerr:film", source_id="film",
        media_key="movie:tmdb:3", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    old = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reservation.reservation_id, selected_files=("old.mkv",),
        budget_bytes=2_000, expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    candidate = {
        "infohash": "c" * 40, "metadata_sha256": "d" * 64,
        "selected_files": ("new.mkv",), "budget_bytes": 2_500,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
    }
    with pytest.raises(PermissionError, match="source_not_stopped"):
        permits.replace_confirmed(
            old.token, **candidate,
            capacity=CapacityEvidence(free_bytes=3_000, remaining_by_hash={old.infohash: 1_500}),
        )
    evidence = CapacityEvidence(
        free_bytes=2_000, remaining_by_hash={old.infohash: 1_500},
        paused_hashes=frozenset({old.infohash}),
    )
    with pytest.raises(PermissionError, match="waiting_space"):
        permits.replace_confirmed(old.token, **candidate, capacity=evidence)
    with pytest.raises(ValueError, match="same_infohash"):
        permits.replace_confirmed(
            old.token, **{**candidate, "infohash": old.infohash},
            capacity=CapacityEvidence(
                free_bytes=3_000, remaining_by_hash={old.infohash: 1_500},
                paused_hashes=frozenset({old.infohash}),
            ),
        )
    assert permits.get(old.token).state == "confirmed"
    assert permits.get_for_reservation(reservation.reservation_id) == permits.get(old.token)


def test_existing_database_unique_indexes_upgrade_before_source_replacement(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="seerr:legacy", source_id="legacy",
        media_key="movie:tmdb:11", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    old = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reservation.reservation_id, selected_files=("old.mkv",),
        budget_bytes=2_000, expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_gateway_permit_reservation")
        connection.execute("DROP INDEX idx_gateway_permit_scope")
        connection.execute(
            "CREATE UNIQUE INDEX idx_gateway_permit_reservation "
            "ON gateway_permits(reservation_id) "
            "WHERE reservation_id IS NOT NULL AND scope_key IS NULL"
        )
        connection.execute(
            "CREATE UNIQUE INDEX idx_gateway_permit_scope "
            "ON gateway_permits(reservation_id, scope_key) "
            "WHERE reservation_id IS NOT NULL AND scope_key IS NOT NULL"
        )
    upgraded = PermitRegistry(database)
    with sqlite3.connect(database) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_gateway_permit_reservation'"
        ).fetchone()[0]
    assert "AND state IN" in sql
    replacement = upgraded.replace_confirmed(
        old.token, infohash="c" * 40, metadata_sha256="d" * 64,
        selected_files=("new.mkv",), budget_bytes=2_500,
        capacity=CapacityEvidence(
            free_bytes=3_000, remaining_by_hash={old.infohash: 1_500},
            paused_hashes=frozenset({old.infohash}),
        ), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    assert replacement.infohash == "c" * 40
    assert upgraded.get(old.token).state == "superseded"


def test_expired_authorized_replacement_renews_only_with_fresh_exact_capacity(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="seerr:renew", source_id="renew",
        media_key="movie:tmdb:13", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    old = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reservation.reservation_id, selected_files=("old.mkv",),
        budget_bytes=2_000, expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    new = permits.replace_confirmed(
        old.token, infohash="c" * 40, metadata_sha256="d" * 64,
        selected_files=("new.mkv",), budget_bytes=2_500,
        capacity=CapacityEvidence(
            free_bytes=3_000, remaining_by_hash={old.infohash: 1_500},
            paused_hashes=frozenset({old.infohash}),
        ), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE gateway_permits SET expires_at = ? WHERE token = ?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), new.token),
        )
    not_enough = CapacityEvidence(
        free_bytes=2_000, remaining_by_hash={old.infohash: 1_500},
        paused_hashes=frozenset({old.infohash}),
    )
    with pytest.raises(PermissionError, match="waiting_space"):
        permits.renew_replacement_authorized(
            new.token, capacity=not_enough,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
    renewed = permits.renew_replacement_authorized(
        new.token, capacity=CapacityEvidence(
            free_bytes=3_000, remaining_by_hash={old.infohash: 1_500},
            paused_hashes=frozenset({old.infohash}),
        ), expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    assert renewed.permit_id == new.permit_id
    assert renewed.expires_at > datetime.now(UTC)
    assert permits.retire_expired_authorized(reservation.reservation_id) == 0
    assert repository.active_reservation(reservation.reservation_id)["budget_bytes"] == 2_500


def test_retired_replacement_cannot_be_reissued_as_an_initial_source(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="seerr:retired", source_id="retired",
        media_key="movie:tmdb:14", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    old = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reservation.reservation_id, selected_files=("old.mkv",),
        budget_bytes=2_000, expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    new = permits.replace_confirmed(
        old.token, infohash="c" * 40, metadata_sha256="d" * 64,
        selected_files=("new.mkv",), budget_bytes=2_500,
        capacity=CapacityEvidence(
            free_bytes=3_000, remaining_by_hash={old.infohash: 1_500},
            paused_hashes=frozenset({old.infohash}),
        ), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE gateway_permits SET expires_at = ? WHERE token = ?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), new.token),
        )
    assert permits.retire_expired_authorized(reservation.reservation_id) == 1
    with pytest.raises(PermissionError, match="replacement_limit"):
        permits.issue(
            infohash="e" * 40, metadata_sha256="f" * 64,
            destination="/data/torrents", category="radarr",
            reservation_id=reservation.reservation_id, selected_files=("third.mkv",),
            budget_bytes=2_500, expires_at=datetime.now(UTC) + timedelta(minutes=5),
            capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
        )


def test_list_uncertain_includes_active_episode_scopes_only_and_caps_limit(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    active = repository.reserve(
        request_id="seerr:active-season", source_id="active-season",
        media_key="season:tmdb:17:1", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    inactive = repository.reserve(
        request_id="seerr:inactive-film", source_id="inactive-film",
        media_key="movie:tmdb:18", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    first = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="sonarr",
        reservation_id=active.reservation_id, scope_key="S01E01",
        selected_files=("episode1.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    second = permits.issue(
        infohash="c" * 40, metadata_sha256="d" * 64,
        destination="/data/torrents", category="sonarr",
        reservation_id=active.reservation_id, scope_key="S01E02",
        selected_files=("episode2.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    ignored = permits.issue(
        infohash="e" * 40, metadata_sha256="f" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=inactive.reservation_id,
        selected_files=("film.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE gateway_permits SET state = 'unknown' WHERE token IN (?, ?)",
            (first.token, ignored.token),
        )
        connection.execute(
            "UPDATE gateway_permits SET state = 'dispatching' WHERE token = ?",
            (second.token,),
        )
        connection.execute(
            "UPDATE reservations SET state = 'cancelled' WHERE id = ?",
            (inactive.reservation_id,),
        )
    assert {item.token for item in permits.list_uncertain()} == {first.token, second.token}
    ordered = sorted((first, second), key=lambda item: item.permit_id)
    assert [item.permit_id for item in permits.list_uncertain(limit=1)] == [
        ordered[0].permit_id
    ]
    assert [
        item.permit_id for item in permits.list_uncertain(
            limit=1, after_id=ordered[0].permit_id
        )
    ] == [ordered[1].permit_id]
    assert permits.list_uncertain(limit=1, after_id=ordered[1].permit_id) == []
    assert len(permits.list_uncertain(limit=10_000)) == 2
    with pytest.raises(ValueError):
        permits.list_uncertain(limit=0)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE gateway_permits SET state = 'confirmed', result_json = ? "
            "WHERE permit_id = ?",
            (json.dumps({"accepted": True, "infohash": "f" * 40}), first.permit_id),
        )
    with pytest.raises(PermissionError, match="confirmed_result_mismatch"):
        permits.confirm_reconciled_source(first.token)
