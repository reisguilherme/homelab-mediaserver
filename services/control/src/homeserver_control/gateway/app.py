"""A deliberately small qBittorrent API surface for the pinned Arr clients."""

from __future__ import annotations

import os
import re
import secrets
import time
from hashlib import sha256
from threading import Lock
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse

from homeserver_control.adapters.http import ContractError, EffectUncertain
from homeserver_control.adapters.qbittorrent import QBittorrentAdapter
from homeserver_control.domain.magnet import magnet_infohash
from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.recovery import recovery_mode_blocks

from .allowlist import GatewayAllowlist
from .auth import token_matches
from .permits import Permit, PermitRegistry


class QbitClient(Protocol):
    def add_torrent(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def read(self, path: str, params: dict[str, str] | None = None) -> object: ...

    def set_running(self, infohash: str, *, running: bool) -> None: ...


class UnconfiguredQbitClient:
    def add_torrent(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("qBittorrent upstream is not configured")

    def read(self, path: str, params: dict[str, str] | None = None) -> object:
        raise RuntimeError("qBittorrent upstream is not configured")

    def set_running(self, infohash: str, *, running: bool) -> None:
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
    torrent_store: TorrentArtifactStore | None = None,
    recovery_mode_path: str | os.PathLike[str] | None = None,
) -> FastAPI:
    app = FastAPI(title="HomeServer download gateway", version="1")
    routes = allowlist or GatewayAllowlist()
    sessions: dict[str, float] = {}
    session_lock = Lock()

    def authenticate(request: Request, header_token: str | None) -> bool:
        """Return whether the caller used the internal header instead of Arr's SID."""
        if arr_token == "unconfigured":
            raise HTTPException(status_code=403, detail="gateway credential is not configured")
        if token_matches(header_token, arr_token):
            return True
        sid = request.cookies.get("SID")
        with session_lock:
            if sid and sessions.get(sid, 0) > time.monotonic():
                return False
        raise HTTPException(status_code=403, detail="gateway login required")

    def require_admission() -> None:
        if recovery_mode_blocks(recovery_mode_path):
            raise HTTPException(status_code=503, detail="admission blocked by recovery mode")

    @app.post("/internal/series-queue-state")
    async def series_queue_state(
        request: Request, x_arr_token: str | None = Header(default=None)
    ) -> dict[str, str]:
        if arr_token == "unconfigured" or not token_matches(x_arr_token, arr_token):
            raise HTTPException(status_code=403, detail="worker credential required")
        try:
            body = await request.json()
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid JSON body") from error
        if not isinstance(body, dict) or set(body) != {"permit_token", "action"}:
            raise HTTPException(status_code=422, detail="permit and action required")
        action = body["action"]
        if action not in {"start", "stop"}:
            raise HTTPException(status_code=422, detail="unsupported queue action")
        token = body["permit_token"]
        permit = permits.get(token) if isinstance(token, str) else None
        if (
            permit is None or permit.state != "confirmed"
            or permit.category != "sonarr" or permit.reservation_id is None
            or not isinstance(permit.scope_key, str)
            or not re.fullmatch(r"S[0-9]{2,}E[0-9]{2,}", permit.scope_key)
            or not re.fullmatch(r"[0-9a-f]{40}", permit.infohash)
            or permit.destination != "/data/torrents"
        ):
            raise HTTPException(status_code=403, detail="confirmed episode permit required")
        if action == "start":
            require_admission()
        entries = upstream.read("/api/v2/torrents/info", {"hashes": permit.infohash})
        if not isinstance(entries, list):
            raise HTTPException(status_code=502, detail="invalid torrent list")
        matches = [
            entry for entry in entries if isinstance(entry, dict)
            and isinstance(entry.get("hash"), str)
            and entry["hash"].lower() == permit.infohash
        ]
        if len(matches) != 1:
            raise HTTPException(status_code=409, detail="permitted torrent is unavailable")
        current = matches[0]
        if (
            current.get("category") != "sonarr"
            or not isinstance(current.get("save_path"), str)
            or current["save_path"].rstrip("/") != permit.destination
        ):
            raise HTTPException(status_code=409, detail="torrent identity changed")
        progress = current.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool) and progress >= 1:
            return {"state": "complete"}
        state = current.get("state")
        if not isinstance(state, str):
            raise HTTPException(status_code=502, detail="invalid torrent state")
        stopped = state in {"stoppedDL", "stoppedUP", "pausedDL", "pausedUP"}
        if action == "stop" and stopped:
            return {"state": "already_stopped"}
        if action == "start" and not stopped:
            return {"state": "already_started"}
        upstream.set_running(permit.infohash, running=action == "start")
        return {"state": "started" if action == "start" else "stopped"}

    @app.post("/internal/repair-metadata")
    async def repair_metadata(
        request: Request, x_arr_token: str | None = Header(default=None)
    ) -> dict[str, str]:
        if not token_matches(x_arr_token, arr_token) or arr_token == "unconfigured":
            raise HTTPException(status_code=403, detail="worker credential required")
        require_admission()
        if torrent_store is None:
            raise HTTPException(status_code=503, detail="torrent metadata store unavailable")
        try:
            body = await request.json()
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid JSON body") from error
        token = body.get("permit_token") if isinstance(body, dict) else None
        permit = permits.get(token) if isinstance(token, str) else None
        if permit is None or permit.state != "confirmed":
            raise HTTPException(status_code=403, detail="confirmed permit required")
        metadata = torrent_store.get(permit)
        if metadata is None:
            raise HTTPException(status_code=409, detail="verified metadata unavailable")

        def torrent_info() -> dict[str, Any] | None:
            entries = upstream.read(
                "/api/v2/torrents/info", {"hashes": permit.infohash}
            )
            if not isinstance(entries, list):
                raise HTTPException(status_code=502, detail="invalid torrent list")
            return next(
                (entry for entry in entries if isinstance(entry, dict)
                 and entry.get("hash", "").lower() == permit.infohash), None
            )

        current = torrent_info()
        if (
            current is None or not isinstance(current.get("total_size"), int)
            or current["total_size"] > 0 or current.get("downloaded") != 0
            or current.get("progress") != 0
        ):
            raise HTTPException(status_code=409, detail="torrent is not metadata-stalled")
        try:
            upstream.add_torrent({
                "infohash": permit.infohash, "savepath": permit.destination,
                "category": permit.category, "torrent_bytes": metadata,
            })
        except (ContractError, EffectUncertain):
            # qBittorrent can apply metadata to an existing magnet while
            # answering "Fails." for the duplicate add. Inspect the state.
            pass
        updated = torrent_info()
        if (
            updated is None or not isinstance(updated.get("total_size"), int)
            or updated["total_size"] <= 0
        ):
            raise HTTPException(status_code=409, detail="metadata remains unavailable")
        return {"state": "metadata_available"}

    @app.get("/internal/queue-capacity")
    def queue_capacity(x_arr_token: str | None = Header(default=None)) -> list[dict[str, object]]:
        if arr_token == "unconfigured" or not token_matches(x_arr_token, arr_token):
            raise HTTPException(status_code=403, detail="worker credential required")
        result = upstream.read("/api/v2/torrents/info")
        if not isinstance(result, list):
            raise HTTPException(status_code=502, detail="invalid torrent list")
        if any(not isinstance(entry, dict) for entry in result):
            raise HTTPException(status_code=502, detail="invalid torrent entry")
        return [
            {
                "hash": entry.get("hash"),
                "total_size": entry.get("total_size"),
                "amount_left": entry.get("amount_left"),
                "state": entry.get("state"),
                "admitted": permits.is_admitted(entry["hash"])
                if isinstance(entry.get("hash"), str) else False,
            }
            for entry in result
        ]

    @app.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v2/auth/login")
    async def login(request: Request) -> Response:
        if not routes.permits("POST", "/api/v2/auth/login"):
            raise HTTPException(status_code=404, detail="gateway path is not allowed")
        form = await request.form()
        password = form.get("password")
        if (
            arr_token == "unconfigured"
            or form.get("username") != "arr"
            or not isinstance(password, str)
            or not token_matches(password, arr_token)
        ):
            return PlainTextResponse("Fails.", status_code=403)
        sid = secrets.token_urlsafe(32)
        with session_lock:
            if len(sessions) > 100:
                sessions.clear()
            sessions[sid] = time.monotonic() + 3600
        response = PlainTextResponse("Ok.")
        response.set_cookie("SID", sid, httponly=True, samesite="strict", max_age=3600)
        return response

    @app.get("/api/v2/app/{item}")
    def app_read(
        item: str, request: Request, x_arr_token: str | None = Header(default=None)
    ) -> object:
        path = f"/api/v2/app/{item}"
        if not routes.permits("GET", path):
            raise HTTPException(status_code=404, detail="gateway path is not allowed")
        internal = authenticate(request, x_arr_token)
        result = upstream.read(path)
        if item == "preferences":
            if not isinstance(result, dict):
                raise HTTPException(status_code=502, detail="invalid qBittorrent preferences")
            safe = {
                "save_path", "queueing_enabled", "max_ratio_enabled", "max_ratio",
                "max_seeding_time_enabled", "max_seeding_time", "max_ratio_act", "dht",
            }
            return {key: value for key, value in result.items() if key in safe}
        if not isinstance(result, str):
            raise HTTPException(status_code=502, detail="invalid qBittorrent version")
        if internal and item == "version":
            return {"version": result}
        return PlainTextResponse(result)

    @app.get("/api/v2/torrents/{item}")
    def torrent_read(
        item: str, request: Request, x_arr_token: str | None = Header(default=None)
    ) -> object:
        path = f"/api/v2/torrents/{item}"
        if not routes.permits("GET", path):
            raise HTTPException(status_code=404, detail="gateway path is not allowed")
        authenticate(request, x_arr_token)
        params = dict(request.query_params)
        if item == "categories":
            if params:
                raise HTTPException(status_code=400, detail="unexpected query")
            return upstream.read(path)
        if item == "info":
            if set(params) - {"category", "hashes"}:
                raise HTTPException(status_code=400, detail="unexpected query")
            result = upstream.read(path, params)
            if not isinstance(result, list):
                raise HTTPException(status_code=502, detail="invalid torrent list")
            return [
                entry
                for entry in result
                if isinstance(entry, dict)
                and isinstance(entry.get("hash"), str)
                and permits.is_admitted(entry["hash"])
            ]
        if set(params) != {"hash"}:
            raise HTTPException(status_code=400, detail="torrent hash required")
        infohash = _validate_infohash(params["hash"])
        if not permits.is_admitted(infohash):
            raise HTTPException(status_code=403, detail="torrent is not admitted")
        return upstream.read(path, {"hash": infohash})

    @app.post("/api/v2/torrents/add")
    async def add_torrent(
        request: Request,
        x_arr_token: str | None = Header(default=None),
        x_admission_permit: str | None = Header(default=None),
        x_infohash: str | None = Header(default=None),
    ) -> object:
        if not routes.permits("POST", "/api/v2/torrents/add"):
            raise HTTPException(status_code=404, detail="gateway path is not allowed")
        internal = authenticate(request, x_arr_token)
        require_admission()
        permit_token = x_admission_permit
        content_type = request.headers.get("content-type", "")
        total_bytes: int | None = None
        if content_type.startswith("application/json"):
            if not internal or not permit_token:
                raise HTTPException(status_code=403, detail="admission permit required")
            try:
                payload = await request.json()
            except ValueError as error:
                raise HTTPException(status_code=422, detail="invalid JSON body") from error
            if not isinstance(payload, dict):
                raise HTTPException(status_code=422, detail="JSON body must be an object")
            metadata_sha256 = payload.get("metadata_sha256")
            if metadata_sha256 is not None and not isinstance(metadata_sha256, str):
                raise HTTPException(status_code=422, detail="invalid metadata digest")
        elif content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
            if internal and not permit_token:
                raise HTTPException(status_code=403, detail="admission permit required")
            try:
                form = await request.form()
            except (AssertionError, ValueError) as error:
                raise HTTPException(status_code=422, detail="invalid multipart body") from error
            stopped = form.get("stopped", "false")
            if (
                not isinstance(stopped, str)
                or stopped.lower() != "false"
            ):
                raise HTTPException(status_code=422, detail="unsupported torrent state")
            destination = form.get("savepath", "/data/torrents")
            category = form.get("category", "")
            if "urls" in form:
                if set(form) - {"urls", "category", "savepath", "stopped"}:
                    raise HTTPException(status_code=422, detail="unsupported torrent options")
                urls = form.getlist("urls")
                magnet = urls[0] if len(urls) == 1 else None
                infohash = magnet_infohash(magnet)
                if infohash is None or not isinstance(category, str) or not category:
                    raise HTTPException(
                        status_code=422, detail="single v1 magnet and category required"
                    )
                if not isinstance(destination, str):
                    raise HTTPException(status_code=422, detail="invalid destination")
                try:
                    matched = permits.find_for_magnet(
                        infohash=infohash, destination=destination, category=category,
                    )
                except PermissionError as error:
                    raise HTTPException(status_code=403, detail=str(error)) from error
                permit_token = matched.token
                metadata_sha256 = matched.metadata_sha256
                verified = torrent_store.get(matched) if torrent_store is not None else None
                if verified is None and torrent_store is not None and matched.state == "authorized":
                    raise HTTPException(
                        status_code=403, detail="verified torrent metadata required"
                    )
                payload = {
                    "infohash": infohash, "savepath": destination,
                    "category": category,
                    **({"torrent_bytes": verified} if verified is not None
                       else {"magnet_url": magnet}),
                }
                if verified is not None:
                    total_bytes = inspect_torrent(verified).total_bytes
            else:
                if not content_type.startswith("multipart/form-data"):
                    raise HTTPException(status_code=403, detail="verified torrent required")
                if set(form) - {"torrents", "category", "savepath", "stopped"}:
                    raise HTTPException(status_code=422, detail="unsupported torrent options")
                upload = form.get("torrents")
                if (
                    len(form.getlist("torrents")) != 1
                    or not hasattr(upload, "read")
                    or not isinstance(destination, str)
                ):
                    raise HTTPException(
                        status_code=422, detail="torrent file and destination are required"
                    )
                torrent_bytes = await upload.read(16 * 1024 * 1024 + 1)
                if len(torrent_bytes) > 16 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="torrent metadata is too large")
                if internal and x_infohash and permit_token:
                    # Compatibility with the internal token API. The real Arr path
                    # always parses the torrent and resolves a persisted permit.
                    infohash = x_infohash
                    metadata_sha256 = sha256(torrent_bytes).hexdigest()
                else:
                    try:
                        inspected = inspect_torrent(torrent_bytes)
                    except TorrentBytesError as error:
                        raise HTTPException(status_code=422, detail=str(error)) from error
                    infohash = inspected.infohash
                    metadata_sha256 = inspected.metadata_sha256
                    total_bytes = inspected.total_bytes
                    if not isinstance(category, str) or not category:
                        raise HTTPException(status_code=422, detail="category is required")
                    try:
                        matched = permits.find_for_metadata(
                            infohash=infohash,
                            metadata_sha256=metadata_sha256,
                            destination=destination,
                            category=category,
                        )
                    except PermissionError as error:
                        raise HTTPException(status_code=403, detail=str(error)) from error
                    permit_token = matched.token
                payload = {
                    "infohash": infohash,
                    "savepath": destination,
                    "torrent_bytes": torrent_bytes,
                    "category": category,
                }
        else:
            raise HTTPException(status_code=403, detail="verified torrent required")
        infohash = _validate_infohash(payload.get("infohash"))
        destination = payload.get("savepath")
        if not isinstance(destination, str) or not (
            destination == "/data/torrents" or destination.startswith("/data/torrents/")
        ):
            raise HTTPException(status_code=422, detail="invalid destination")
        if not permit_token:
            raise HTTPException(status_code=403, detail="admission permit required")
        if total_bytes is not None:
            permit = permits.get(permit_token)
            if permit is None or permit.budget_bytes is None or total_bytes > permit.budget_bytes:
                raise HTTPException(status_code=403, detail="torrent exceeds reserved budget")

        def dispatch(_permit: Permit) -> dict[str, Any]:
            return upstream.add_torrent({**payload, "infohash": infohash, "savepath": destination})

        try:
            result = permits.authorize(
                token=permit_token,
                infohash=infohash,
                destination=destination,
                metadata_sha256=metadata_sha256,
                effect=dispatch,
            )
            return result if internal else PlainTextResponse("Ok.")
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/v2/torrents/{item}")
    def denied_torrent_mutation(item: str) -> None:
        raise HTTPException(status_code=404, detail="gateway path is not allowed")

    return app


def _configured_upstream() -> QbitClient:
    credentials_file = os.environ.get("HOMESERVER_QBIT_CREDENTIALS_FILE")
    if not credentials_file:
        return UnconfiguredQbitClient()
    credentials: dict[str, str] = {}
    with open(credentials_file, encoding="utf-8") as handle:
        for line in handle:
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.strip().split("=", 1)
                credentials[key] = value
    return QBittorrentAdapter(
        base_url=os.environ.get("HOMESERVER_QBIT_URL", "http://qbittorrent:8080"),
        username=credentials["QBIT_USERNAME"],
        password=credentials["QBIT_PASSWORD"],
    )


_database_path = os.environ.get("HOMESERVER_DB_PATH")
app = create_app(
    permits=PermitRegistry(os.environ.get("HOMESERVER_DB_PATH")),
    upstream=_configured_upstream(),
    torrent_store=TorrentArtifactStore(_database_path) if _database_path else None,
    arr_token=os.environ.get("HOMESERVER_ARR_TOKEN", "unconfigured"),
    recovery_mode_path=os.environ.get(
        "HOMESERVER_RECOVERY_MODE", "/var/lib/homeserver/RECOVERY_MODE"
    ),
)
