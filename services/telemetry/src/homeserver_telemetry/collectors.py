from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


async def collect_sources(
    sources: dict[str, Callable[[], Awaitable[dict[str, Any]]]],
    *,
    timeout_seconds: float = 3,
) -> dict[str, dict[str, Any]]:
    """Collect independent sources without letting one timeout block the rest."""

    async def collect_one(
        name: str, getter: Callable[[], Awaitable[dict[str, Any]]]
    ) -> tuple[str, dict[str, Any]]:
        try:
            return name, {"status": "ok", "data": await asyncio.wait_for(getter(), timeout_seconds)}
        except TimeoutError:
            return name, {"status": "error", "error": "timeout"}
        except Exception as error:  # source isolation is intentional
            return name, {"status": "error", "error": type(error).__name__}

    results = await asyncio.gather(*(collect_one(name, getter) for name, getter in sources.items()))
    return dict(results)
