from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .status import StatusProvider

_TAILSCALE_HOSTNAME_PATTERN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){2,}ts\.net")


def _service_hostname() -> str | None:
    hostname = os.environ.get("HOMESERVER_TAILSCALE_HOSTNAME", "").strip().rstrip(".").lower()
    if len(hostname) <= 253 and _TAILSCALE_HOSTNAME_PATTERN.fullmatch(hostname):
        return hostname
    return None


def create_app(
    *,
    snapshot_provider: Callable[[], dict[str, Any]] | None = None,
    status_provider: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    if status_provider is None:
        status_provider = StatusProvider(
            host_path=Path(os.environ.get("HOMESERVER_HOST_SNAPSHOT", "/run/homeserver/host.json")),
            capacity_path=Path(
                os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT", "/run/homeserver/capacity.json")
            ),
            media_root=Path(os.environ.get("HOMESERVER_MEDIA_ROOT", "/data")),
            expected_filesystem_id=os.environ.get("HOMESERVER_MEDIA_UUID") or None,
            host_max_age_seconds=float(os.environ.get("HOMESERVER_METRICS_MAX_AGE_SECONDS", "90")),
            capacity_max_age_seconds=float(
                os.environ.get("HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS", "30")
            ),
        )
    app = FastAPI(title="HomeServer telemetry", version="1")

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/telemetry")
    @app.get("/api/v1/status")
    def status() -> JSONResponse:
        try:
            payload = status_provider()
        except Exception as error:
            raise HTTPException(status_code=503, detail="status unavailable") from error
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/health/ready")
    def ready():
        try:
            payload = status_provider()
            available = payload["host"]["state"] == "ok" and payload["capacity"]["state"] == "ok"
        except Exception:
            available = False
        return JSONResponse(
            {"status": "ready" if available else "unavailable"},
            status_code=200 if available else 503,
        )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/ui/status", response_class=HTMLResponse)
    def status_page(request: Request) -> HTMLResponse:
        template = Path(__file__).parent / "templates" / "status.html"
        page = template.read_text(encoding="utf-8")
        hostname = _service_hostname() or request.url.hostname
        if hostname and ":" in hostname:
            hostname = f"[{hostname}]"
        page = page.replace(
            "{{SERVICE_ACCESS_STATE}}",
            "Painéis dos serviços",
        )
        for name, key, port in (
            ("JELLYFIN", "JELLYFIN", 8096),
            ("SEERR", "SEERR", 5055),
            ("SONARR", "SONARR", 8989),
            ("RADARR", "RADARR", 7878),
            ("QBITTORRENT", "QBIT_MONITOR", 18080),
            ("PROWLARR", "PROWLARR", 9696),
            ("BAZARR", "BAZARR", 6767),
        ):
            from html import escape
            from urllib.parse import urlsplit

            url = os.environ.get(f"HOMESERVER_{key}_PUBLIC_URL", "")
            parsed = urlsplit(url)
            if not (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username):
                url = (
                    f"http://{hostname}:{os.environ.get(f'HOMESERVER_{key}_PORT', str(port))}/"
                    if hostname
                    else ""
                )
            attributes = (
                f'href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer"'
                if url
                else 'aria-disabled="true"'
            )
            page = page.replace(f"{{{{SERVICE_{name}_LINK}}}}", attributes)
        return HTMLResponse(
            page,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "connect-src 'self'; base-uri 'none'; form-action 'none'"
                ),
            },
        )

    return app


app = create_app()
