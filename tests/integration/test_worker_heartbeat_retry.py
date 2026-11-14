"""Heartbeat persistence may retry without extending a cycle health lease."""

import asyncio
import sqlite3

import pytest

import homeserver_control.worker.__main__ as worker
from homeserver_control.persistence.heartbeat import WorkerHeartbeatStore


@pytest.mark.asyncio
async def test_pulse_recovers_transient_sqlite_failure_without_fabricating_health(
    tmp_path, monkeypatch, caplog,
):
    clock = [1000.0]
    monkeypatch.setattr(worker.time, 'time', lambda: clock[0])
    recovered = asyncio.Event()
    release_cycle = asyncio.Event()
    running = True

    class TransientFailure(WorkerHeartbeatStore):
        writes = 0

        def write(self, **kwargs):
            self.writes += 1
            if self.writes in {2, 3} and not kwargs.get('success', False):
                clock[0] += 91
                assert not self.ready(now=clock[0], max_age=90)
                raise sqlite3.OperationalError('private database path and token')
            super().write(**kwargs)
            if self.writes == 4:
                recovered.set()

    store = TransientFailure(tmp_path / 'control.sqlite')

    class Cycle:
        source = object()

        async def run_once(self):
            await release_cycle.wait()

    task = asyncio.create_task(worker._run_forever(
        Cycle(), should_run=lambda: running, interval=0.001, heartbeat=store,
        cycle_timeout_seconds=200,
    ))
    try:
        try:
            await asyncio.wait_for(recovered.wait(), timeout=0.5)
        except TimeoutError:
            pass
        assert recovered.is_set(), 'heartbeat pulse died instead of retrying its SQLite write'
        snapshot = store.snapshot()
        assert snapshot['last_seen_at'] == 1182
        assert snapshot['last_success_at'] is None
        assert snapshot['cycle_deadline_at'] == 1200
        assert store.ready(now=1182, max_age=90)
        assert not store.ready(now=1200, max_age=90)
        assert 'OperationalError' in caplog.text
        assert 'private database path and token' not in caplog.text
    finally:
        running = False
        release_cycle.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=1)
