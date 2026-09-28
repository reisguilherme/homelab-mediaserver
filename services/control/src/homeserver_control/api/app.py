from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from homeserver_common.filesystem import filesystem_identity
from homeserver_control.api.deletion_capture import DeletionAdmission, DeletionCaptureError
from homeserver_control.domain.deletion_plan import DeletionPlanError, DeletionPlanner
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.persistence.heartbeat import WorkerHeartbeatStore
from homeserver_control.recovery import recovery_mode_blocks

CapacityProvider = Callable[[], dict[str, Any]]
TelemetryProvider = Callable[[], dict[str, Any]]


def _default_capacity() -> dict[str, Any]:
    return {
        "filesystem_id": None,
        "total_bytes": 0,
        "free_bytes": 0,
        "reserved_unallocated_bytes": 0,
        "admissible_bytes": 0,
        "measured_at": None,
    }


def _capacity_from_snapshot(path: Path, *, max_age_seconds: float = 30) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return _default_capacity()
        measured = payload.get("measured_at")
        total = payload.get("total_bytes")
        free = payload.get("free_bytes")
        if (
            not isinstance(payload.get("filesystem_id"), str)
            or not payload["filesystem_id"]
            or type(total) is not int
            or total <= 0
            or type(free) is not int
            or free < 0
            or free > total
            or type(measured) not in (int, float)
            or not 0 <= time.time() - measured <= max_age_seconds
        ):
            return _default_capacity()
        return payload
    except (OSError, ValueError, TypeError):
        return _default_capacity()


def _host_status_from_environment() -> dict[str, Any]:
    path = Path(os.environ.get("HOMESERVER_HOST_SNAPSHOT", "/run/homeserver/host.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("host"), dict):
        raise ValueError("invalid host snapshot")
    return {
        key: payload.get(key)
        for key in ("schema_version", "generated_at", "host", "network", "storage")
    }


@dataclass
class ControlState:
    """Mutable process state shared by the API and the worker.

    The production worker is responsible for replacing queue/capacity values
    from SQLite and host collectors.  Keeping the HTTP boundary explicit makes
    the API testable without silently inventing a filesystem or credentials.
    """

    media_roots: tuple[str | Path, ...] = ()
    admin_token: str = "unconfigured"
    csrf_token: str = "unconfigured"
    collector_token: str = "unconfigured"
    db_path: str | Path | None = None
    recovery_mode_path: str | Path | None = None
    capacity_provider: CapacityProvider = _default_capacity
    telemetry_provider: TelemetryProvider | None = None
    media_catalog: dict[str, tuple[str | Path, ...]] = field(default_factory=dict)
    queue: list[dict[str, Any]] = field(default_factory=list)
    admission_enabled: bool = True
    planner: DeletionPlanner | None = field(init=False, default=None)
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    repository: ReservationRepository | None = field(init=False, default=None)
    deletion_admission: DeletionAdmission | None = None
    worker_health_required: bool = False
    worker_max_age_seconds: float = 90
    maintenance_path: str | Path | None = None
    expected_filesystem_id: str | None = None
    capacity_max_age_seconds: float = 30

    def __post_init__(self) -> None:
        if self.media_roots:
            self.planner = DeletionPlanner(roots=self.media_roots)
        if self.db_path is not None:
            self.repository = ReservationRepository(self.db_path)
            self.repository.initialize()


class DeletionPreviewRequest(BaseModel):
    media_key: str = Field(min_length=1, max_length=300)
    paths: list[str] | None = Field(default=None, max_length=256)


class DeletionConfirmRequest(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(min_length=1, max_length=128)


def _configured(value: str) -> bool:
    return bool(value) and value != "unconfigured"


def create_app(*, state: ControlState | None = None) -> FastAPI:
    if state is None:
        roots = tuple(
            item for item in os.environ.get("HOMESERVER_MEDIA_ROOTS", "").split(":") if item
        )
        state = ControlState(
            media_roots=roots,
            admin_token=os.environ.get("HOMESERVER_ADMIN_TOKEN", "unconfigured"),
            csrf_token=os.environ.get("HOMESERVER_CSRF_TOKEN", "unconfigured"),
            db_path=os.environ.get("HOMESERVER_DB_PATH"),
            recovery_mode_path=os.environ.get(
                "HOMESERVER_RECOVERY_MODE", "/var/lib/homeserver/RECOVERY_MODE"
            ),
            worker_health_required=True,
            expected_filesystem_id=os.environ.get("HOMESERVER_MEDIA_UUID") or None,
            capacity_max_age_seconds=float(
                os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS", "30")
            ),
            worker_max_age_seconds=float(
                os.environ.get("HOMESERVER_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "90")
            ),
            maintenance_path=Path(os.environ.get("HOMESERVER_RUN_ROOT", "/run/homeserver"))
            / "maintenance",
            capacity_provider=lambda: _capacity_from_snapshot(
                Path(
                    os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT", "/run/homeserver/capacity.json")
                ),
                max_age_seconds=float(
                    os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS", "30")
                ),
            ),
            telemetry_provider=lambda: _host_status_from_environment(),
        )
        media_uuid = filesystem_identity(
            Path(os.environ.get("HOMESERVER_MEDIA_ROOT", "/data")),
            os.environ.get("HOMESERVER_MEDIA_UUID"),
        )
        if media_uuid and state.db_path is not None:
            deletion_jobs = DeletionJobStore(state.db_path)
            deletion_jobs.initialize()
            state.deletion_admission = DeletionAdmission(
                jobs=deletion_jobs,
                media_root="/data/media",
                snapshot_path=os.environ.get(
                    "HOMESERVER_CAPACITY_SNAPSHOT", "/run/homeserver/capacity.json"
                ),
                filesystem_id=media_uuid,
                jellyfin_url=os.environ.get("HOMESERVER_JELLYFIN_URL", "http://jellyfin:8096"),
                radarr_url=os.environ.get("HOMESERVER_RADARR_URL", ""),
                radarr_api_key=os.environ.get("HOMESERVER_RADARR_API_KEY", ""),
                sonarr_url=os.environ.get("HOMESERVER_SONARR_URL", ""),
                sonarr_api_key=os.environ.get("HOMESERVER_SONARR_API_KEY", ""),
                http_timeout_seconds=float(os.environ.get("HOMESERVER_HTTP_TIMEOUT_SECONDS", "15")),
            )

    app = FastAPI(title="HomeServer control API", version="1")

    def worker_is_ready() -> bool:
        if not state.worker_health_required:
            return True
        if state.db_path is None:
            return False
        try:
            return WorkerHeartbeatStore(state.db_path).ready(
                now=time.time(), max_age=state.worker_max_age_seconds
            )
        except (OSError, ValueError, sqlite3.Error):
            return False

    def admission_enabled() -> bool:
        return (
            state.admission_enabled
            and not recovery_mode_blocks(state.recovery_mode_path)
            and not (state.maintenance_path is not None and Path(state.maintenance_path).exists())
            and worker_is_ready()
        )

    def require_admin(
        x_admin_token: str | None,
        *,
        csrf: str | None = None,
        mutation: bool = False,
    ) -> None:
        if not _configured(state.admin_token) or x_admin_token != state.admin_token:
            raise HTTPException(status_code=401, detail="invalid admin credential")
        if mutation and csrf != state.csrf_token:
            raise HTTPException(status_code=403, detail="csrf token required")
        if mutation and not admission_enabled():
            raise HTTPException(status_code=503, detail="admission blocked by recovery mode")

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", response_model=None)
    def health_ready() -> Response:
        try:
            capacity = state.capacity_provider()
        except Exception:
            capacity = {}
        capacity_ready = (
            isinstance(capacity, dict)
            and isinstance(capacity.get("filesystem_id"), str)
            and bool(capacity["filesystem_id"])
            and type(capacity.get("total_bytes")) is int
            and capacity["total_bytes"] > 0
            and type(capacity.get("free_bytes")) is int
            and capacity["free_bytes"] >= 0
            and type(capacity.get("measured_at")) in (int, float)
            and capacity["free_bytes"] <= capacity["total_bytes"]
            and (
                state.expected_filesystem_id is None
                or capacity["filesystem_id"] == state.expected_filesystem_id
            )
            and 0 <= time.time() - capacity["measured_at"] <= state.capacity_max_age_seconds
        )
        worker_ready = worker_is_ready()
        ready = (
            admission_enabled()
            and _configured(state.admin_token)
            and _configured(state.csrf_token)
            and bool(state.media_roots)
            and state.planner is not None
            and state.repository is not None
            and capacity_ready
            and worker_ready
        )
        if not ready:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "blocked", "admission_enabled": admission_enabled()},
            )
        return {"status": "ready", "admission_enabled": True}

    @app.get("/api/v1/queue")
    def queue(
        x_admin_token: str | None = Header(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        require_admin(x_admin_token)
        return {"items": state.queue[:limit], "next_cursor": None}

    @app.delete("/Items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    @app.delete("/{prefix:path}/Items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def jellyfin_delete(
        item_id: str,
        prefix: str = "",
        authorization: str | None = Header(default=None),
        x_emby_token: str | None = Header(default=None),
        x_emby_authorization: str | None = Header(default=None),
    ) -> Response:
        if prefix and any(part in {"", ".", ".."} for part in prefix.split("/")):
            raise HTTPException(status_code=404, detail="invalid Jellyfin base path")
        if not admission_enabled() or state.deletion_admission is None:
            raise HTTPException(status_code=503, detail="coordinated deletion unavailable")
        auth = authorization or x_emby_authorization or ""
        match = re.search(r'(?:^|,)\s*Token="([^"]+)"', auth)
        user_token = x_emby_token or (match.group(1) if match else "")
        try:
            await state.deletion_admission.capture(item_id, user_token)
        except DeletionCaptureError as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/api/v1/deletions/jobs")
    def deletion_jobs_status(
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_admin(x_admin_token)
        if state.deletion_admission is None:
            raise HTTPException(status_code=503, detail="coordinated deletion unavailable")
        return {
            "items": [
                {
                    "item_id": job["item_id"],
                    "item_type": job["item_type"],
                    "stage": job["stage"],
                    "error": job["error"],
                    "updated_at": job["updated_at"],
                }
                for job in state.deletion_admission.jobs.list()
            ]
        }

    @app.get("/ui/queue", response_class=HTMLResponse)
    def queue_page(x_admin_token: str | None = Header(default=None)) -> Response:
        require_admin(x_admin_token)
        template = Path(__file__).parent / "templates" / "queue.html"
        return HTMLResponse(template.read_text(encoding="utf-8"))

    @app.get("/ui/deletion", response_class=HTMLResponse)
    def deletion_page(x_admin_token: str | None = Header(default=None)) -> Response:
        require_admin(x_admin_token)
        template = Path(__file__).parent / "templates" / "deletion.html"
        return HTMLResponse(template.read_text(encoding="utf-8"))

    @app.get("/api/v1/capacity")
    def capacity(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_admin(x_admin_token)
        try:
            return state.capacity_provider()
        except Exception as error:
            raise HTTPException(status_code=503, detail="capacity unavailable") from error

    @app.post("/api/v1/deletions/preview")
    def deletion_preview(
        payload: DeletionPreviewRequest,
        x_admin_token: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_admin(x_admin_token, csrf=x_csrf_token, mutation=True)
        if state.planner is None:
            raise HTTPException(status_code=503, detail="deletion roots are not configured")
        trusted_paths = state.media_catalog.get(payload.media_key)
        if trusted_paths is None:
            raise HTTPException(
                status_code=404, detail="media object is not in the trusted catalog"
            )
        try:
            preview = state.planner.preview(payload.media_key, trusted_paths)
        except DeletionPlanError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {
            "media_key": preview.media_key,
            "paths": [str(path) for path in preview.paths],
            "bytes_estimated": preview.bytes_estimated,
            "version": preview.version,
            "token": preview.token,
            "expires_at": preview.expires_at,
        }

    @app.post("/api/v1/deletions", status_code=status.HTTP_202_ACCEPTED)
    def confirm_deletion(
        payload: DeletionConfirmRequest,
        x_admin_token: str | None = Header(default=None),
        x_csrf_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_admin(x_admin_token, csrf=x_csrf_token, mutation=True)
        if state.planner is None:
            raise HTTPException(status_code=503, detail="deletion roots are not configured")
        try:
            confirmation = state.planner.confirm(
                payload.token, payload.version, payload.operation_id
            )
        except DeletionPlanError as error:
            detail = str(error)
            code = 409 if "changed" in detail or "expired" in detail else 422
            raise HTTPException(status_code=code, detail=detail) from error
        operation = state.operations.setdefault(
            confirmation.operation_id,
            {
                "operation_id": confirmation.operation_id,
                "kind": "delete",
                "state": "authorized",
                "media_key": confirmation.media_key,
                "paths": [str(path) for path in confirmation.paths],
                "bytes_estimated": confirmation.bytes_estimated,
            },
        )
        if state.repository is not None:
            operation = state.repository.record_operation(
                operation_id=confirmation.operation_id,
                idempotency_key=f"delete:{confirmation.operation_id}",
                kind="delete",
                payload={
                    "media_key": confirmation.media_key,
                    "paths": [str(path) for path in confirmation.paths],
                    "bytes_estimated": confirmation.bytes_estimated,
                },
                state="authorized",
            )
            state.operations[confirmation.operation_id] = operation
        return operation

    @app.get("/api/v1/operations/{operation_id}")
    def operation(
        operation_id: str,
        x_admin_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_admin(x_admin_token)
        found = state.operations.get(operation_id)
        if found is None and state.repository is not None:
            found = state.repository.get_operation(operation_id)
        if found is None:
            raise HTTPException(status_code=404, detail="operation not found")
        return found

    @app.get("/api/v1/telemetry")
    def telemetry(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_admin(x_admin_token)
        if state.telemetry_provider is None:
            raise HTTPException(status_code=503, detail="telemetry unavailable")
        try:
            return state.telemetry_provider()
        except Exception as error:
            raise HTTPException(status_code=503, detail="telemetry unavailable") from error

    return app


app = create_app()
