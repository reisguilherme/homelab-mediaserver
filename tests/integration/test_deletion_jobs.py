import sqlite3

from homeserver_control.persistence.db import ReservationRepository


def test_duplicate_delete_preserves_original_capture_across_restart(tmp_path) -> None:
    from homeserver_control.persistence.deletion_jobs import DeletionJobStore

    database = tmp_path / "control.sqlite3"
    store = DeletionJobStore(database)
    store.initialize()
    capture = {"media_key": "movie:tmdb:13", "path": "/srv/data/media/old.mkv"}

    first = store.enqueue("jellyfin-item", "Movie", capture)
    capture["path"] = "/srv/data/media/replaced.mkv"
    duplicate = store.enqueue(
        "jellyfin-item", "Episode", {"media_key": "episode:tmdb:9:S01E01"}
    )
    reopened = DeletionJobStore(database)

    assert first["stage"] == "queued"
    assert duplicate == first
    assert reopened.get("jellyfin-item") == first
    assert first["payload"] == {
        "media_key": "movie:tmdb:13",
        "path": "/srv/data/media/old.mkv",
    }
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM deletion_jobs").fetchone()[0] == 1


def test_restart_returns_oldest_active_job_with_persisted_stage(tmp_path) -> None:
    from homeserver_control.persistence.deletion_jobs import DeletionJobStore

    database = tmp_path / "control.sqlite3"
    store = DeletionJobStore(database)
    store.initialize()
    store.enqueue("first", "Movie", {"media_key": "movie:tmdb:1"})
    store.enqueue("second", "Movie", {"media_key": "movie:tmdb:2"})
    store.set_stage("first", "arr_removed")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE deletion_jobs SET updated_at = ? WHERE item_id = ?",
            ("2026-01-01T00:00:00Z", "first"),
        )
    assert store.set_stage("first", "arr_removed")["updated_at"] == "2026-01-01T00:00:00Z"

    reopened = DeletionJobStore(database)
    assert reopened.next_queued()["item_id"] == "first"
    assert reopened.get("first")["stage"] == "arr_removed"
    assert [job["item_id"] for job in reopened.list()] == ["first", "second"]

    reopened.set_stage("first", "blocked", error="path changed")
    assert reopened.get("first")["error"] == "path changed"
    assert reopened.next_queued()["item_id"] == "second"
    reopened.set_stage("second", "complete")
    assert reopened.next_queued() is None


def test_tombstone_is_idempotent_and_blocks_future_reservation(tmp_path) -> None:
    from homeserver_control.persistence.deletion_jobs import DeletionJobStore

    database = tmp_path / "control.sqlite3"
    store = DeletionJobStore(database)
    store.initialize()
    store.tombstone("movie:tmdb:13", "generation-1")
    store.tombstone("movie:tmdb:13", "generation-2")

    assert DeletionJobStore(database).is_tombstoned("movie:tmdb:13")
    assert not store.is_tombstoned("movie:tmdb:14")
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT source_generation FROM tombstones WHERE media_key = ?",
            ("movie:tmdb:13",),
        ).fetchall()
    assert rows == [("generation-1",)]

    reservation = ReservationRepository(database).reserve(
        request_id="request-1",
        source_id="source-1",
        media_key="movie:tmdb:13",
        filesystem_id="fs-test",
        budget_bytes=1,
        free_bytes=100_000_000_000,
        total_bytes=1_000_000_000_000,
    )
    assert not reservation.accepted
    assert reservation.reason == "media_deleted"
