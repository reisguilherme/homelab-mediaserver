from __future__ import annotations

import os
import re
from hashlib import sha256
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Request

from homeserver_control.recovery import recovery_mode_blocks

from .allowlist import GatewayAllowlist
from .auth import token_matches
from .permits import Permit, PermitRegistry


class QbitClient(Protocol):
    def add_torrent(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class UnconfiguredQbitClient:
    def add_torrent(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("qBittorrent upstream is not configured")


def _validate_infohash(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise HTTPException(status_code=422, detail="invalid infohash")
    return value.lower()


def create_app(
    *,
    permits: PermitRegistry,
    upstream: QbitClient,
    arr_token: str,
    allowlist: GatewayAllowlist | None = None,
    recovery_mode_path: str | os.PathLike[str] | None = None,
) -> FastAPI:
    app = FastAPI(title="HomeServer download gateway", version="1")
    routes = allowlist or GatewayAllowlist()

    def require_arr_token(value: str | None) -> None:
        if arr_token == "unconfigured" or not token_matches(value, arr_token):
            raise HTTPException(status_code=401, detail="invalid gateway credential")

    def require_admission() -> None:
        if recovery_mode_blocks(recovery_mode_path):
            raise HTTPException(status_code=503, detail="admission blocked by recovery mode")

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v2/app/version")
    def version(x_arr_token: str | None = Header(default=None)) -> dict[str, str]:
        if not routes.permits("GET", "/api/v2/app/version"):
            raise HTTPException(status_code=404, detail="gateway path is not allowed")
        require_arr_token(x_arr_token)
        return {"version": "admission-gateway/1"}

    @app.post("/api/v2/torrents/add")
    async def add_torrent(
        request: Request,
        x_arr_token: str | None = Header(default=None),
        x_admission_permit: str | None = Header(default=None),
        x_infohash: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not routes.permits("POST", "/api/v2/torrents/add"):
            raise HTTPException(status_code=404, detail="gateway path is not allowed")
        require_arr_token(x_arr_token)
        require_admission()
        if not x_admission_permit:
            raise HTTPException(status_code=403, detail="admission permit required")
        payload: dict[str, Any]
        metadata_sha256: str | None = None
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            try:
                body = await request.json()
            except ValueError as error:
                raise HTTPException(status_code=422, detail="invalid JSON body") from error
            if not isinstance(body, dict):
                raise HTTPException(status_code=422, detail="JSON body must be an object")
            payload = body
            metadata_sha256 = payload.get("metadata_sha256")
            if metadata_sha256 is not None and not isinstance(metadata_sha256, str):
                raise HTTPException(status_code=422, detail="invalid metadata digest")
        elif content_type.startswith("multipart/form-data"):
            try:
                form = await request.form()
            except (AssertionError, ValueError) as error:
                raise HTTPException(status_code=422, detail="invalid multipart body") from error
            upload = form.get("torrents")
            destination = form.get("savepath")
            if not hasattr(upload, "read") or not isinstance(destination, str):
                raise HTTPException(
                    status_code=422, detail="torrent file and destination are required"
                )
            if not x_infohash:
                raise HTTPException(status_code=422, detail="verified infohash header is required")
            torrent_bytes = await upload.read(16 * 1024 * 1024 + 1)
            if len(torrent_bytes) > 16 * 1024 * 1024:
                raise HTTPException(status_code=413, detail="torrent metadata is too large")
            payload = {
                "infohash": x_infohash,
                "savepath": destination,
                "torrent_bytes": torrent_bytes,
            }
            metadata_sha256 = sha256(torrent_bytes).hexdigest()
        else:
            raise HTTPException(status_code=422, detail="JSON or torrent multipart body required")
        infohash = _validate_infohash(payload.get("infohash"))
        destination = payload.get("savepath")
        if not isinstance(destination, str) or not destination.startswith("/data/"):
            raise HTTPException(status_code=422, detail="invalid destination")

        def dispatch(permit: Permit) -> dict[str, Any]:
            return upstream.add_torrent({**payload, "infohash": infohash, "savepath": destination})

        try:
            return permits.authorize(
                token=x_admission_permit,
                infohash=infohash,
                destination=destination,
                metadata_sha256=metadata_sha256,
                effect=dispatch,
            )
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


# The production process supplies a real adapter and permit store during
# bootstrap.  This importable default keeps the container entrypoint alive
# while refusing any mutation until credentials and permits are configured.
app = create_app(
    permits=PermitRegistry(os.environ.get("HOMESERVER_DB_PATH")),
    upstream=UnconfiguredQbitClient(),
    arr_token=os.environ.get("HOMESERVER_ARR_TOKEN", "unconfigured"),
    recovery_mode_path=os.environ.get(
        "HOMESERVER_RECOVERY_MODE", "/var/lib/homeserver/RECOVERY_MODE"
    ),
)
