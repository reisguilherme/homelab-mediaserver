"""Long work has a fixed health lease and an enforced cycle deadline."""

import asyncio
import time

import pytest

import homeserver_control.worker.__main__ as worker
from homeserver_control.persistence.heartbeat import WorkerHeartbeatStore


@pytest.mark.asyncio
async def test_legitimate_busy_cycle_outlives_freshness_without_renewing_lease(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr(worker.time, "time", lambda: clock[0])
    store = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    running = [True]

    class Cycle:
        source = object()
        calls = 0

        async def run_once(self):
            self.calls += 1
            if self.calls == 1:
                return
            started = store.snapshot()
            assert started["last_success_at"] == 1000
            deadline = started["cycle_deadline_at"]
            assert deadline == 1200
            clock[0] = 1101
            await asyncio.sleep(0.004)
            assert store.ready(now=clock[0], max_age=90)
            snapshot = store.snapshot()
            assert snapshot["cycle_deadline_at"] == deadline
            assert snapshot["last_success_at"] == 1000
            running[0] = False

    await worker._run_forever(
        Cycle(),
        should_run=lambda: running[0],
        interval=0.001,
        heartbeat=store,
        cycle_timeout_seconds=200,
    )
    assert store.snapshot()["last_success_at"] == 1101
    assert store.snapshot()["cycle_deadline_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["deadline", "exception"])
async def test_hung_or_failed_cycle_clears_lease_and_never_records_success(tmp_path, failure):
    running = [True]

    class StopOnFailure(WorkerHeartbeatStore):
        def write(self, **kwargs):
            super().write(**kwargs)
            if kwargs["state"] == "failed":
                running[0] = False

    store = StopOnFailure(tmp_path / "control.sqlite")

    class Cycle:
        source = object()
        cancelled = False

        async def run_once(self):
            if failure == "exception":
                raise ValueError("private failure content")
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    cycle = Cycle()
    await worker._run_forever(
        cycle,
        should_run=lambda: running[0],
        interval=0.001,
        heartbeat=store,
        cycle_timeout_seconds=0.02,
    )
    observed = store.snapshot()
    assert observed["state"] == "failed"
    assert observed["last_error"] == ("TimeoutError" if failure == "deadline" else "ValueError")
    assert observed["last_success_at"] is None
    assert observed["cycle_deadline_at"] is None
    assert not store.ready(now=time.time(), max_age=90)
    assert cycle.cancelled is (failure == "deadline")


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", ["maintenance", "recovery"])
async def test_blocked_worker_has_no_busy_lease_or_admission(tmp_path, marker):
    running = [True]
    path = tmp_path / marker
    path.write_text("admission_enabled=false\n")

    class StopWhenBlocked(WorkerHeartbeatStore):
        def write(self, **kwargs):
            super().write(**kwargs)
            if kwargs["state"] == "maintenance":
                running[0] = False

    store = StopWhenBlocked(tmp_path / "control.sqlite")

    class Cycle:
        source = object()

        async def run_once(self):
            pytest.fail("maintenance/recovery must not dispatch a cycle")

    await worker._run_forever(
        Cycle(),
        should_run=lambda: running[0],
        interval=0.001,
        heartbeat=store,
        maintenance_path=path if marker == "maintenance" else None,
        recovery_mode_path=path if marker == "recovery" else None,
        cycle_timeout_seconds=200,
    )
    assert store.snapshot()["cycle_deadline_at"] is None
    assert not store.ready(now=time.time(), max_age=90)
