"""Unprivileged subtitle presentation relay with a fixed native Jellyfin upstream."""

import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from homeserver_control.api.jellyfin_labels import (
    JellyfinLabelProxy,
    register_jellyfin_label_routes,
)


def create_relay_app(
    base_url: str, *, client: httpx.AsyncClient | None = None,
    enabled: bool = True, english_us_label: str = 'English - US',
    portuguese_br_label: str = 'Português BR',
) -> FastAPI:
    proxy = JellyfinLabelProxy(
        base_url, client=client, enabled=enabled, english_us_label=english_us_label,
        portuguese_br_label=portuguese_br_label,
    )

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await proxy.aclose()

    app = FastAPI(title='HomeServer Jellyfin presentation relay', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    register_jellyfin_label_routes(app, proxy)

    @app.get('/health/live', include_in_schema=False)
    def health_live() -> dict[str, str]:
        return {'status': 'ok'}

    return app


def create_app() -> FastAPI:
    """Uvicorn factory; reads only the four nonsecret role-specific settings."""
    return create_relay_app(
        os.environ.get('HOMESERVER_JELLYFIN_URL', 'http://jellyfin:8096/jellyfin'),
        enabled=os.environ.get(
            'HOMESERVER_JELLYFIN_SUBTITLE_LABELS_ENABLED', 'true',
        ).lower() == 'true',
        english_us_label=os.environ.get('HOMESERVER_JELLYFIN_SUBTITLE_LABEL_EN_US', 'English - US'),
        portuguese_br_label=os.environ.get(
            'HOMESERVER_JELLYFIN_SUBTITLE_LABEL_PT_BR', 'Português BR',
        ),
    )
