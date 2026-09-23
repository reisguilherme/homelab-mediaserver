from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from .models import TelemetrySnapshot
from .status import StatusProvider


def create_app(
    *,
    snapshot_provider: Callable[[], dict[str, Any]],
    status_provider: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    if status_provider is None:
        status_provider = StatusProvider(
            host_path=Path(os.environ.get("HOMESERVER_HOST_SNAPSHOT", "/run/homeserver/host.json")),
            capacity_path=Path(
                os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT", "/run/homeserver/capacity.json")
            ),
            media_root=Path(os.environ.get("HOMESERVER_MEDIA_ROOT", "/data")),
        )
    app = FastAPI(title="HomeServer telemetry", version="1")

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/telemetry")
    def telemetry() -> dict[str, Any]:
        try:
            snapshot = TelemetrySnapshot.model_validate(snapshot_provider())
        except Exception as error:
            raise HTTPException(status_code=503, detail="telemetry unavailable") from error
        return snapshot.model_dump(mode="json")

    @app.get("/api/v1/status")
    def status() -> JSONResponse:
        try:
            payload = status_provider()
        except Exception as error:
            raise HTTPException(status_code=503, detail="status unavailable") from error
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/ui/status", response_class=HTMLResponse)
    def status_page() -> HTMLResponse:
        template = Path(__file__).parent / "templates" / "status.html"
        return HTMLResponse(
            template.read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "connect-src 'self'; base-uri 'none'; form-action 'none'"
                ),
            },
        )

    return app


def _snapshot_from_file() -> dict[str, Any]:
    path = Path(os.environ.get("HOMESERVER_TELEMETRY_SNAPSHOT", "/run/homeserver/snapshot.json"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("telemetry snapshot is unavailable") from error


app = create_app(snapshot_provider=_snapshot_from_file)
