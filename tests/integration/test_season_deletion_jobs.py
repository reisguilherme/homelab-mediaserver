"""A season's explicit scope is persisted without admitting partial child jobs."""

from __future__ import annotations

from copy import deepcopy

import pytest

from homeserver_control.persistence.deletion_jobs import DeletionJobStore


def _season_payload():
    return {
        "media_key": "season:tmdb:123:2",
        "episodes": [
            {"item_id": "b" * 32, "payload": {
                "media_key": "episode:tmdb:123:S02E01", "parent_item_id": "a" * 32,
            }},
            {"item_id": "c" * 32, "payload": {
                "media_key": "episode:tmdb:123:S02E02", "parent_item_id": "a" * 32,
            }},
        ],
    }


def test_season_jobs_are_persisted_parent_first_and_idempotently(tmp_path):
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    first = jobs.enqueue_season("a" * 32, _season_payload())
    assert jobs.next_queued() == first
    assert [(job["item_id"], job["item_type"]) for job in jobs.list()] == [
        ("a" * 32, "Season"), ("b" * 32, "Episode"), ("c" * 32, "Episode"),
    ]
    changed = deepcopy(_season_payload())
    changed["episodes"][0]["payload"]["media_key"] = "changed"
    assert jobs.enqueue_season("a" * 32, changed) == first
    assert len(jobs.list()) == 3


@pytest.mark.parametrize("conflict", ["changed", "complete", "blocked", "wrong_type", "parent"])
def test_season_enqueue_rolls_back_every_new_job_on_existing_child_conflict(tmp_path, conflict):
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    payload = _season_payload()
    child_payload = deepcopy(payload["episodes"][1]["payload"])
    if conflict == "changed":
        child_payload["media_key"] = "different_episode"
    elif conflict == "parent":
        child_payload["parent_item_id"] = "d" * 32
    jobs.enqueue("c" * 32, "Movie" if conflict == "wrong_type" else "Episode", child_payload)
    if conflict in {"complete", "blocked"}:
        jobs.set_stage("c" * 32, conflict)
    with pytest.raises(ValueError):
        jobs.enqueue_season("a" * 32, payload)
    assert [job["item_id"] for job in jobs.list()] == ["c" * 32]


def test_season_enqueue_reuses_matching_pending_child_without_restarting_it(tmp_path):
    jobs = DeletionJobStore(tmp_path / "control.sqlite")
    jobs.initialize()
    payload = _season_payload()
    jobs.enqueue("b" * 32, "Episode", payload["episodes"][0]["payload"])
    jobs.set_stage("b" * 32, "tombstoned")
    jobs.enqueue_season("a" * 32, payload)
    assert jobs.get("b" * 32)["stage"] == "tombstoned"
    assert len(jobs.list()) == 3
