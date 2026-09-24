from __future__ import annotations

import sqlite3

from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import (
    MOVIE_FINALIZER_SOURCE,
    SubtitleArtifactStore,
)


def test_subtitle_artifact_survives_restart_and_rejects_corruption(tmp_path):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(
        request_id="seerr:3:4", source_id="3:4",
        media_key="season:tmdb:97546:4", filesystem_id="fixture",
        budget_bytes=100_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert reservation.reservation_id
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nOla!\n"
    store = SubtitleArtifactStore(database)
    store.put(reservation.reservation_id, "S04E01", "a" * 40, srt)
    assert SubtitleArtifactStore(database).get(
        reservation.reservation_id, "S04E01", "a" * 40
    ) == srt
    assert store.get(reservation.reservation_id, "S04E02", "a" * 40) is None
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE subtitle_artifacts SET content = ? WHERE reservation_id = ?",
            (b"corrupt", reservation.reservation_id),
        )
    assert SubtitleArtifactStore(database).get(
        reservation.reservation_id, "S04E01", "a" * 40
    ) is None


def test_english_subtitle_can_be_replaced_by_preferred_brazilian_portuguese(tmp_path):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(
        request_id="seerr:3:4", source_id="3:4",
        media_key="season:tmdb:97546:4", filesystem_id="fixture",
        budget_bytes=100_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    english = b"1\n00:00:01,000 --> 00:00:02,000\nHello!\n"
    brazilian = b"1\n00:00:01,000 --> 00:00:02,000\nOla!\n"
    store = SubtitleArtifactStore(database)
    store.put(reservation.reservation_id, "S04E01", "a" * 40, english, language="EN")
    assert store.get(reservation.reservation_id, "S04E01", "a" * 40, language="EN") == english
    assert store.get(reservation.reservation_id, "S04E01", "a" * 40) is None

    store.put(reservation.reservation_id, "S04E01", "a" * 40, brazilian)
    assert store.get(reservation.reservation_id, "S04E01", "a" * 40) == brazilian
    assert store.get(reservation.reservation_id, "S04E01", "a" * 40, language="EN") is None
    store.put(reservation.reservation_id, "S04E01", "a" * 40, english, language="EN")
    assert store.get(reservation.reservation_id, "S04E01", "a" * 40) == brazilian


def test_movie_finalizer_replaces_legacy_preflight_artifact(tmp_path):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reservation = repo.reserve(
        request_id="seerr:movie:1", source_id="movie:1",
        media_key="movie:tmdb:152532", filesystem_id="fixture",
        budget_bytes=100, free_bytes=1000, total_bytes=2000,
    )
    assert reservation.reservation_id
    legacy = b"1\n00:00:01,000 --> 00:00:02,000\nOld cut!\n"
    selected = b"1\n00:00:01,000 --> 00:00:02,000\nNew cut!\n"
    infohash = "a" * 40
    store = SubtitleArtifactStore(database)
    store.put(reservation.reservation_id, None, infohash, legacy)
    assert store.get(
        reservation.reservation_id, None, infohash,
        source=MOVIE_FINALIZER_SOURCE,
    ) is None

    store.put(
        reservation.reservation_id, None, infohash, selected,
        source=MOVIE_FINALIZER_SOURCE,
    )
    assert store.get(
        reservation.reservation_id, None, infohash,
        source=MOVIE_FINALIZER_SOURCE,
    ) == selected
    assert store.get(reservation.reservation_id, None, infohash) is None
