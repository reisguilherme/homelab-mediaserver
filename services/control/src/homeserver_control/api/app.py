from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from homeserver_control.domain.deletion_plan import DeletionPlanError, DeletionPlanner
from homeserver_control.persistence.db import ReservationRepository
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


def _capacity_from_snapshot(path: Path) -> dict[str, Any]:
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
            or not 0 <= time.time() - measured <= 30
        ):
            return _default_capacity()
        return payload
    except (OSError, ValueError, TypeError):
        return _default_capacity()


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
    acknowledged_event_id: int = 0
    seed_limit: dict[str, Any] = field(default_factory=dict)
    repository: ReservationRepository | None = field(init=False, default=None)

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


class EventAckRequest(BaseModel):
    sequence: int = Field(ge=0)


class SeedLimitRequest(BaseModel):
    bytes_per_second: int = Field(ge=0, le=2_500_000)
    reason: str = Field(min_length=1, max_length=100)


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
            collector_token=os.environ.get("HOMESERVER_COLLECTOR_TOKEN", "unconfigured"),
            db_path=os.environ.get("HOMESERVER_DB_PATH"),
            recovery_mode_path=os.environ.get(
                "HOMESERVER_RECOVERY_MODE", "/var/lib/homeserver/RECOVERY_MODE"
            ),
            capacity_provider=lambda: _capacity_from_snapshot(
                Path(
                    os.environ.get(
                        "HOMESERVER_CAPACITY_SNAPSHOT", "/run/homeserver/capacity.json"
                    )
                )
            ),
        )

    app = FastAPI(title="HomeServer control API", version="1")

    def admission_enabled() -> bool:
        return state.admission_enabled and not recovery_mode_blocks(state.recovery_mode_path)

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

    def require_collector(x_collector_token: str | None, *, mutation: bool = False) -> None:
        if not _configured(state.collector_token) or x_collector_token != state.collector_token:
            raise HTTPException(status_code=401, detail="invalid collector credential")
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
            and 0 <= time.time() - capacity["measured_at"] <= 30
        )
        ready = (
            admission_enabled()
            and _configured(state.admin_token)
            and _configured(state.collector_token)
            and bool(state.media_roots)
            and state.planner is not None
            and state.repository is not None
            and capacity_ready
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

    @app.get("/internal/v1/events")
    def events(
        x_collector_token: str | None = Header(default=None),
        after_id: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> dict[str, Any]:
        require_collector(x_collector_token)
        selected = [event for event in state.events if int(event.get("id", 0)) > after_id]
        return {"events": selected[:limit], "next_id": after_id + len(selected[:limit])}

    @app.post("/internal/v1/events/ack")
    def events_ack(
        payload: EventAckRequest,
        x_collector_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_collector(x_collector_token, mutation=True)
        if payload.sequence < state.acknowledged_event_id:
            raise HTTPException(status_code=409, detail="acknowledgement moved backwards")
        state.acknowledged_event_id = payload.sequence
        return {"acknowledged": state.acknowledged_event_id}

    @app.post("/internal/v1/seed-limit")
    def seed_limit(
        payload: SeedLimitRequest,
        x_collector_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_collector(x_collector_token, mutation=True)
        state.seed_limit = payload.model_dump()
        return state.seed_limit

    return app


app = create_app()
