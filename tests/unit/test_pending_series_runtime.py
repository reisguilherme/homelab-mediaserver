"""Published TV recovery follows new work and refreshes absent approvals from Seerr."""

from types import SimpleNamespace

import pytest

from homeserver_control.persistence.db import ReservationResult
from homeserver_control.worker.runtime import WorkerCycle
from homeserver_control.worker.series_identity import SeriesIdentityResolver

PENDING = {
    "source_id": "127:1", "media_key": "season:tmdb:74606:1", "reservation_id": "pending",
}
PROOF = {
    "source_id": "127:1", "media_key": "season:tmdb:74606:1", "kind": "season",
    "tvdb_id": 293012, "external_service_id": 124, "title": "Irmão do Jorel",
}
NATIVE = {"id": 124, "tmdbId": 0, "tvdbId": 293012}


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [PROOF, None, PROOF | {"media_key": "season:tmdb:999:1"}])
async def test_pending_series_outside_approved_refreshes_identity_only_from_matching_current_proof(
    fresh,
):
    events = []
    resolver = SeriesIdentityResolver()

    class Source:
        async def list_approved(self, page):
            return ([{"source_id": "128", "media_key": "movie:tmdb:999", "kind": "movie"}]
                    if page == 1 else [])

        async def get_series_request(self, source_id):
            assert source_id == "127:1"
            events.append("fresh-proof")
            return fresh

    class Profiles:
        async def reconcile(self, requests):
            resolver.update_requests(requests)
            return set()

    class Movie:
        async def finalize(self, *_args):
            events.append("new-movie")
            return "downloading"

    class Series:
        async def finalize(self, *_args):
            events.append("confirm-series")
            return "subtitles_pending"

        async def retry_pending_subtitles(self, *_args):
            events.append("retry-series")
            assert resolver.resolve([NATIVE], 74606) is not None
            assert resolver.subtitle_titles([NATIVE], 74606) == ("Irmão do Jorel",)
            return "waiting_subtitles"

    cycle = WorkerCycle(
        source=Source(), scheduler=SimpleNamespace(admit=lambda _: ReservationResult(
            True, reservation_id="new-movie",
        )), request_profiles=Profiles(), finalizer=Movie(), series_finalizer=Series(),
        series_identity=resolver, pending_series_imports=lambda: [PENDING],
    )
    await cycle.run_once()
    if fresh == PROOF:
        assert events == ["new-movie", "fresh-proof", "confirm-series", "retry-series"]
    else:
        assert events == ["new-movie", "fresh-proof"]
        assert resolver.resolve([NATIVE], 74606) is None


@pytest.mark.asyncio
async def test_admitted_series_is_confirmed_once_then_retried_after_all_new_imports():
    events = []

    class Source:
        async def list_approved(self, page):
            return [PROOF] if page == 1 else []

        async def get_series_request(self, _source_id):
            raise AssertionError("current authenticated snapshot already contains the request")

    class Series:
        async def finalize(self, *_args):
            events.append("new-series")
            return "subtitles_pending"

        async def retry_pending_subtitles(self, *_args):
            events.append("retry-series")
            return "waiting_subtitles"

    cycle = WorkerCycle(
        source=Source(), scheduler=SimpleNamespace(admit=lambda _: ReservationResult(
            True, reservation_id="pending",
        )), series_finalizer=Series(), pending_series_imports=lambda: [PENDING],
    )
    await cycle.run_once()
    assert events == ["new-series", "retry-series"]
