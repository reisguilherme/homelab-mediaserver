import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Lock
from time import sleep

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import CapacityEvidence


def test_concurrent_authorization_runs_external_effect_once() -> None:
    registry = PermitRegistry()
    permit = registry.issue(
        infohash="a" * 40,
        destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    effect_started = Event()
    release_effect = Event()
    calls: list[str] = []

    def effect(_permit: object) -> dict[str, bool]:
        calls.append("called")
        effect_started.set()
        release_effect.wait(timeout=2)
        return {"accepted": True}

    def authorize() -> dict[str, bool]:
        return registry.authorize(
            token=permit.token,
            infohash=permit.infohash,
            destination=permit.destination,
            effect=effect,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(authorize)
        assert effect_started.wait(timeout=2)
        second = pool.submit(authorize)
        sleep(0.05)
        release_effect.set()
        assert first.result(timeout=2) == {"accepted": True}
        assert second.result(timeout=2) == {"accepted": True}

    assert calls == ["called"]


def test_sqlite_registry_preserves_confirmed_result_across_instances() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "control.sqlite"
        first = PermitRegistry(database)
        permit = first.issue(
            infohash="b" * 40,
            destination="/data/torrents",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        calls: list[str] = []
        result = first.authorize(
            token=permit.token,
            infohash=permit.infohash,
            destination=permit.destination,
            effect=lambda _permit: calls.append("first") or {"accepted": True},
        )

        second = PermitRegistry(database)
        repeated = second.authorize(
            token=permit.token,
            infohash=permit.infohash,
            destination=permit.destination,
            effect=lambda _permit: calls.append("second") or {"accepted": False},
        )

        assert result == repeated == {"accepted": True}
        assert calls == ["first"]


def test_late_dispatch_error_cannot_demote_reconciled_replacement() -> None:
    registry = PermitRegistry()
    old = registry.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id="movie-reservation", selected_files=("old.mkv",),
        budget_bytes=1_000,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    registry.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True, "infohash": old.infohash},
    )
    new = registry.replace_confirmed(
        old.token, infohash="c" * 40, metadata_sha256="d" * 64,
        selected_files=("new.mkv",), budget_bytes=1_000,
        capacity=CapacityEvidence(
            free_bytes=2_000, remaining_by_hash={old.infohash: 800},
            paused_hashes=frozenset({old.infohash}),
        ), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    def late_failure(_permit):
        registry.confirm_replacement(new.token)
        raise RuntimeError("late upstream error")

    with pytest.raises(RuntimeError, match="late upstream error"):
        registry.authorize(
            token=new.token, infohash=new.infohash, destination=new.destination,
            metadata_sha256=new.metadata_sha256, effect=late_failure,
        )
    assert registry.get(new.token).state == "confirmed"


def test_legacy_permits_migrate_and_keep_unknown_seed_count(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE gateway_permits (
            permit_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE,
            operation_id TEXT NOT NULL UNIQUE, reservation_id TEXT, scope_key TEXT,
            infohash TEXT NOT NULL, metadata_sha256 TEXT, destination TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '', selected_files_json TEXT NOT NULL,
            budget_bytes INTEGER, expires_at TEXT NOT NULL, state TEXT NOT NULL,
            result_json TEXT)""")
        connection.execute("""INSERT INTO gateway_permits VALUES
            ('legacy-id', 'legacy-token', 'legacy-operation', NULL, NULL,
             ?, NULL, '/data/torrents', 'radarr', '[]', NULL, ?, 'confirmed', '{}')""",
            ("a" * 40, (datetime.now(UTC) + timedelta(hours=1)).isoformat()))

    registry = PermitRegistry(database)
    assert registry.get("legacy-token").reported_seeders is None
    fresh = registry.issue(
        infohash="b" * 40, destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(hours=1), reported_seeders=42,
    )
    assert PermitRegistry(database).get(fresh.token).reported_seeders == 42


def test_two_registry_starts_migrate_legacy_seed_column_once(tmp_path, monkeypatch) -> None:
    database = tmp_path / "control.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TABLE gateway_permits (
            permit_id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE,
            operation_id TEXT NOT NULL UNIQUE, reservation_id TEXT, scope_key TEXT,
            infohash TEXT NOT NULL, metadata_sha256 TEXT, destination TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '', selected_files_json TEXT NOT NULL,
            budget_bytes INTEGER, expires_at TEXT NOT NULL, state TEXT NOT NULL,
            result_json TEXT)""")

    real_connect = sqlite3.connect
    alter_lock = Lock()
    second_alter_finished = Event()
    start = Barrier(3)
    alter_count = 0

    class RacingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal alter_count
            if "ALTER TABLE gateway_permits ADD COLUMN reported_seeders" not in sql:
                return super().execute(sql, parameters)
            with alter_lock:
                alter_count += 1
                order = alter_count
            if order == 1:
                # Give a second process time to observe the old schema. With a
                # migration lock it must instead wait until this one commits.
                second_alter_finished.wait(timeout=1)
            result = super().execute(sql, parameters)
            if order == 2:
                second_alter_finished.set()
            return result

    def racing_connect(*args, **kwargs):
        return real_connect(*args, factory=RacingConnection, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", racing_connect)

    def initialize() -> PermitRegistry:
        start.wait(timeout=3)
        return PermitRegistry(database)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(initialize)
        second = pool.submit(initialize)
        start.wait(timeout=3)
        assert first.result(timeout=5).issue(
            infohash="a" * 40, destination="/data/torrents",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            reported_seeders=3,
        ).reported_seeders == 3
        assert second.result(timeout=5).get_for_reservation("missing") is None

    with real_connect(database) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(gateway_permits)")]
    assert columns.count("reported_seeders") == 1
    assert alter_count == 1


@pytest.mark.parametrize("invalid", [True, -1, 1.5, "10"])
def test_issue_rejects_invalid_reported_seeders(invalid) -> None:
    with pytest.raises(ValueError, match="seed"):
        PermitRegistry().issue(
            infohash="a" * 40, destination="/data/torrents",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            reported_seeders=invalid,
        )


@pytest.mark.parametrize("invalid", [False, -1, 1.5, "10"])
def test_replacement_rejects_invalid_reported_seeders(invalid) -> None:
    registry = PermitRegistry()
    old = registry.issue(
        infohash="a" * 40, destination="/data/torrents", category="radarr",
        reservation_id="movie-reservation", metadata_sha256="b" * 64,
        selected_files=("old.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    registry.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    with pytest.raises(ValueError, match="seed"):
        registry.replace_confirmed(
            old.token, infohash="c" * 40, metadata_sha256="d" * 64,
            selected_files=("new.mkv",), budget_bytes=100,
            capacity=CapacityEvidence(
                free_bytes=1_000, remaining_by_hash={old.infohash: 100},
                paused_hashes=frozenset({old.infohash}),
            ), expires_at=datetime.now(UTC) + timedelta(hours=1),
            reported_seeders=invalid,
        )


def test_replacement_persists_reported_seeders(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(
        request_id="seerr:movie", source_id="movie", media_key="movie:tmdb:123",
        filesystem_id="fixture", budget_bytes=1_000, free_bytes=10_000,
        total_bytes=20_000,
    )
    registry = PermitRegistry(database)
    old = registry.issue(
        infohash="a" * 40, destination="/data/torrents", category="radarr",
        reservation_id=reservation.reservation_id, metadata_sha256="b" * 64,
        selected_files=("old.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(hours=1), reported_seeders=1,
    )
    registry.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    new = registry.replace_confirmed(
        old.token, infohash="c" * 40, metadata_sha256="d" * 64,
        selected_files=("new.mkv",), budget_bytes=100,
        capacity=CapacityEvidence(
            free_bytes=1_000, remaining_by_hash={old.infohash: 100},
            paused_hashes=frozenset({old.infohash}),
        ), expires_at=datetime.now(UTC) + timedelta(hours=1),
        reported_seeders=50,
    )
    assert PermitRegistry(database).get(old.token).reported_seeders == 1
    assert PermitRegistry(database).get(new.token).reported_seeders == 50


def test_active_confirmed_movie_list_excludes_series_and_inactive_reservations(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    registry = PermitRegistry(database)
    permits = []
    for name, media_key, category, scope, hash_digit in (
        ("active", "movie:tmdb:123", "radarr", None, "a"),
        ("series", "season:tmdb:123:1", "sonarr", "S01E01", "b"),
        ("inactive", "movie:tmdb:456", "radarr", None, "c"),
        ("inactive-series", "season:tmdb:456:1", "sonarr", "S01E01", "d"),
        ("series-next-season", "season:tmdb:123:2", "sonarr", "S02E01", "e"),
    ):
        reservation = repo.reserve(
            request_id=f"seerr:{name}", source_id=name, media_key=media_key,
            filesystem_id="fixture", budget_bytes=1_000, free_bytes=10_000,
            total_bytes=20_000,
        )
        permit = registry.issue(
            infohash=hash_digit * 40, destination="/data/torrents",
            category=category, reservation_id=reservation.reservation_id,
            scope_key=scope, metadata_sha256="a" * 64,
            selected_files=(f"{name}.mkv",), budget_bytes=100,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            reported_seeders=5,
        )
        registry.authorize(
            token=permit.token, infohash=permit.infohash,
            destination=permit.destination, metadata_sha256=permit.metadata_sha256,
            effect=lambda _: {"accepted": True},
        )
        permits.append((permit, reservation.reservation_id))
    completed = registry.issue(
        infohash="f" * 40, destination="/data/torrents", category="sonarr",
        reservation_id=permits[1][1], scope_key="S01E02",
        metadata_sha256="a" * 64, selected_files=("complete.mkv",),
        budget_bytes=100, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    registry.authorize(
        token=completed.token, infohash=completed.infohash,
        destination=completed.destination, metadata_sha256=completed.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE reservations SET state = 'complete' WHERE id = ?",
                           (permits[2][1],))
        connection.execute("UPDATE reservations SET state = 'complete' WHERE id = ?",
                           (permits[3][1],))
        connection.execute(
            "INSERT INTO episode_imports(permit_id, state, updated_at) VALUES (?, ?, ?)",
            (completed.permit_id, "complete", datetime.now(UTC).isoformat()),
        )

    listed = PermitRegistry(database).list_active_confirmed_movies()
    assert [permit.permit_id for permit in listed] == [permits[0][0].permit_id]
    assert listed[0].reported_seeders == 5
    episodes = PermitRegistry(database).list_active_confirmed_episodes()
    assert {(key, permit.permit_id) for key, permit in episodes} == {
        ("season:tmdb:123", permits[1][0].permit_id),
        ("season:tmdb:123", permits[4][0].permit_id),
    }


def test_in_memory_active_confirmed_episode_list_excludes_movie_and_unconfirmed() -> None:
    registry = PermitRegistry()
    episode = registry.issue(
        infohash="a" * 40, destination="/data/torrents", category="sonarr",
        reservation_id="season-reservation", scope_key="S01E01",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    movie = registry.issue(
        infohash="b" * 40, destination="/data/torrents", category="radarr",
        reservation_id="movie-reservation",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    registry.issue(
        infohash="c" * 40, destination="/data/torrents", category="sonarr",
        reservation_id="season-reservation", scope_key="S01E02",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    for permit in (episode, movie):
        registry.authorize(
            token=permit.token, infohash=permit.infohash,
            destination=permit.destination,
            effect=lambda _: {"accepted": True},
        )
    assert [
        (series_key, permit.permit_id)
        for series_key, permit in registry.list_active_confirmed_episodes()
    ] == [
        ("season-reservation", episode.permit_id)
    ]
