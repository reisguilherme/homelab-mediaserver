"""Run the control worker as ``python -m homeserver_control.worker``."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from homeserver_common.filesystem import filesystem_identity as _media_filesystem_id
from homeserver_common.storage import load_storage_registry
from homeserver_control.adapters.seerr import SeerrAdapter
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.persistence.heartbeat import WorkerHeartbeatStore
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.recovery import recovery_mode_blocks

from .acquisition import MovieAcquirer
from .cancellation import CancellationReconciler
from .capacity_evidence import read_capacity_evidence
from .deletion_coordinator import DeletionCoordinator
from .finalization import MovieFinalizer
from .movie_priority import MoviePrioritizer
from .release_quality import ReleasePolicy
from .runtime import WorkerCycle
from .scheduler import AdmissionScheduler, FilesystemSnapshot
from .series_acquisition import SeriesAcquirer
from .series_finalization import SeriesFinalizer
from .source_health import SourceHealthStore
from .source_reconciliation import SourceReconciler
from .subdl import SubDLSource
from .subtitle_language import SubtitlePolicy
from .tracker_availability import TrackerAvailabilityProbe

LOGGER = logging.getLogger(__name__)
_STORAGE_QUEUE_REFRESH_SECONDS = 5


def _publish_storage_queue(path: Path, evidence, permits: PermitRegistry) -> None:
    if not evidence.pools:
        return
    pools = []
    for pool in evidence.pools:
        pending = permits.pending_bytes(evidence, pool_id=pool.pool_id)
        pools.append({'pool_id': pool.pool_id, 'filesystem_id': pool.filesystem_id,
                      'pending_bytes': pending,
                      'available_bytes': max(0, pool.free_bytes - pending)})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, encoding='utf-8',
                                         prefix='.storage-queue-', delete=False) as output:
            temporary = Path(output.name)
            json.dump({'measured_at': time.time(), 'pools': pools}, output)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _snapshot_from_file(
    path: Path, *, expected_filesystem_id=None, max_age_seconds=30
) -> FilesystemSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    filesystem_id = payload.get("filesystem_id")
    if expected_filesystem_id is not None and filesystem_id != expected_filesystem_id:
        filesystem_id = None
    if type(payload.get("free_bytes")) is not int or type(payload.get("total_bytes")) is not int:
        raise ValueError("invalid capacity snapshot")
    if payload["free_bytes"] > payload["total_bytes"]:
        filesystem_id = None
    return FilesystemSnapshot(
        filesystem_id=filesystem_id,
        free_bytes=int(payload["free_bytes"]),
        total_bytes=int(payload["total_bytes"]),
        measured_at=float(payload["measured_at"]),
        stale_after_seconds=max_age_seconds,
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
    storage_registry = load_storage_registry()
    repository = ReservationRepository(database)
    repository.initialize()
    deletion_jobs = DeletionJobStore(database)
    subdl_key = os.environ.get("HOMESERVER_SUBDL_API_KEY")
    subtitle_store = SubtitleArtifactStore(database) if subdl_key else None
    torrent_store = TorrentArtifactStore(database)
    subtitle_policy = SubtitlePolicy.from_environment(os.environ)
    movie_release_policy = ReleasePolicy.from_environment(os.environ, media_kind="movie")
    series_release_policy = ReleasePolicy.from_environment(os.environ, media_kind="series")
    gateway_url = os.environ.get("HOMESERVER_QBIT_GATEWAY_URL", "http://download-gateway:8081")
    http_timeout = float(os.environ.get("HOMESERVER_HTTP_TIMEOUT_SECONDS", "15"))
    search_timeout = float(os.environ.get("HOMESERVER_SEARCH_TIMEOUT_SECONDS", "90"))
    retry_seconds = float(os.environ.get("HOMESERVER_SEARCH_RETRY_SECONDS", "300"))
    source_retry_seconds = float(os.environ.get("HOMESERVER_SOURCE_SEARCH_RETRY_SECONDS", "300"))
    live_probes = os.environ.get("HOMESERVER_SOURCE_PROBE_ENABLED", "true") == "true"
    source = SeerrAdapter(
        base_url=seerr_url,
        api_key=seerr_key,
        client=httpx.AsyncClient(timeout=httpx.Timeout(http_timeout)),
    )
    scheduler = AdmissionScheduler(
        repository=repository,
        snapshot_provider=lambda: _snapshot_from_file(
            Path(snapshot_path),
            expected_filesystem_id=(storage_registry.pools['ssd'].filesystem_id
                                    if storage_registry is not None else
                                    os.environ.get("HOMESERVER_MEDIA_UUID") or None),
            max_age_seconds=float(
                os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS", "30")
            ),
        ),
        clock=time.time,
    )
    radarr_url = os.environ.get("HOMESERVER_RADARR_URL")
    radarr_key = os.environ.get("HOMESERVER_RADARR_API_KEY")
    sonarr_url = os.environ.get("HOMESERVER_SONARR_URL")
    sonarr_key = os.environ.get("HOMESERVER_SONARR_API_KEY")
    arr_token = os.environ.get("HOMESERVER_ARR_TOKEN")
    arr_uid = int(os.environ["HOMESERVER_ARR_UID"]) if "HOMESERVER_ARR_UID" in os.environ else None
    arr_gid = int(os.environ["HOMESERVER_ARR_GID"]) if "HOMESERVER_ARR_GID" in os.environ else None
    permits = (
        PermitRegistry(database, storage_registry=storage_registry)
        if (radarr_url and radarr_key) or (sonarr_url and sonarr_key)
        else None
    )
    if permits is not None:
        repository.normalize_verified_budgets()
    health_store = (
        SourceHealthStore(
            database,
            slow_seconds=float(os.environ.get("HOMESERVER_SOURCE_SLOW_WINDOW_SECONDS", "300")),
            stalled_seconds=float(os.environ.get("HOMESERVER_SOURCE_STALL_SECONDS", "300")),
            slow_bytes_per_second=float(os.environ.get("HOMESERVER_SOURCE_MIN_RATE_KIB", "1024"))
            * 1024,
            probe_seconds=float(os.environ.get("HOMESERVER_SOURCE_PROBE_SECONDS", "60")),
            min_eta_gain=float(os.environ.get("HOMESERVER_SOURCE_MIN_TIME_GAIN_PERCENT", "20"))
            / 100,
            slow_replacement_enabled=os.environ.get(
                "HOMESERVER_SOURCE_SLOW_REPLACEMENT_ENABLED", "false"
            ) == "true",
        )
        if permits is not None and arr_token
        else None
    )
    if health_store is not None:
        for infohash in os.environ.get("HOMESERVER_SOURCE_PROTECTED_HASHES", "").split(","):
            if infohash.strip():
                health_store.protect_source(infohash.strip())
    availability_probe = TrackerAvailabilityProbe()

    async def capacity_provider():
        if not arr_token:
            raise ValueError("gateway token is required for capacity evidence")
        async with httpx.AsyncClient(timeout=httpx.Timeout(http_timeout)) as capacity_client:
            evidence = await read_capacity_evidence(
                snapshot_path=Path(snapshot_path),
                data_root=Path("/data"),
                gateway_url=gateway_url,
                arr_token=arr_token,
                client=capacity_client,
                storage_registry=storage_registry,
                expected_filesystem_id=_media_filesystem_id(
                    Path("/data"), os.environ.get("HOMESERVER_MEDIA_UUID")
                ),
                max_age_seconds=float(
                    os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS", "30")
                ),
            )
        if storage_registry is not None and permits is not None:
            _publish_storage_queue(database.parent / 'storage-queue.json', evidence, permits)
        return evidence

    acquirer = None
    finalizer = None
    series_acquirer = None
    series_finalizer = None
    if radarr_url and radarr_key:
        assert permits is not None
        movie_client = httpx.AsyncClient(timeout=httpx.Timeout(http_timeout))
        acquirer = MovieAcquirer(
            repository=repository,
            permits=permits,
            radarr_url=radarr_url,
            radarr_api_key=radarr_key,
            prowlarr_url=os.environ.get("HOMESERVER_PROWLARR_URL", "http://prowlarr:9696"),
            client=movie_client,
            subtitle_source=(
                SubDLSource(api_key=subdl_key, client=movie_client, policy=subtitle_policy)
                if subdl_key
                else None
            ),
            subtitle_store=subtitle_store,
            torrent_store=torrent_store,
            capacity_provider=capacity_provider,
            health_store=health_store,
            gateway_url=gateway_url if arr_token else None,
            arr_token=arr_token,
            availability_probe=availability_probe,
            live_source_probes=live_probes,
            release_policy=movie_release_policy,
            subtitle_policy=subtitle_policy,
            retry_seconds=retry_seconds,
            source_retry_seconds=source_retry_seconds,
            search_timeout_seconds=search_timeout,
        )
        if arr_token:
            finalizer_client = httpx.AsyncClient(timeout=httpx.Timeout(http_timeout))
            finalizer = MovieFinalizer(
                repository=repository,
                permits=permits,
                torrent_root="/data/torrents",
                gateway_url=gateway_url,
                arr_token=arr_token,
                radarr_url=radarr_url,
                radarr_api_key=radarr_key,
                client=finalizer_client,
                subtitle_source=(
                    SubDLSource(api_key=subdl_key, client=finalizer_client, policy=subtitle_policy)
                    if subdl_key
                    else None
                ),
                capacity_provider=capacity_provider,
                import_uid=arr_uid,
                import_gid=arr_gid,
                subtitle_policy=subtitle_policy,
            )
    if sonarr_url and sonarr_key:
        assert permits is not None
        series_client = httpx.AsyncClient(timeout=httpx.Timeout(http_timeout))
        series_acquirer = SeriesAcquirer(
            repository=repository,
            permits=permits,
            sonarr_url=sonarr_url,
            sonarr_api_key=sonarr_key,
            prowlarr_url=os.environ.get("HOMESERVER_PROWLARR_URL", "http://prowlarr:9696"),
            client=series_client,
            subtitle_source=(
                SubDLSource(api_key=subdl_key, client=series_client, policy=subtitle_policy)
                if subdl_key
                else None
            ),
            subtitle_store=subtitle_store,
            torrent_store=torrent_store,
            capacity_provider=capacity_provider,
            gateway_url=gateway_url if arr_token else None,
            arr_token=arr_token,
            health_store=health_store,
            is_tombstoned=deletion_jobs.is_tombstoned,
            availability_probe=availability_probe,
            live_source_probes=live_probes,
            release_policy=series_release_policy,
            subtitle_policy=subtitle_policy,
            retry_seconds=retry_seconds,
            source_retry_seconds=source_retry_seconds,
            search_timeout_seconds=search_timeout,
            download_window=int(os.environ.get("HOMESERVER_SERIES_DOWNLOAD_WINDOW", "10")),
            max_active_downloads=int(os.environ.get("HOMESERVER_DOWNLOAD_MAX_ACTIVE", "10")),
            prefer_season_pack=(
                os.environ.get("HOMESERVER_SERIES_PREFER_SEASON_PACK", "true") == "true"
            ),
            release_affinity=(
                os.environ.get("HOMESERVER_SERIES_RELEASE_AFFINITY", "true") == "true"
            ),
        )
        if arr_token:
            series_finalizer_client = httpx.AsyncClient(timeout=httpx.Timeout(http_timeout))
            series_finalizer = SeriesFinalizer(
                repository=repository,
                permits=permits,
                torrent_root="/data/torrents",
                gateway_url=gateway_url,
                arr_token=arr_token,
                sonarr_url=sonarr_url,
                sonarr_api_key=sonarr_key,
                client=series_finalizer_client,
                subtitle_source=(
                    SubDLSource(
                        api_key=subdl_key, client=series_finalizer_client, policy=subtitle_policy
                    )
                    if subdl_key
                    else None
                ),
                capacity_provider=capacity_provider,
                import_uid=arr_uid,
                import_gid=arr_gid,
                is_tombstoned=deletion_jobs.is_tombstoned,
                subtitle_policy=subtitle_policy,
            )
    jellyfin_key = os.environ.get("HOMESERVER_JELLYFIN_API_KEY")
    media_uuid = _media_filesystem_id(
        Path(os.environ.get("HOMESERVER_MEDIA_ROOT", "/data")),
        os.environ.get("HOMESERVER_MEDIA_UUID"),
    )
    if storage_registry is not None:
        media_uuid = storage_registry.pools['ssd'].filesystem_id
    deletion_coordinator = None
    if all(
        (
            media_uuid,
            jellyfin_key,
            radarr_url,
            radarr_key,
            sonarr_url,
            sonarr_key,
            seerr_url,
            seerr_key,
            arr_token,
        )
    ):
        deletion_coordinator = DeletionCoordinator(
            jobs=deletion_jobs,
            media_root="/data/media",
            data_root="/data",
            snapshot_path=snapshot_path,
            filesystem_id=media_uuid,
            storage_registry=storage_registry,
            radarr_url=radarr_url,
            radarr_api_key=radarr_key,
            sonarr_url=sonarr_url,
            sonarr_api_key=sonarr_key,
            seerr_url=seerr_url,
            seerr_api_key=seerr_key,
            gateway_url=gateway_url,
            arr_token=arr_token,
            jellyfin_url=os.environ.get("HOMESERVER_JELLYFIN_URL", "http://jellyfin:8096"),
            jellyfin_api_key=jellyfin_key,
        )
    return WorkerCycle(
        source=source,
        scheduler=scheduler,
        capacity_refresh=capacity_provider if storage_registry is not None and permits else None,
        acquirer=acquirer,
        finalizer=finalizer,
        series_acquirer=series_acquirer,
        series_finalizer=series_finalizer,
        cancellation=CancellationReconciler(source=source, repository=repository),
        source_reconciler=(
            SourceReconciler(
                permits=permits,
                gateway_url=gateway_url,
                arr_token=arr_token,
                client=httpx.AsyncClient(timeout=httpx.Timeout(http_timeout)),
            )
            if permits is not None and arr_token
            else None
        ),
        movie_prioritizer=(
            MoviePrioritizer(
                gateway_url=gateway_url,
                arr_token=arr_token,
                client=httpx.AsyncClient(timeout=httpx.Timeout(http_timeout)),
                interval_seconds=float(
                    os.environ.get("HOMESERVER_MOVIE_PRIORITY_INTERVAL_SECONDS", "60")
                ),
            )
            if permits is not None and arr_token
            else None
        ),
        deletion_coordinator=deletion_coordinator,
    )


async def _run_forever(
    cycle: WorkerCycle | None,
    *,
    should_run: Callable[[], bool],
    interval: float,
    recovery_mode_path: str | Path | None = None,
    heartbeat: WorkerHeartbeatStore | None = None,
    maintenance_path: str | Path | None = None,
    cycle_timeout_seconds: float = 900,
) -> None:
    """Keep one event loop alive for the lifetime of the Seerr HTTP client."""
    if not math.isfinite(cycle_timeout_seconds) or cycle_timeout_seconds <= 0:
        raise ValueError("worker cycle timeout must be finite and positive")
    heartbeat_state = {
        "initialized": cycle is not None,
        "state": "starting",
        "error": None,
        "cycle_deadline_at": None,
    }

    def write_heartbeat(*, success=False):
        if heartbeat is not None:
            heartbeat.write(now=time.time(), **heartbeat_state, success=success)

    async def pulse():
        while should_run():
            write_heartbeat()
            await asyncio.sleep(min(interval, 5))

    capacity_refresh = getattr(cycle, 'capacity_refresh', None)

    async def observe_storage():
        # Read-only collection continues during maintenance/recovery; it never admits
        # downloads. A failed read leaves the last snapshot and timestamp unchanged.
        while should_run():
            try:
                await capacity_refresh()
            except Exception as error:
                LOGGER.warning('storage queue snapshot unavailable (%s)', type(error).__name__)
            await asyncio.sleep(_STORAGE_QUEUE_REFRESH_SECONDS)

    pulse_task = asyncio.create_task(pulse()) if heartbeat is not None else None
    storage_task = asyncio.create_task(observe_storage()) if capacity_refresh is not None else None
    try:
        while should_run():
            blocked = recovery_mode_blocks(recovery_mode_path) or (
                maintenance_path is not None and Path(maintenance_path).exists()
            )
            heartbeat_state["state"] = "maintenance" if blocked else "running"
            heartbeat_state["cycle_deadline_at"] = None
            if cycle is not None and not blocked:
                heartbeat_state["cycle_deadline_at"] = time.time() + cycle_timeout_seconds
                write_heartbeat()
                try:
                    async with asyncio.timeout(cycle_timeout_seconds):
                        await cycle.run_once()
                    heartbeat_state["error"] = None
                    heartbeat_state["cycle_deadline_at"] = None
                    write_heartbeat(success=True)
                except Exception as error:
                    heartbeat_state.update(
                        state="failed", error=type(error).__name__, cycle_deadline_at=None
                    )
                    write_heartbeat()
                    LOGGER.error(
                        "worker cycle failed (%s); admission remains blocked", type(error).__name__
                    )
            else:
                write_heartbeat()
            if should_run():
                await asyncio.sleep(interval)
    finally:
        tasks = [task for task in (pulse_task, storage_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
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
        if cycle is not None and isinstance(
            getattr(cycle, "source_reconciler", None), SourceReconciler
        ):
            await cycle.source_reconciler.client.aclose()
        if cycle is not None and isinstance(
            getattr(cycle, "movie_prioritizer", None), MoviePrioritizer
        ):
            await cycle.movie_prioritizer.client.aclose()
        if cycle is not None and isinstance(
            getattr(cycle, "deletion_coordinator", None), DeletionCoordinator
        ):
            await cycle.deletion_coordinator.client.aclose()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("HOMESERVER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # Request URLs contain provider credentials; do not emit them even in DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    database = Path(os.environ.get("HOMESERVER_DB_PATH", "/var/lib/homeserver/control.sqlite"))
    recovery_mode_path = os.environ.get(
        "HOMESERVER_RECOVERY_MODE", "/var/lib/homeserver/RECOVERY_MODE"
    )
    cycle = _build_cycle(database)
    heartbeat = WorkerHeartbeatStore(database)
    if cycle is None:
        heartbeat.write(
            now=time.time(), initialized=False, state="failed", error="ConfigurationError"
        )
        LOGGER.error("worker could not initialize; verify configuration and recovery mode")
        raise SystemExit(2)
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
            heartbeat=heartbeat,
            maintenance_path=Path(os.environ.get("HOMESERVER_RUN_ROOT", "/run/homeserver"))
            / "maintenance",
            cycle_timeout_seconds=float(
                os.environ.get("HOMESERVER_WORKER_CYCLE_TIMEOUT_SECONDS", "900")
            ),
        )
    )


if __name__ == "__main__":
    main()
