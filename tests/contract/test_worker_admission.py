"""Expired worker leases block production mutation paths before side effects."""

import time

import pytest
from fastapi.testclient import TestClient

from homeserver_control.api.app import ControlState
from homeserver_control.api.app import create_app as control_app
from homeserver_control.gateway.app import create_app as gateway_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.heartbeat import WorkerHeartbeatStore


@pytest.mark.parametrize("deadline_state", ["busy", "expired"])
def test_api_admission_follows_fixed_busy_lease(tmp_path, deadline_state):
    now = time.time()
    database = tmp_path / "control.sqlite"
    heartbeat = WorkerHeartbeatStore(database)
    heartbeat.write(now=now - 101, initialized=True, state="running", success=True)
    heartbeat.write(
        now=now,
        initialized=True,
        state="running",
        cycle_deadline_at=now + 30 if deadline_state == "busy" else now - 1,
    )
    media = tmp_path / "media"
    media.mkdir()
    item = media / "fixture.mkv"
    item.write_bytes(b"fixture")
    state = ControlState(
        db_path=database,
        media_roots=(media,),
        admin_token="fixture",
        csrf_token="fixture",
        media_catalog={"movie:tmdb:10": (item,)},
        worker_health_required=True,
    )
    response = TestClient(control_app(state=state)).post(
        "/api/v1/deletions/preview",
        headers={"X-Admin-Token": "fixture", "X-CSRF-Token": "fixture"},
        json={"media_key": "movie:tmdb:10", "paths": [str(item)]},
    )
    assert response.status_code == (200 if deadline_state == "busy" else 503)
    assert item.read_bytes() == b"fixture"
    assert heartbeat.snapshot()["last_success_at"] == now - 101


@pytest.mark.parametrize("provider", ["expired", "unavailable"])
def test_gateway_worker_health_guard_precedes_dispatch(tmp_path, provider):
    heartbeat = WorkerHeartbeatStore(tmp_path / "control.sqlite")
    now = time.time()
    heartbeat.write(
        now=now, initialized=True, state="running", success=True, cycle_deadline_at=now - 1
    )

    def worker_health():
        if provider == "unavailable":
            raise OSError("private storage failure")
        return heartbeat.ready(now=time.time(), max_age=90)

    class NoDispatch:
        def add_torrent(self, _payload):
            pytest.fail("unhealthy worker must not dispatch qBit mutation")

    client = TestClient(
        gateway_app(
            permits=PermitRegistry(),
            upstream=NoDispatch(),
            arr_token="fixture",
            worker_health_provider=worker_health,
        )
    )
    response = client.post("/api/v2/torrents/add", headers={"X-Arr-Token": "fixture"}, json={})
    assert response.status_code == 503
    assert "private" not in response.text
