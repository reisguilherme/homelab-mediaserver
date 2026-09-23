from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

import httpx

from homeserver_control.domain.magnet import magnet_infohash

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
        torrent_bytes = payload.get("torrent_bytes")
        magnet = payload.get("magnet_url")
        if isinstance(torrent_bytes, bytes) and torrent_bytes and magnet is None:
            files = {"torrents": ("approved.torrent", torrent_bytes, "application/x-bittorrent")}
        elif torrent_bytes is None and magnet_infohash(magnet) == infohash.lower():
            files = {"urls": (None, magnet)}
        else:
            raise ContractError("verified torrent bytes or permitted magnet are required")
        category = payload.get("category")
        if not isinstance(category, str) or category not in {"sonarr", "radarr"}:
            raise ContractError("unsupported torrent category")
        response = self._request(
            "POST",
            "/api/v2/torrents/add",
            data={
                "savepath": destination,
                "category": category,
                "stopped": "false",
            },
            files=files,
        )
        text = response.text.strip()
        if text.lower() not in {"ok.", "ok", ""}:
            try:
                result = response.json()
            except ValueError as error:
                raise ContractError("qBittorrent add response is incompatible") from error
            if not isinstance(result, dict) or result.get("success_count", 0) < 1:
                raise ContractError("qBittorrent did not accept the torrent")
        # An accepted add can precede the torrent's appearance in the info API.
        deadline = time.monotonic() + 1.0
        for attempt in range(5):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            matches = [
                item for item in self.find_by_infohash(infohash, timeout=remaining)
                if isinstance(item.get("hash"), str)
                and item["hash"].lower() == infohash.lower()
                and item.get("category") == category
                and isinstance(item.get("save_path"), str)
                and item["save_path"].rstrip("/") == destination.rstrip("/")
            ]
            if len(matches) == 1:
                return {"accepted": True, "infohash": infohash.lower()}
            if attempt < 4:
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        raise EffectUncertain("qBittorrent accepted but torrent was not visible")

    def read(self, path: str, params: dict[str, str] | None = None) -> object:
        allowed = {
            "/api/v2/app/webapiVersion",
            "/api/v2/app/version",
            "/api/v2/app/preferences",
            "/api/v2/torrents/categories",
            "/api/v2/torrents/info",
            "/api/v2/torrents/properties",
            "/api/v2/torrents/files",
        }
        if path not in allowed:
            raise ContractError("qBittorrent path is not allowlisted")
        self._ensure_login()
        response = self._request("GET", path, params=params)
        if path in {"/api/v2/app/webapiVersion", "/api/v2/app/version"}:
            return response.text.strip()
        try:
            return response.json()
        except ValueError as error:
            raise ContractError("qBittorrent JSON response is incompatible") from error

    def find_by_infohash(
        self, infohash: str, *, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        self._ensure_login()
        options = {"timeout": timeout} if timeout is not None else {}
        response = self._request(
            "GET", "/api/v2/torrents/info", params={"hashes": infohash.lower()}, **options
        )
        try:
            payload = response.json()
        except ValueError as error:
            raise ContractError("qBittorrent info response is not JSON") from error
        if not isinstance(payload, list) or any(not isinstance(item, Mapping) for item in payload):
            raise ContractError("qBittorrent info response is incompatible")
        return [dict(item) for item in payload]

    def set_running(self, infohash: str, *, running: bool) -> None:
        if not isinstance(infohash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
            raise ValueError("invalid infohash")
        if not isinstance(running, bool):
            raise ValueError("invalid torrent state")
        self._ensure_login()
        action = "start" if running else "stop"
        response = self._request(
            "POST", f"/api/v2/torrents/{action}",
            data={"hashes": infohash.lower()},
        )
        if response.text.strip().lower() not in {"", "ok", "ok."}:
            raise ContractError("qBittorrent queue response is incompatible")
