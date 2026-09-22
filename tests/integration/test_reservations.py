import shutil
import sqlite3
from multiprocessing import get_context
from pathlib import Path
from uuid import uuid4

import pytest

from homeserver_control.persistence.db import ReservationRepository


def _try_reserve(db_path: str, request_id: str, output) -> None:
    repo = ReservationRepository(db_path)
    repo.initialize()
    result = repo.reserve(
        request_id=request_id,
        source_id=f"source-{request_id}",
        media_key=f"movie:tmdb:{request_id}",
        filesystem_id="fs-test",
        budget_bytes=50_000_000_000,
        free_bytes=80_000_000_000,
        total_bytes=100_000_000_000,
    )
    output.put(result.accepted)


@pytest.fixture
def local_tmp():
    path = Path(".runtime") / f"test-reservations-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_two_processes_cannot_overcommit_one_capacity_snapshot(local_tmp) -> None:
    db_path = str(local_tmp / "control.sqlite3")
    repo = ReservationRepository(db_path)
    repo.initialize()

    ctx = get_context("spawn")
    output = ctx.Queue()
    processes = [
        ctx.Process(target=_try_reserve, args=(db_path, f"request-{index}", output))
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(output.get() for _ in processes) == [False, True]
    assert repo.count_reservations() == 1


def test_reservation_and_operation_are_idempotent(local_tmp) -> None:
    repo = ReservationRepository(str(local_tmp / "control.sqlite3"))
    repo.initialize()
    first = repo.reserve(
        request_id="request-1",
        source_id="source-1",
        media_key="movie:tmdb:1",
        filesystem_id="fs-test",
        budget_bytes=10,
        free_bytes=100_000_000_000,
        total_bytes=1_000_000_000_000,
    )
    second = repo.reserve(
        request_id="request-1",
        source_id="source-1",
        media_key="movie:tmdb:1",
        filesystem_id="fs-test",
        budget_bytes=10,
        free_bytes=100_000_000_000,
        total_bytes=1_000_000_000_000,
    )
    assert first.accepted is True
    assert second.accepted is True
    assert second.reservation_id == first.reservation_id
    assert second.operation_id == first.operation_id
    assert repo.count_reservations() == 1


def test_deleted_media_cannot_be_admitted_again(local_tmp) -> None:
    path = local_tmp / "control.sqlite3"
    repo = ReservationRepository(path)
    repo.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO tombstones(media_key, deleted_at, source_generation) VALUES (?, ?, ?)",
            ("movie:tmdb:1", "2026-09-22T00:00:00Z", "generation-1"),
        )

    result = repo.reserve(
        request_id="request-1",
        source_id="source-1",
        media_key="movie:tmdb:1",
        filesystem_id="fs-test",
        budget_bytes=10,
        free_bytes=100_000_000_000,
        total_bytes=1_000_000_000_000,
    )
    assert result.accepted is False
    assert result.reason == "media_deleted"
    assert repo.count_reservations() == 0
