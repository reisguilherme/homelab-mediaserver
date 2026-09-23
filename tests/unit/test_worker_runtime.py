from dataclasses import dataclass

import pytest

from homeserver_control.persistence.db import ReservationResult
from homeserver_control.worker.__main__ import _build_cycle
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


class FakeFinalizer:
    def __init__(self) -> None:
        self.calls = []

    async def finalize(self, media_key: str, reservation_id: str) -> str:
        self.calls.append((media_key, reservation_id))
        return "downloading"


class FakeCancellation:
    def __init__(self) -> None:
        self.approved = None

    async def reconcile(self, approved_source_ids: set[str]) -> int:
        self.approved = approved_source_ids
        return 0


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
    assert scheduler.candidates[0].budget_bytes == 0
    assert scheduler.candidates[1].budget_bytes == 0


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
    assert scheduler.candidates[0].budget_bytes == 0


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
    finalizer = FakeFinalizer()
    cycle = WorkerCycle(
        source=source, scheduler=scheduler, acquirer=acquirer, finalizer=finalizer
    )

    report = await cycle.run_once()

    assert acquirer.calls == [("movie:tmdb:10", "r1")]
    assert finalizer.calls == [("movie:tmdb:10", "r1")]
    assert report.grabbed == 1


@pytest.mark.asyncio
async def test_worker_dispatches_only_reserved_seasons_to_series_pipeline() -> None:
    source = FakeSource(pages=[[
        {"source_id": "3:4", "media_key": "season:tmdb:97546:4", "kind": "season"},
        {"source_id": "4:2", "media_key": "season:tmdb:111:2", "kind": "season"},
    ]])
    scheduler = FakeScheduler([
        ReservationResult(True, reservation_id="r1"),
        ReservationResult(False, reason="waiting_space"),
    ])
    acquirer = FakeAcquirer()
    finalizer = FakeFinalizer()
    cycle = WorkerCycle(
        source=source, scheduler=scheduler,
        series_acquirer=acquirer, series_finalizer=finalizer,
    )
    report = await cycle.run_once()
    assert acquirer.calls == [("season:tmdb:97546:4", "r1")]
    assert finalizer.calls == [("season:tmdb:97546:4", "r1")]
    assert report.grabbed == 1


@pytest.mark.asyncio
async def test_worker_continues_pages_after_seerr_expands_one_request_to_seasons() -> None:
    source = FakeSource(pages=[
        [{"source_id": "3:4", "media_key": "season:tmdb:97546:4", "kind": "season"}],
        [{"source_id": "4", "media_key": "movie:tmdb:123", "kind": "movie"}],
    ])
    scheduler = FakeScheduler([
        ReservationResult(False, reason="waiting_space"),
        ReservationResult(False, reason="waiting_space"),
    ])
    report = await WorkerCycle(source=source, scheduler=scheduler).run_once()
    assert report.processed == 2
    assert len(scheduler.candidates) == 2


@pytest.mark.asyncio
async def test_worker_reconciles_withdrawn_requests_after_pagination() -> None:
    source = FakeSource(pages=[[
        {"source_id": "7", "media_key": "movie:tmdb:10", "kind": "movie"},
    ]])
    scheduler = FakeScheduler([ReservationResult(False, reason="waiting_space")])
    cancellation = FakeCancellation()
    cycle = WorkerCycle(source=source, scheduler=scheduler, cancellation=cancellation)

    await cycle.run_once()

    assert cancellation.approved == {"7"}


@pytest.mark.asyncio
async def test_worker_admits_every_page_before_slow_acquisition() -> None:
    source = FakeSource(pages=[
        [{"source_id": "1", "media_key": "movie:tmdb:10", "kind": "movie"}],
        [{"source_id": "2", "media_key": "movie:tmdb:11", "kind": "movie"}],
    ])
    scheduler = FakeScheduler([
        ReservationResult(True, reservation_id="r1"),
        ReservationResult(True, reservation_id="r2"),
    ])
    seen_admissions: list[int] = []

    class InspectingAcquirer:
        async def acquire(self, media_key: str, reservation_id: str) -> str:
            seen_admissions.append(len(scheduler.candidates))
            return "no_eligible_release"

    cycle = WorkerCycle(source=source, scheduler=scheduler,
                        acquirer=InspectingAcquirer())
    report = await cycle.run_once()
    assert report.accepted == 2
    assert [item.request_id for item in scheduler.candidates] == ["seerr:1", "seerr:2"]
    assert seen_admissions == [2, 2]


def test_worker_reads_subdl_key_from_private_file(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "subdl.key"
    key_file.write_text("test-subdl-key\n", encoding="utf-8")
    monkeypatch.setenv("HOMESERVER_SUBDL_API_KEY_FILE", str(key_file))
    monkeypatch.setenv("HOMESERVER_SEERR_URL", "http://seerr:5055")
    monkeypatch.setenv("HOMESERVER_SEERR_API_KEY", "seerr-test")
    monkeypatch.setenv("HOMESERVER_CAPACITY_SNAPSHOT", str(tmp_path / "capacity.json"))
    monkeypatch.setenv("HOMESERVER_RADARR_URL", "http://radarr:7878")
    monkeypatch.setenv("HOMESERVER_RADARR_API_KEY", "radarr-test")
    monkeypatch.setenv("HOMESERVER_SONARR_URL", "http://sonarr:8989")
    monkeypatch.setenv("HOMESERVER_SONARR_API_KEY", "sonarr-test")
    monkeypatch.setenv("HOMESERVER_ARR_TOKEN", "gateway-test")
    monkeypatch.setenv("HOMESERVER_RECOVERY_MODE", str(tmp_path / "RECOVERY_MODE"))
    cycle = _build_cycle(tmp_path / "control.sqlite")
    assert cycle is not None
    assert cycle.acquirer.subtitle_source.api_key == "test-subdl-key"
    assert cycle.series_acquirer.subtitle_source.api_key == "test-subdl-key"
    assert cycle.series_acquirer.gateway_url == "http://download-gateway:8081"
    assert cycle.series_acquirer.arr_token == "gateway-test"
