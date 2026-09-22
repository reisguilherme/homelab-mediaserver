from __future__ import annotations

from typing import Any

import httpx

from .http import ContractError, endpoint, request_json


class JellyfinAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Jellyfin API key is required")
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Emby-Token": api_key, "Accept": "application/json"}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def sessions(self) -> list[dict[str, Any]]:
        payload = await request_json(
            self.client,
            "GET",
            endpoint(self.base_url, "/Sessions"),
            headers=self.headers,
        )
        if not isinstance(payload, list):
            raise ContractError("Jellyfin sessions response must be a list")
        sessions: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ContractError("Jellyfin session item is not an object")
            play_state = item.get("PlayState")
            if play_state is not None and not isinstance(play_state, dict):
                raise ContractError("Jellyfin PlayState is incompatible")
            sessions.append(
                {
                    "client": item.get("Client", "unknown"),
                    "remote": bool(item.get("RemoteEndPoint")),
                    "mode": "transcoding"
                    if isinstance(play_state, dict) and play_state.get("PlayMethod") == "Transcode"
                    else "direct",
                }
            )
        return sessions
