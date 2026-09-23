from datetime import UTC, datetime, timedelta

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.cancellation import CancellationReconciler


def _reserved(tmp_path):
    path = tmp_path / "control.sqlite"
    repo = ReservationRepository(path)
    repo.initialize()
    permits = PermitRegistry(path)
    result = repo.reserve(
        request_id="seerr:7", source_id="7", media_key="movie:tmdb:77",
        filesystem_id="fixture", budget_bytes=50_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert result.reservation_id
    return repo, permits, result.reservation_id


def test_removed_request_revokes_unused_permit_and_reservation(tmp_path):
    repo, permits, reservation_id = _reserved(tmp_path)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
        category="radarr", reservation_id=reservation_id, budget_bytes=50_000_000_000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    assert repo.cancel_unstarted(reservation_id) == "cancelled"
    assert permits.get(permit.token).state == "revoked"
    assert repo.active_reservation(reservation_id) is None
    assert repo.cancel_unstarted(reservation_id) == "already_cancelled"


def test_removed_request_with_confirmed_download_stays_reserved(tmp_path):
    repo, permits, reservation_id = _reserved(tmp_path)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
        category="radarr", reservation_id=reservation_id, budget_bytes=50_000_000_000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _permit: {"accepted": True},
    )

    assert repo.cancel_unstarted(reservation_id) == "download_started"
    assert repo.active_reservation(reservation_id) is not None
    assert permits.get(permit.token).state == "confirmed"


def test_season_cancellation_checks_every_episode_permit(tmp_path):
    path = tmp_path / "control.sqlite"
    repo = ReservationRepository(path)
    repo.initialize()
    permits = PermitRegistry(path)
    result = repo.reserve(
        request_id="seerr:3:4", source_id="3:4",
        media_key="season:tmdb:97546:4", filesystem_id="fixture",
        budget_bytes=1000, free_bytes=500_000_000_000,
        total_bytes=600_000_000_000,
    )
    assert result.reservation_id
    common = {
        "reservation_id": result.reservation_id,
        "destination": "/data/torrents", "category": "sonarr",
        "metadata_sha256": "b" * 64, "budget_bytes": 400,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    first = permits.issue(**common, scope_key="S04E01", infohash="a" * 40)
    second = permits.issue(**common, scope_key="S04E02", infohash="c" * 40)
    permits.authorize(
        token=second.token, infohash=second.infohash,
        destination=second.destination, metadata_sha256=second.metadata_sha256,
        effect=lambda _permit: {"accepted": True},
    )
    assert repo.cancel_unstarted(result.reservation_id) == "download_started"
    assert repo.active_reservation(result.reservation_id) is not None
    assert permits.get(first.token).state == "authorized"
    assert permits.get(second.token).state == "confirmed"


@pytest.mark.asyncio
async def test_reconciler_checks_missing_approval_before_cancelling(tmp_path):
    repo, _permits, reservation_id = _reserved(tmp_path)

    class Source:
        def __init__(self):
            self.calls = []

        async def get_request_status(self, source_id):
            self.calls.append(source_id)
            return None

    source = Source()
    reconciler = CancellationReconciler(source=source, repository=repo)
    assert await reconciler.reconcile({"7"}) == 0
    assert source.calls == []
    assert await reconciler.reconcile(set()) == 1
    assert source.calls == ["7"]
    assert repo.active_reservation(reservation_id) is None
