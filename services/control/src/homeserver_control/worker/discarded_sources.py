"""Reclaim stopped partial sources after the gateway proves exclusive ownership."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from homeserver_control.gateway.permits import Permit

LOGGER = logging.getLogger(__name__)
_SAFE_CLEANUP_DETAILS = {
    "season pack cleanup requires review", "manual source replacement is pending",
    "source transition still pending", "episode source import has started",
    "movie source import has started", "source has another active owner",
    "source protected by operator", "source protection unavailable",
    "discarded payload deletion is still pending", "torrent deletion is still pending",
    "admission blocked by worker health", "admission blocked by recovery mode",
    "exclusive storage placement unavailable", "current stopped source unavailable",
    "source paused by controller",
}


class DiscardedPermitSource(Protocol):
    def list_discarded_sources(
        self, *, limit: int = 100, after_id: str | None = None,
        stopped_infohashes: frozenset[str] | None = None,
    ) -> list[Permit]: ...


class DiscardedSourceReconciler:
    def __init__(
        self, *, permits: DiscardedPermitSource, gateway_url: str, arr_token: str,
        client: httpx.AsyncClient, clock: Callable[[], float] = time.monotonic,
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
        self._next_queue_warning = 0.0

    def _warn(self, permit: Permit, message: str) -> None:
        now = self.clock()
        if now >= self._next_warning.get(permit.permit_id, 0):
            LOGGER.warning("Discarded torrent %s: %s", permit.infohash, message)
            self._next_warning[permit.permit_id] = now + 900

    async def _stopped_infohashes(self) -> frozenset[str]:
        try:
            response = await self.client.get(
                f"{self.gateway_url}/api/v2/torrents/info",
                headers={"X-Arr-Token": self.arr_token},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                raise ValueError("invalid queue response")
            return frozenset(
                item["hash"].lower() for item in payload
                if isinstance(item.get("hash"), str)
                and item.get("state") in {"stoppedDL", "pausedDL"}
                and type(item.get("progress")) in {int, float} and 0 <= item["progress"] < 1
                and type(item.get("amount_left")) is int and item["amount_left"] > 0
            )
        except (httpx.HTTPError, ValueError) as error:
            now = self.clock()
            if now >= self._next_queue_warning:
                LOGGER.warning("Stopped torrent queue check unavailable (%s)", type(error).__name__)
                self._next_queue_warning = now + 900
            return frozenset()

    async def reconcile(self) -> int:
        now = self.clock()
        if now < self._next_run:
            return 0
        self._next_run = now + self.interval_seconds
        stopped = await self._stopped_infohashes()
        batch = self.permits.list_discarded_sources(
            limit=10, after_id=self._last_permit_id, stopped_infohashes=stopped,
        )
        if not batch and self._last_permit_id is not None:
            self._last_permit_id = None
            batch = self.permits.list_discarded_sources(limit=10, stopped_infohashes=stopped)
        if batch:
            self._last_permit_id = batch[-1].permit_id
        removed = 0
        for permit in batch:
            try:
                response = await self.client.post(
                    f"{self.gateway_url}/internal/discard-source",
                    headers={"X-Arr-Token": self.arr_token},
                    json={"permit_token": permit.token},
                )
                response.raise_for_status()
                payload = response.json()
                state = payload.get("state") if isinstance(payload, dict) else None
                if state == "deleted":
                    removed += 1
                    LOGGER.info("Removed discarded partial torrent %s", permit.infohash)
                elif state != "missing":
                    self._warn(permit, "invalid gateway cleanup response")
            except httpx.HTTPStatusError as error:
                detail = None
                try:
                    payload = error.response.json()
                    detail = payload.get("detail") if isinstance(payload, dict) else None
                except ValueError:
                    pass
                reason = f"HTTP {error.response.status_code}"
                if isinstance(detail, str) and detail in _SAFE_CLEANUP_DETAILS:
                    reason += f": {detail}"
                self._warn(permit, f"cleanup deferred ({reason})")
            except (httpx.HTTPError, ValueError) as error:
                # Never skip the rest of the page or expose credential-bearing URLs/tokens.
                self._warn(permit, f"cleanup deferred ({type(error).__name__})")
        return removed
