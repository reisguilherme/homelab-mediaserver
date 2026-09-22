from __future__ import annotations

import httpx

from .http import ContractError, endpoint, request_json


class SubtitleAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("subtitle service API key is required")
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))

    async def request_missing(self, *, media_key: str, language: str) -> str:
        if not media_key or not language:
            raise ValueError("media key and language are required")
        payload = await request_json(
            self.client,
            "POST",
            endpoint(self.base_url, "/api/v1/subtitles/search"),
            headers=self.headers,
            json_body={"media_key": media_key, "language": language},
            uncertain_on_timeout=True,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), (int, str)):
            raise ContractError("subtitle service response has no job id")
        return str(payload["id"])
