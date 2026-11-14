from __future__ import annotations

import sqlite3

import pytest

from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore

SOURCE = "opensubtitles_series_finalizer"
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nOla!\n"


def _reservation(tmp_path):
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    result = repository.reserve(
        request_id="seerr:fixture:1", source_id="fixture:1",
        media_key="season:tmdb:76479:1", filesystem_id="fixture",
        budget_bytes=100, free_bytes=1000, total_bytes=2000,
    )
    assert result.reservation_id
    return database, result.reservation_id


def test_opensubtitles_series_artifact_keeps_explicit_provenance(tmp_path):
    database, reservation = _reservation(tmp_path)
    SubtitleArtifactStore(database).put(
        reservation, "S01E01", "a" * 40, SRT, source=SOURCE,
    )

    restarted = SubtitleArtifactStore(database)
    assert restarted.get(reservation, "S01E01", "a" * 40, source=SOURCE) == SRT
    assert restarted.get(reservation, "S01E01", "a" * 40) is None
    assert restarted.get(reservation, "S01E02", "a" * 40, source=SOURCE) is None
    assert restarted.get(reservation, "S01E01", "b" * 40, source=SOURCE) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT source, language, content FROM subtitle_artifacts",
        ).fetchone() == ("opensubtitles_series_finalizer", "BR_PT", SRT)


def test_opensubtitles_cannot_relabel_an_existing_subdl_artifact(tmp_path):
    database, reservation = _reservation(tmp_path)
    store = SubtitleArtifactStore(database)
    store.put(reservation, "S01E01", "a" * 40, SRT)

    with pytest.raises(ValueError, match="source conflicts"):
        store.put(reservation, "S01E01", "a" * 40, SRT, source=SOURCE)
    assert store.get(reservation, "S01E01", "a" * 40) == SRT
    assert store.get(reservation, "S01E01", "a" * 40, source=SOURCE) is None


def test_subdl_cannot_relabel_an_existing_opensubtitles_artifact(tmp_path):
    database, reservation = _reservation(tmp_path)
    store = SubtitleArtifactStore(database)
    store.put(reservation, "S01E01", "a" * 40, SRT, source=SOURCE)

    with pytest.raises(ValueError, match="source conflicts"):
        store.put(reservation, "S01E01", "a" * 40, SRT)
    assert store.get(reservation, "S01E01", "a" * 40, source=SOURCE) == SRT
    assert store.get(reservation, "S01E01", "a" * 40) is None


@pytest.mark.parametrize("scope", [None, "S01PACK", "S00E01", "S01E00"])
def test_opensubtitles_series_source_requires_an_episode_scope(tmp_path, scope):
    database, reservation = _reservation(tmp_path)
    store = SubtitleArtifactStore(database)

    with pytest.raises(ValueError, match="episode scope"):
        store.put(reservation, scope, "a" * 40, SRT, source=SOURCE)
    with pytest.raises(ValueError, match="episode scope"):
        store.get(reservation, scope, "a" * 40, source=SOURCE)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM subtitle_artifacts").fetchone() == (0,)
