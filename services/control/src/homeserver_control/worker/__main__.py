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

import httpx

from homeserver_control.adapters.seerr import SeerrAdapter
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.recovery import recovery_mode_blocks

from .acquisition import MovieAcquirer
from .cancellation import CancellationReconciler
from .capacity_evidence import read_capacity_evidence
from .finalization import MovieFinalizer
from .runtime import WorkerCycle
from .scheduler import AdmissionScheduler, FilesystemSnapshot
from .series_acquisition import SeriesAcquirer
from .series_finalization import SeriesFinalizer
from .subdl import SubDLSource

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
    key_file = os.environ.get("HOMESERVER_SUBDL_API_KEY_FILE")
    subdl_key = Path(key_file).read_text(encoding="utf-8").strip() if key_file else None
    if key_file and not subdl_key:
        raise ValueError("SubDL key file is empty")
    subtitle_store = SubtitleArtifactStore(database) if subdl_key else None
    torrent_store = TorrentArtifactStore(database)
    source = SeerrAdapter(base_url=seerr_url, api_key=seerr_key)
    scheduler = AdmissionScheduler(
        repository=repository,
        snapshot_provider=lambda: _snapshot_from_file(Path(snapshot_path)),
        clock=time.time,
    )
    radarr_url = os.environ.get("HOMESERVER_RADARR_URL")
    radarr_key = os.environ.get("HOMESERVER_RADARR_API_KEY")
    sonarr_url = os.environ.get("HOMESERVER_SONARR_URL")
    sonarr_key = os.environ.get("HOMESERVER_SONARR_API_KEY")
    arr_token = os.environ.get("HOMESERVER_ARR_TOKEN")
    permits = (
        PermitRegistry(database)
        if (radarr_url and radarr_key) or (sonarr_url and sonarr_key)
        else None
    )
    if permits is not None:
        repository.normalize_verified_budgets()
    async def capacity_provider():
        if not arr_token:
            raise ValueError("gateway token is required for capacity evidence")
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as capacity_client:
            return await read_capacity_evidence(
                snapshot_path=Path(snapshot_path), data_root=Path("/data"),
                gateway_url="http://download-gateway:8081",
                arr_token=arr_token, client=capacity_client,
            )
    acquirer = None
    finalizer = None
    series_acquirer = None
    series_finalizer = None
    if radarr_url and radarr_key:
        assert permits is not None
        movie_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        acquirer = MovieAcquirer(
            repository=repository,
            permits=permits,
            radarr_url=radarr_url,
            radarr_api_key=radarr_key,
            prowlarr_url=os.environ.get("HOMESERVER_PROWLARR_URL", "http://prowlarr:9696"),
            client=movie_client,
            subtitle_source=(
                SubDLSource(api_key=subdl_key, client=movie_client) if subdl_key else None
            ),
            subtitle_store=subtitle_store,
            torrent_store=torrent_store,
            capacity_provider=capacity_provider,
        )
        if arr_token:
            finalizer_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
            finalizer = MovieFinalizer(
                repository=repository,
                permits=permits,
                torrent_root="/data/torrents",
                gateway_url="http://download-gateway:8081",
                arr_token=arr_token,
                radarr_url=radarr_url,
                radarr_api_key=radarr_key,
                client=finalizer_client,
                subtitle_source=(
                    SubDLSource(api_key=subdl_key, client=finalizer_client)
                    if subdl_key else None
                ),
            )
    if sonarr_url and sonarr_key:
        assert permits is not None
        series_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        series_acquirer = SeriesAcquirer(
            repository=repository, permits=permits,
            sonarr_url=sonarr_url, sonarr_api_key=sonarr_key,
            prowlarr_url=os.environ.get("HOMESERVER_PROWLARR_URL", "http://prowlarr:9696"),
            client=series_client,
            subtitle_source=(
                SubDLSource(api_key=subdl_key, client=series_client) if subdl_key else None
            ),
            subtitle_store=subtitle_store,
            torrent_store=torrent_store,
            capacity_provider=capacity_provider,
        )
        if arr_token:
            series_finalizer_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
            series_finalizer = SeriesFinalizer(
                repository=repository, permits=permits,
                torrent_root="/data/torrents",
                gateway_url="http://download-gateway:8081",
                arr_token=arr_token, sonarr_url=sonarr_url,
                sonarr_api_key=sonarr_key,
                client=series_finalizer_client,
                subtitle_source=(
                    SubDLSource(api_key=subdl_key, client=series_finalizer_client)
                    if subdl_key else None
                ),
            )
    return WorkerCycle(
        source=source, scheduler=scheduler, acquirer=acquirer, finalizer=finalizer,
        series_acquirer=series_acquirer, series_finalizer=series_finalizer,
        cancellation=CancellationReconciler(source=source, repository=repository),
    )


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
        if cycle is not None and isinstance(getattr(cycle, "finalizer", None), MovieFinalizer):
            await cycle.finalizer.client.aclose()
        if cycle is not None and isinstance(
            getattr(cycle, "series_acquirer", None), SeriesAcquirer
        ):
            await cycle.series_acquirer.client.aclose()
        if cycle is not None and isinstance(
            getattr(cycle, "series_finalizer", None), SeriesFinalizer
        ):
            await cycle.series_finalizer.client.aclose()


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
