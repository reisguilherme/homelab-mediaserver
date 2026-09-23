from dataclasses import dataclass

import pytest

from homeserver_control.persistence.db import ReservationResult
from homeserver_control.worker.runtime import WorkerCycle


@dataclass
class FakeSource:
    pages: list[list[dict[str, object]]]

    async def list_approved(self, page: int) -> list[dict[str, object]]:
        return self.pages[page - 1] if page <= len(self.pages) else []


class FakeScheduler:
    def __init__(self, results: list[ReservationResult]) -> None:
        self.results = iter(results)
        self.candidates = []

    def admit(self, candidate):
        self.candidates.append(candidate)
        return next(self.results)


class FakeAcquirer:
    def __init__(self) -> None:
        self.calls = []

    async def acquire(self, media_key: str, reservation_id: str) -> str:
        self.calls.append((media_key, reservation_id))
        return "grabbed"


@pytest.mark.asyncio
async def test_worker_reserves_approved_requests_without_dispatching_downloads() -> None:
    source = FakeSource(
        pages=[
            [
                {"source_id": "42", "media_key": "movie:tmdb:10", "kind": "movie"},
                {"source_id": "43", "media_key": "season:tvdb:11:1", "kind": "season"},
            ]
        ]
    )
    scheduler = FakeScheduler(
        [
            ReservationResult(True, reservation_id="r1"),
            ReservationResult(False, reason="waiting_space"),
        ]
    )
    cycle = WorkerCycle(source=source, scheduler=scheduler, page_size=10)

    report = await cycle.run_once()

    assert report.processed == 2
    assert report.accepted == 1
    assert report.deferred == 1
    assert [candidate.request_id for candidate in scheduler.candidates] == [
        "seerr:42",
        "seerr:43",
    ]
    assert scheduler.candidates[0].budget_bytes == 50_000_000_000
    assert scheduler.candidates[1].budget_bytes == 100_000_000_000


@pytest.mark.asyncio
async def test_worker_skips_malformed_request_and_continues() -> None:
    source = FakeSource(
        pages=[
            [
                {"source_id": "missing-media-key", "kind": "movie"},
                {"source_id": "44", "media_key": "episode:tvdb:12:1", "kind": "episode"},
            ]
        ]
    )
    scheduler = FakeScheduler([ReservationResult(True, reservation_id="r1")])
    cycle = WorkerCycle(source=source, scheduler=scheduler, page_size=10)

    report = await cycle.run_once()

    assert report.processed == 1
    assert report.accepted == 1
    assert report.malformed == 1
    assert scheduler.candidates[0].budget_bytes == 5_000_000_000


@pytest.mark.asyncio
async def test_worker_acquires_only_admitted_movies() -> None:
    source = FakeSource(pages=[[
        {"source_id": "42", "media_key": "movie:tmdb:10", "kind": "movie"},
        {"source_id": "43", "media_key": "movie:tmdb:11", "kind": "movie"},
        {"source_id": "44", "media_key": "season:tvdb:12", "kind": "season"},
    ]])
    scheduler = FakeScheduler([
        ReservationResult(True, reservation_id="r1"),
        ReservationResult(False, reason="waiting_space"),
        ReservationResult(True, reservation_id="r3"),
    ])
    acquirer = FakeAcquirer()
    cycle = WorkerCycle(source=source, scheduler=scheduler, acquirer=acquirer)

    report = await cycle.run_once()

    assert acquirer.calls == [("movie:tmdb:10", "r1")]
    assert report.grabbed == 1
