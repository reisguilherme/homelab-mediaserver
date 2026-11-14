"""Authenticated same-origin facade; internal control tokens never reach the browser."""

from __future__ import annotations

import secrets
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field


class ReplacementRequest(BaseModel):
    permit_id: str = Field(min_length=1, max_length=100)


def public_path(request: Request) -> str:
    """Only the fixed router prefix may become HTML or JavaScript content."""
    return "/status" if request.headers.get("X-HomeServer-Path") == "/status" else ""


class DownloadsProxy:
    def __init__(
        self,
        *,
        username: str,
        password: str,
        admin_token: str,
        csrf_token: str,
        api_url: str = "http://control-api:8080",
        transport=None,
    ) -> None:
        self.username = username
        self.password = password
        self.admin_token = admin_token
        self.csrf_token = csrf_token
        self.api_url = api_url.rstrip("/")
        self.transport = transport

    def authenticate(self, credentials: HTTPBasicCredentials) -> None:
        if not all((self.username, self.password, self.admin_token, self.csrf_token)):
            raise HTTPException(503, "download controls not configured")
        username_ok = secrets.compare_digest(credentials.username.encode(), self.username.encode())
        password_ok = secrets.compare_digest(credentials.password.encode(), self.password.encode())
        if not (username_ok and password_ok):
            raise HTTPException(401, "invalid credential", headers={"WWW-Authenticate": "Basic"})

    async def forward(self, method: str, path: str, payload: dict | None = None):
        headers = {"X-Admin-Token": self.admin_token}
        if method == "POST":
            headers["X-CSRF-Token"] = self.csrf_token
        try:
            async with httpx.AsyncClient(timeout=25, transport=self.transport) as client:
                response = await client.request(
                    method,
                    self.api_url + path,
                    headers=headers,
                    json=payload,
                )
            if response.status_code not in {200, 202}:
                # Do not relay arbitrary upstream bodies, URLs or authentication errors.
                code = response.status_code if response.status_code in {409, 422, 503} else 503
                raise HTTPException(code, "download action unavailable or source changed")
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("invalid control response")
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, "download controls unavailable") from error
        return JSONResponse(
            value, status_code=response.status_code, headers={"Cache-Control": "no-store"}
        )


def downloads_router(proxy: DownloadsProxy) -> APIRouter:
    router = APIRouter()
    basic = HTTPBasic()
    basic_credentials = Depends(basic)

    def authenticate(credentials: HTTPBasicCredentials = basic_credentials) -> None:
        proxy.authenticate(credentials)

    def same_origin(request: Request) -> None:
        if (
            request.headers.get("X-HomeServer-Action") != "replace"
            or request.headers.get("Content-Type", "").split(";", 1)[0] != "application/json"
            or (
                request.headers.get("Origin") is not None
                and request.headers["Origin"].rstrip("/") != str(request.base_url).rstrip("/")
            )
        ):
            raise HTTPException(403, "same-origin JSON action required")

    @router.get("/ui/downloads", dependencies=[Depends(authenticate)])
    def page(request: Request):
        template = Path(__file__).parent / "templates" / "downloads.html"
        return HTMLResponse(
            template.read_text(encoding="utf-8").replace("{{URL_BASE}}", public_path(request)),
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'"
                ),
            },
        )

    @router.get("/api/v1/downloads", dependencies=[Depends(authenticate)])
    async def downloads():
        return await proxy.forward("GET", "/api/v1/downloads")

    @router.post(
        "/api/v1/downloads/{infohash}/replace",
        dependencies=[Depends(authenticate), Depends(same_origin)],
    )
    async def replace(infohash: str, payload: ReplacementRequest):
        return await proxy.forward(
            "POST",
            f"/api/v1/downloads/{infohash}/replace",
            payload.model_dump(),
        )

    return router
