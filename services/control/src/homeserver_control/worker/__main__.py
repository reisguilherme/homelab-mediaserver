"""Run the control worker as ``python -m homeserver_control.worker``."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from homeserver_control.adapters.seerr import SeerrAdapter
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.recovery import recovery_mode_blocks

from .acquisition import MovieAcquirer
from .runtime import WorkerCycle
from .scheduler import AdmissionScheduler, FilesystemSnapshot

LOGGER = logging.getLogger(__name__)


def _snapshot_from_file(path: Path) -> FilesystemSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return FilesystemSnapshot(
        filesystem_id=payload.get("filesystem_id"),
        free_bytes=int(payload["free_bytes"]),
        total_bytes=int(payload["total_bytes"]),
        measured_at=float(payload["measured_at"]),
    )


def _build_cycle(database: Path) -> WorkerCycle | None:
    recovery_mode_path = os.environ.get(
        "HOMESERVER_RECOVERY_MODE", "/var/lib/homeserver/RECOVERY_MODE"
    )
    if recovery_mode_blocks(recovery_mode_path):
        return None
    seerr_url = os.environ.get("HOMESERVER_SEERR_URL")
    seerr_key = os.environ.get("HOMESERVER_SEERR_API_KEY")
    snapshot_path = os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT")
    if not seerr_url or not seerr_key or not snapshot_path:
        return None
    repository = ReservationRepository(database)
    repository.initialize()
    source = SeerrAdapter(base_url=seerr_url, api_key=seerr_key)
    scheduler = AdmissionScheduler(
        repository=repository,
        snapshot_provider=lambda: _snapshot_from_file(Path(snapshot_path)),
        clock=time.time,
    )
    radarr_url = os.environ.get("HOMESERVER_RADARR_URL")
    radarr_key = os.environ.get("HOMESERVER_RADARR_API_KEY")
    acquirer = None
    if radarr_url and radarr_key:
        acquirer = MovieAcquirer(
            repository=repository,
            permits=PermitRegistry(database),
            radarr_url=radarr_url,
            radarr_api_key=radarr_key,
            prowlarr_url=os.environ.get("HOMESERVER_PROWLARR_URL", "http://prowlarr:9696"),
        )
    return WorkerCycle(source=source, scheduler=scheduler, acquirer=acquirer)


async def _run_forever(
    cycle: WorkerCycle | None,
    *,
    should_run: Callable[[], bool],
    interval: float,
    recovery_mode_path: str | Path | None = None,
) -> None:
    """Keep one event loop alive for the lifetime of the Seerr HTTP client."""
    try:
        while should_run():
            if cycle is not None and not recovery_mode_blocks(recovery_mode_path):
                try:
                    await cycle.run_once()
                except Exception:
                    LOGGER.exception("worker cycle failed; admission remains fail-closed")
            if should_run():
                await asyncio.sleep(interval)
    finally:
        if cycle is not None and isinstance(cycle.source, SeerrAdapter):
            await cycle.source.client.aclose()
        if cycle is not None and isinstance(getattr(cycle, "acquirer", None), MovieAcquirer):
            await cycle.acquirer.client.aclose()


def main() -> None:
    database = Path(os.environ.get("HOMESERVER_DB_PATH", "/var/lib/homeserver/control.sqlite"))
    recovery_mode_path = os.environ.get(
        "HOMESERVER_RECOVERY_MODE", "/var/lib/homeserver/RECOVERY_MODE"
    )
    cycle = _build_cycle(database)
    if os.environ.get("HOMESERVER_WORKER_ONCE") == "1":
        if cycle is not None and not recovery_mode_blocks(recovery_mode_path):
            asyncio.run(cycle.run_once())
        return

    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    interval = max(1.0, float(os.environ.get("HOMESERVER_WORKER_INTERVAL", "5")))
    asyncio.run(
        _run_forever(
            cycle,
            should_run=lambda: running,
            interval=interval,
            recovery_mode_path=recovery_mode_path,
        )
    )


if __name__ == "__main__":
    main()
