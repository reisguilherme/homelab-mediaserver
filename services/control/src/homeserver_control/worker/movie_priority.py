"""Rank eligible movies while protecting the current series episodes."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

LOGGER = logging.getLogger(__name__)


class MoviePrioritizer:
    def __init__(
        self, *, gateway_url: str, arr_token: str, client: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic, interval_seconds: float = 60,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.arr_token = arr_token
        self.client = client
        self.clock = clock
        self.interval_seconds = interval_seconds
        self._next_run = 0.0

    async def prioritize(self) -> str:
        now = self.clock()
        if now < self._next_run:
            return "deferred"
        self._next_run = now + self.interval_seconds
        response = await self.client.post(
            f"{self.gateway_url}/internal/prioritize-movies",
            headers={"X-Arr-Token": self.arr_token},
            timeout=10.0,
        )
        response.raise_for_status()
        result = response.json()
        if (
            not isinstance(result, dict)
            or result.get("state") not in {"unchanged", "reordered"}
            or isinstance(result.get("count"), bool)
            or not isinstance(result.get("count"), int)
            or result["count"] < 0
        ):
            raise ValueError("invalid priority response")
        if result["state"] == "reordered":
            LOGGER.info("Adjusted queue priority for %s eligible downloads", result["count"])
        return result["state"]
