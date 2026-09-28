import sqlite3
import time

import pytest

from homeserver_control.persistence.heartbeat import WorkerHeartbeatStore


def test_worker_health_requires_initialized_fresh_error_free_state(tmp_path):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    assert not store.ready(now=1000, max_age=30)
    store.write(now=1000, initialized=False, state="starting")
    assert not store.ready(now=1001, max_age=30)
    store.write(now=1002, initialized=True, state="running", success=True)
    assert store.ready(now=1003, max_age=30)
    assert not WorkerHeartbeatStore(store.db_path).ready(now=1033, max_age=30)
    store.write(now=1034, initialized=True, state="failed", error="TimeoutError")
    assert not store.ready(now=1035, max_age=30)
    assert store.snapshot()["last_success_at"] == 1002


def test_diagnostics_never_accept_arbitrary_exception_text(tmp_path):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(now=time.time(), initialized=True, state="failed", error="token=secret-value")
    assert store.snapshot()["last_error"] == "OperationalError"


def test_pulses_do_not_mask_missing_or_hung_successful_cycle(tmp_path):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(now=1000, initialized=True, state="running")
    assert not store.ready(now=1001, max_age=30)
    store.write(now=1002, initialized=True, state="running", success=True)
    store.write(now=1040, initialized=True, state="running")
    assert not store.ready(now=1041, max_age=30)


def test_bounded_busy_cycle_is_ready_without_fabricating_success(tmp_path):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(now=1000, initialized=True, state="running", cycle_deadline_at=1120)
    assert store.ready(now=1001, max_age=30)
    assert store.snapshot()["last_success_at"] is None

    store.write(now=1091, initialized=True, state="running", cycle_deadline_at=1120)
    assert store.ready(now=1091, max_age=30)
    assert store.snapshot()["cycle_deadline_at"] == 1120
    assert store.snapshot()["last_success_at"] is None
    assert not store.ready(now=1120, max_age=30)


def test_expired_busy_deadline_overrides_recent_success(tmp_path):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(
        now=1110, initialized=True, state="running", success=True, cycle_deadline_at=1120
    )
    assert store.ready(now=1119, max_age=30)
    assert not store.ready(now=1120, max_age=30)
    # An expired, unchanged pulse remains writable; it cannot restore readiness.
    store.write(now=1121, initialized=True, state="running", cycle_deadline_at=1120)
    assert not store.ready(now=1121, max_age=30)
    assert store.snapshot()["last_success_at"] == 1110


def test_completed_cycle_clears_deadline_and_records_actual_success(tmp_path):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(now=1000, initialized=True, state="running", cycle_deadline_at=1120)
    store.write(now=1100, initialized=True, state="running", success=True)
    assert store.snapshot()["cycle_deadline_at"] is None
    assert store.snapshot()["last_success_at"] == 1100
    assert store.ready(now=1101, max_age=30)
    store.write(now=1131, initialized=True, state="running")
    assert not store.ready(now=1131, max_age=30)


@pytest.mark.parametrize("state", ["starting", "failed", "maintenance"])
def test_busy_deadline_does_not_hide_nonrunning_state(tmp_path, state):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(now=1000, initialized=True, state=state, cycle_deadline_at=1120)
    assert not store.ready(now=1001, max_age=30)


@pytest.mark.parametrize(
    ("initialized", "error", "now"),
    [(False, None, 1001), (True, "TimeoutError", 1001), (True, None, 1031), (True, None, 999)],
)
def test_busy_deadline_requires_initialized_error_free_fresh_pulse(
    tmp_path, initialized, error, now
):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(
        now=1000, initialized=initialized, state="running", error=error, cycle_deadline_at=1120
    )
    assert not store.ready(now=now, max_age=30)


@pytest.mark.parametrize(
    "deadline", [0, -1, float("nan"), float("inf"), -float("inf"), "1120", True]
)
def test_busy_deadline_rejects_malformed_writes(tmp_path, deadline):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    with pytest.raises(ValueError, match="deadline"):
        store.write(now=1000, initialized=True, state="running", cycle_deadline_at=deadline)
    assert store.snapshot() == {}


@pytest.mark.parametrize("deadline", [0, -1, float("inf"), -float("inf"), "corrupt"])
def test_corrupt_persisted_deadline_is_unready_even_with_recent_success(tmp_path, deadline):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(now=1000, initialized=True, state="running", success=True)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("UPDATE worker_heartbeat SET cycle_deadline_at=?", (deadline,))
    assert not store.ready(now=1001, max_age=30)


def test_pulse_without_explicit_deadline_cannot_inherit_busy_lease(tmp_path):
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    store.write(now=1000, initialized=True, state="running", cycle_deadline_at=1120)
    store.write(now=1001, initialized=True, state="running")
    assert store.snapshot()["cycle_deadline_at"] is None
    assert store.snapshot()["last_success_at"] is None
    assert not store.ready(now=1001, max_age=30)


def test_legacy_heartbeat_database_migrates_without_losing_diagnostics(tmp_path):
    path = tmp_path / "control.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("""CREATE TABLE worker_heartbeat (
            id INTEGER PRIMARY KEY CHECK(id=1), last_seen_at REAL NOT NULL,
            last_success_at REAL, initialized INTEGER NOT NULL,
            state TEXT NOT NULL, last_error TEXT)""")
        connection.execute("INSERT INTO worker_heartbeat VALUES(1,1000,998,1,'running',NULL)")

    store = WorkerHeartbeatStore(path)
    assert store.snapshot() == {
        "id": 1,
        "last_seen_at": 1000,
        "last_success_at": 998,
        "initialized": 1,
        "state": "running",
        "last_error": None,
        "cycle_deadline_at": None,
    }
    assert store.ready(now=1001, max_age=30)
    store.write(now=1035, initialized=True, state="running", cycle_deadline_at=1100)
    reopened = WorkerHeartbeatStore(path)
    assert reopened.ready(now=1036, max_age=30)
    assert reopened.snapshot()["last_success_at"] == 998
    assert reopened.snapshot()["cycle_deadline_at"] == 1100
