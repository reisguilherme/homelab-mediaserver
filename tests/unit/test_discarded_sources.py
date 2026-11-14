import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.gateway.permits import Permit


def historical(number):
    return Permit(
        permit_id=f"permit-{number:02}", token=f"token-{number:02}",
        infohash=f"{number:040x}", destination=f"/data/torrents/.placements/permit-{number:02}",
        expires_at=datetime.now(UTC) + timedelta(hours=1), state="probe_rejected",
    )


class DiscardedPermits:
    def __init__(self, count):
        self.items = [historical(number) for number in range(1, count + 1)]

    def list_discarded_sources(self, *, limit=100, after_id=None, stopped_infohashes=None):
        assert limit == 10
        candidates = [item for item in self.items if after_id is None or item.permit_id > after_id]
        if stopped_infohashes is not None:
            candidates = [item for item in candidates if item.state != "confirmed"
                          or item.infohash in stopped_infohashes]
        return candidates[:limit]


@pytest.mark.asyncio
async def test_cleanup_rotates_past_missing_or_blocked_history_and_throttles():
    from homeserver_control.worker.discarded_sources import DiscardedSourceReconciler

    permits = DiscardedPermits(12)
    now = [100.0]
    sent = []

    def handle(request):
        if request.method == "GET":
            assert request.url.path == "/api/v2/torrents/info"
            return httpx.Response(200, json=[])
        assert request.url.path == "/internal/discard-source"
        assert request.headers["X-Arr-Token"] == "worker-secret"
        token = json.loads(request.content)["permit_token"]
        sent.append(token)
        if token == "token-01":
            return httpx.Response(409, json={"detail": "source protected"})
        if token == "token-12":
            return httpx.Response(200, json={"state": "deleted"})
        return httpx.Response(200, json={"state": "missing"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        reconciler = DiscardedSourceReconciler(
            permits=permits, gateway_url="http://gateway", arr_token="worker-secret",
            client=client, clock=lambda: now[0],
        )
        assert await reconciler.reconcile() == 0
        assert len(sent) == 10
        assert await reconciler.reconcile() == 0
        assert len(sent) == 10
        now[0] += 60
        assert await reconciler.reconcile() == 1
        assert sent[-2:] == ["token-11", "token-12"]
        now[0] += 60
        assert await reconciler.reconcile() == 0
        assert len(sent) == 22


@pytest.mark.asyncio
async def test_cleanup_treats_uncertain_or_invalid_success_response_as_retryable(caplog):
    from homeserver_control.worker.discarded_sources import DiscardedSourceReconciler

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        token = json.loads(request.content)["permit_token"]
        if token == "token-01":
            return httpx.Response(503, json={"detail": "deletion still pending"})
        if token == "token-02":
            return httpx.Response(200, json={"state": "unexpected"})
        return httpx.Response(200, json={"state": "deleted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        reconciler = DiscardedSourceReconciler(
            permits=DiscardedPermits(3), gateway_url="http://gateway", arr_token="secret",
            client=client,
        )
        assert await reconciler.reconcile() == 1
    assert "token-01" not in caplog.text and "token-02" not in caplog.text


@pytest.mark.asyncio
async def test_cleanup_native_filter_ignores_current_active_queued_stalled_and_completed_sources():
    from homeserver_control.worker.discarded_sources import DiscardedSourceReconciler

    permits = DiscardedPermits(7)
    for permit in permits.items[:6]:
        permit.state = "confirmed"
    states = ["stoppedDL", "pausedDL", "stalledDL", "queuedDL", "stoppedUP", "stoppedDL"]
    sent = []

    def handle(request):
        assert request.headers["X-Arr-Token"] == "worker-secret"
        if request.method == "GET":
            assert request.url.path == "/api/v2/torrents/info"
            return httpx.Response(200, json=[
                {"hash": permit.infohash, "state": state,
                 "progress": 1 if number == 5 else 0.1,
                 "amount_left": 0 if number == 5 else 111}
                for number, (permit, state) in enumerate(zip(permits.items, states, strict=False))
            ])
        sent.append(json.loads(request.content)["permit_token"])
        return httpx.Response(200, json={"state": "deleted"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        reconciler = DiscardedSourceReconciler(
            permits=permits, gateway_url="http://gateway", arr_token="worker-secret", client=client,
        )
        assert await reconciler.reconcile() == 3
    assert sent == ["token-01", "token-02", "token-07"]


@pytest.mark.asyncio
async def test_cleanup_logs_only_allowed_gateway_reason_without_secrets(caplog):
    from homeserver_control.worker.discarded_sources import DiscardedSourceReconciler

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json=[])
        token = json.loads(request.content)["permit_token"]
        return httpx.Response(409, json={
            "detail": "season pack cleanup requires review" if token == "token-01"
            else "token-02 at http://user:password@host/secret", "token": token,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        reconciler = DiscardedSourceReconciler(
            permits=DiscardedPermits(2), gateway_url="http://gateway",
            arr_token="secret", client=client,
        )
        assert await reconciler.reconcile() == 0
    assert "season pack cleanup requires review" in caplog.text
    assert "HTTP 409" in caplog.text
    assert "token-01" not in caplog.text and "token-02" not in caplog.text
    assert "password" not in caplog.text and "secret" not in caplog.text
