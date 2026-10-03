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


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['busy', 'maintenance', 'recovery'])
async def test_storage_queue_refreshes_independently_of_busy_or_blocked_cycle(
    tmp_path, monkeypatch, mode,
):
    import json
    from types import SimpleNamespace

    from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence

    monkeypatch.setattr(worker, '_STORAGE_QUEUE_REFRESH_SECONDS', 0.01, raising=False)
    path = tmp_path / 'storage-queue.json'
    marker = tmp_path / mode
    if mode != 'busy':
        marker.write_text('admission_enabled=false\n')
    second_refresh = asyncio.Event()
    release_cycle = asyncio.Event()
    running = True
    publications = []

    class Cycle:
        source = object()

        async def run_once(self):
            assert mode == 'busy', 'maintenance/recovery must not dispatch admission'
            await release_cycle.wait()

        async def capacity_refresh(self):
            evidence = CapacityEvidence(100, {}, pools=(
                PoolCapacityEvidence('ssd', 'fixture-uuid', 100),
            ))
            worker._publish_storage_queue(
                path, evidence, SimpleNamespace(pending_bytes=lambda *args, **kwargs: 10),
            )
            publications.append(json.loads(path.read_text()))
            if len(publications) >= 2:
                second_refresh.set()

    task = asyncio.create_task(worker._run_forever(
        Cycle(), should_run=lambda: running, interval=0.01,
        maintenance_path=marker if mode == 'maintenance' else None,
        recovery_mode_path=marker if mode == 'recovery' else None,
    ))
    try:
        await asyncio.wait_for(second_refresh.wait(), timeout=1)
        assert not task.done()
        assert len(publications) >= 2
        assert publications[-1]['measured_at'] > publications[0]['measured_at']
        assert publications[-1]['pools'][0]['available_bytes'] == 90
    finally:
        running = False
        release_cycle.set()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_failed_storage_refresh_keeps_last_evidence_and_shutdown_cancels_inflight(
    tmp_path, monkeypatch, caplog,
):
    from types import SimpleNamespace

    from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence

    monkeypatch.setattr(worker, '_STORAGE_QUEUE_REFRESH_SECONDS', 0.01, raising=False)
    path = tmp_path / 'storage-queue.json'
    release_cycle = asyncio.Event()
    refresh_inflight = asyncio.Event()
    refresh_cancelled = asyncio.Event()
    running = True
    first_publication = None
    calls = 0

    class Cycle:
        source = object()

        async def run_once(self):
            await release_cycle.wait()

        async def capacity_refresh(self):
            nonlocal first_publication, calls
            calls += 1
            if calls == 1:
                evidence = CapacityEvidence(100, {}, pools=(
                    PoolCapacityEvidence('ssd', 'fixture-uuid', 100),
                ))
                worker._publish_storage_queue(
                    path, evidence, SimpleNamespace(pending_bytes=lambda *args, **kwargs: 10),
                )
                first_publication = path.read_bytes()
                return
            if calls == 2:
                raise RuntimeError('private upstream credentials must not appear in logs')
            refresh_inflight.set()
            try:
                await asyncio.Event().wait()
            finally:
                refresh_cancelled.set()

    task = asyncio.create_task(worker._run_forever(
        Cycle(), should_run=lambda: running, interval=0.01,
    ))
    try:
        await asyncio.wait_for(refresh_inflight.wait(), timeout=1)
        assert calls == 3
        assert path.read_bytes() == first_publication
        assert 'RuntimeError' in caplog.text
        assert 'private upstream credentials' not in caplog.text
    finally:
        running = False
        release_cycle.set()
        await asyncio.wait_for(task, timeout=1)
    assert refresh_cancelled.is_set()
    assert path.read_bytes() == first_publication
