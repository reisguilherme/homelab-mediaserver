from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException

from .models import TelemetrySnapshot


def create_app(*, snapshot_provider: Callable[[], dict[str, Any]]) -> FastAPI:
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

    return app


def _snapshot_from_file() -> dict[str, Any]:
    path = Path(os.environ.get("HOMESERVER_TELEMETRY_SNAPSHOT", "/run/homeserver/snapshot.json"))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("telemetry snapshot is unavailable") from error


app = create_app(snapshot_provider=_snapshot_from_file)
