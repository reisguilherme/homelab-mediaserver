"""Recover uncertain gateway additions only after the gateway verifies qBittorrent."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from homeserver_control.gateway.permits import Permit

LOGGER = logging.getLogger(__name__)


class UncertainPermitSource(Protocol):
    def list_uncertain(
        self, *, limit: int = 100, after_id: str | None = None
    ) -> list[Permit]: ...


class SourceReconciler:
    """Check every uncertain movie/episode slot, including later series episodes."""

    def __init__(
        self, *, permits: UncertainPermitSource, gateway_url: str,
        arr_token: str, client: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
        interval_seconds: float = 60,
    ) -> None:
        self.permits = permits
        self.gateway_url = gateway_url.rstrip("/")
        self.arr_token = arr_token
        self.client = client
        self.clock = clock
        self.interval_seconds = interval_seconds
        self._next_run = 0.0
        self._last_permit_id: str | None = None
        self._next_warning: dict[str, float] = {}

    def _warn(self, permit: Permit, message: str) -> None:
        now = self.clock()
        if now >= self._next_warning.get(permit.permit_id, 0):
            LOGGER.warning("Torrent %s: %s", permit.infohash, message)
            self._next_warning[permit.permit_id] = now + 900

    async def reconcile(self) -> int:
        now = self.clock()
        if now < self._next_run:
            return 0
        self._next_run = now + self.interval_seconds
        confirmed = 0
        batch = self.permits.list_uncertain(limit=10, after_id=self._last_permit_id)
        if not batch and self._last_permit_id is not None:
            self._last_permit_id = None
            batch = self.permits.list_uncertain(limit=10)
        if batch:
            self._last_permit_id = batch[-1].permit_id
        for permit in batch:
            try:
                response = await self.client.post(
                    f"{self.gateway_url}/internal/reconcile-source",
                    headers={"X-Arr-Token": self.arr_token},
                    json={"permit_token": permit.token},
                    timeout=3.0,
                )
                response.raise_for_status()
                result = response.json()
                state = result.get("state") if isinstance(result, dict) else None
                if state == "confirmed":
                    confirmed += 1
                    LOGGER.info("Reconciled torrent %s", permit.infohash)
                elif state == "missing":
                    self._warn(permit, "uncertain source absent from qBittorrent")
                else:
                    self._warn(permit, "invalid gateway reconciliation response")
            except (httpx.HTTPError, ValueError) as error:
                self._warn(permit, f"reconciliation failed ({type(error).__name__})")
        return confirmed
