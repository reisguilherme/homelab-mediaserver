from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from .http import ContractError, CredentialError, EffectUncertain, UpstreamError, endpoint


class QBittorrentAdapter:
    """Fixed-subset qBittorrent client used behind the admission gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not username or not password:
            raise ValueError("qBittorrent credentials are required")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = client or httpx.Client(timeout=httpx.Timeout(15.0))
        self._logged_in = False

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, endpoint(self.base_url, path), **kwargs)
        except httpx.TimeoutException as error:
            raise EffectUncertain("qBittorrent request timed out") from error
        except httpx.HTTPError as error:
            raise UpstreamError("qBittorrent request failed") from error
        if response.status_code in (401, 403):
            raise CredentialError("qBittorrent credential rejected")
        if response.status_code >= 400:
            raise UpstreamError(f"qBittorrent returned HTTP {response.status_code}")
        return response

    def _login(self) -> None:
        response = self._request(
            "POST",
            "/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
        )
        if response.text.strip().lower() not in {"ok.", "ok"}:
            raise ContractError("qBittorrent login response is incompatible")
        self._logged_in = True

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self._login()

    def add_torrent(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_login()
        infohash = payload.get("infohash")
        destination = payload.get("savepath")
        if not isinstance(infohash, str) or not isinstance(destination, str):
            raise ContractError("qBittorrent payload identity is incomplete")
        if not destination.startswith("/data/"):
            raise ContractError("qBittorrent destination is outside /data")
        urls = payload.get("torrent_url") or payload.get("urls")
        if not isinstance(urls, str) or not urls.startswith(("https://", "http://")):
            raise ContractError("verified torrent URL is required")
        response = self._request(
            "POST",
            "/api/v2/torrents/add",
            data={
                "urls": urls,
                "savepath": destination,
                "category": payload.get("category", "homeserver"),
            },
        )
        if response.text.strip().lower() not in {"ok.", "ok", ""}:
            raise ContractError("qBittorrent add response is incompatible")
        return {"accepted": True, "infohash": infohash.lower()}

    def find_by_infohash(self, infohash: str) -> list[dict[str, Any]]:
        self._ensure_login()
        response = self._request(
            "GET", "/api/v2/torrents/info", params={"hashes": infohash.lower()}
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise ContractError("qBittorrent info response is not JSON") from error
        if not isinstance(payload, list) or any(not isinstance(item, Mapping) for item in payload):
            raise ContractError("qBittorrent info response is incompatible")
        return [dict(item) for item in payload]
