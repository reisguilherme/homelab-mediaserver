from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.gateway.permits import Permit
from homeserver_control.worker.source_reconciliation import SourceReconciler


def _permit(scope: str, suffix: str) -> Permit:
    return Permit(
        permit_id=f"permit-{scope}", token=f"token-{scope}",
        infohash=suffix * 40, destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        category="sonarr", reservation_id="season-reservation",
        scope_key=scope, budget_bytes=100, state="unknown",
    )


class UncertainPermits:
    def __init__(self, entries: list[Permit]) -> None:
        self.entries = entries
        self.calls = 0

    def list_uncertain(
        self, *, limit: int = 100, after_id: str | None = None
    ) -> list[Permit]:
        self.calls += 1
        assert limit == 10
        return [
            item for item in self.entries
            if after_id is None or item.permit_id > after_id
        ][:limit]


@pytest.mark.asyncio
async def test_reconciles_uncertain_permits_across_episode_scopes_and_throttles() -> None:
    permits = UncertainPermits([_permit("S01E01", "a"), _permit("S01E08", "b")])
    sent: list[str] = []
    now = [100.0]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/reconcile-source"
        assert request.headers["X-Arr-Token"] == "worker-secret"
        sent.append(request.read().decode())
        return httpx.Response(200, json={"state": "confirmed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reconciler = SourceReconciler(
            permits=permits, gateway_url="http://download-gateway:8081",
            arr_token="worker-secret", client=client, clock=lambda: now[0],
        )
        assert await reconciler.reconcile() == 2
        assert await reconciler.reconcile() == 0
        now[0] += 60
        assert await reconciler.reconcile() == 2

    assert permits.calls == 3
    assert sent[:2] == [
        '{"permit_token":"token-S01E01"}',
        '{"permit_token":"token-S01E08"}',
    ]


@pytest.mark.asyncio
async def test_reconciliation_failure_does_not_block_later_permit() -> None:
    permits = UncertainPermits([_permit("S01E01", "a"), _permit("S01E02", "b")])

    def handler(request: httpx.Request) -> httpx.Response:
        if b"token-S01E01" in request.content:
            return httpx.Response(409, json={"detail": "metadata unavailable"})
        return httpx.Response(200, json={"state": "confirmed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reconciler = SourceReconciler(
            permits=permits, gateway_url="http://download-gateway:8081",
            arr_token="worker-secret", client=client,
        )
        assert await reconciler.reconcile() == 1


@pytest.mark.asyncio
async def test_reconciliation_rotates_past_ten_persistently_missing_sources() -> None:
    permits = UncertainPermits([
        _permit(f"S01E{number:02}", "a") for number in range(1, 13)
    ])
    now = [100.0]
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.read().decode())
        assert request.extensions["timeout"]["read"] == 3.0
        return httpx.Response(200, json={"state": "missing"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reconciler = SourceReconciler(
            permits=permits, gateway_url="http://download-gateway:8081",
            arr_token="worker-secret", client=client, clock=lambda: now[0],
        )
        assert await reconciler.reconcile() == 0
        assert len(sent) == 10
        now[0] += 60
        assert await reconciler.reconcile() == 0
        assert len(sent) == 12
        assert "token-S01E12" in sent[-1]
