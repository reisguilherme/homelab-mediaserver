import shutil
import sqlite3
from multiprocessing import get_context
from pathlib import Path
from uuid import uuid4

import pytest

from homeserver_control.persistence.db import ReservationRepository


@pytest.mark.parametrize("state,included", [
    ("accepted", True), ("accepted_copy", True), ("dispatching", False),
    ("complete", False), ("unknown", False),
    ("subtitles_pending", True), ("subtitles_pending_copy", True),
])
def test_pending_movie_imports_are_owned_accepted_active_and_not_cancelled(
    tmp_path, state, included,
):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservation = repo.reserve(
        request_id="seerr:42", source_id="42", media_key="movie:tmdb:10",
        filesystem_id="fixture", budget_bytes=0, free_bytes=100, total_bytes=200,
    )
    assert repo.claim_movie_import(reservation.reservation_id)
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE movie_imports SET state=?", (state,))
    expected = [{"reservation_id": reservation.reservation_id,
                 "media_key": "movie:tmdb:10", "source_id": "42"}] if included else []
    assert repo.pending_movie_imports() == expected
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE requests SET state='cancel_requested'")
    assert repo.pending_movie_imports() == []
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE requests SET state='reserved'")
        connection.execute("INSERT INTO tombstones VALUES ('movie:tmdb:10','now','fixture')")
    assert repo.pending_movie_imports() == []
    with sqlite3.connect(repo.path) as connection:
        connection.execute("DELETE FROM tombstones")
        connection.execute("UPDATE reservations SET state='imported'")
    assert repo.pending_movie_imports() == []


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


@pytest.mark.parametrize("copied", [False, True])
def test_verified_import_waiting_subtitle_releases_import_slot(tmp_path, copied):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservations = [repo.reserve(
        request_id=f"seerr:{n}", source_id=str(n), media_key=f"movie:tmdb:{n}",
        filesystem_id="fixture", budget_bytes=0, free_bytes=1000, total_bytes=2000,
    ).reservation_id for n in (1, 2)]
    assert repo.claim_movie_import(reservations[0], copy_allowed=copied)
    repo.record_movie_import(reservations[0], "42")
    assert not repo.claim_movie_import(reservations[1], copy_allowed=True)
    repo.verify_movie_import(reservations[0])
    assert repo.import_state(reservations[0]) == (
        "subtitles_pending_copy" if copied else "subtitles_pending"
    )
    assert repo.claim_movie_import(reservations[1], copy_allowed=True)
    # A submitted copy still blocks every other import until its bytes are verified.
    assert not repo.claim_episode_import("episode-permit")


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


def test_requests_without_releases_consume_no_disk_capacity(local_tmp) -> None:
    repo = ReservationRepository(str(local_tmp / "control.sqlite3"))
    repo.initialize()
    for number in range(10):
        result = repo.reserve(
            request_id=f"request-{number}", source_id=f"source-{number}",
            media_key=f"movie:tmdb:{number}", filesystem_id="fs-test",
            budget_bytes=0, free_bytes=1, total_bytes=100,
        )
        assert result.accepted and result.reservation_id
        assert repo.active_reservation(result.reservation_id)["budget_bytes"] == 0
    assert repo.count_reservations() == 10


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
