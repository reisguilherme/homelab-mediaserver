from __future__ import annotations

import httpx

from .http import ContractError, endpoint, request_json


class SeerrAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        page_size: int = 20,
    ) -> None:
        if not api_key:
            raise ValueError("Seerr API key is required")
        if not 1 <= page_size <= 100:
            raise ValueError("Seerr page size must be between 1 and 100")
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.page_size = page_size

    async def list_approved(self, page: int) -> list[dict[str, str]]:
        if page < 1:
            raise ValueError("page must be positive")
        payload = await request_json(
            self.client,
            "GET",
            endpoint(self.base_url, "/api/v1/request"),
            headers=self.headers,
            json_body=None,
            params={
                "take": self.page_size,
                "skip": (page - 1) * self.page_size,
                "filter": "approved",
            },
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ContractError("Seerr request response has no results list")
        result: list[dict[str, str]] = []
        for item in payload["results"]:
            # Seerr returns numeric request status: 1 pending, 2 approved,
            # 3 declined. It does not expose an isApproved field.
            if not isinstance(item, dict) or item.get("status") != 2:
                continue
            media = item.get("media")
            if not isinstance(media, dict):
                raise ContractError("Seerr request is missing media")
            source_id = media.get("tmdbId") or media.get("tvdbId")
            media_type = media.get("mediaType")
            if not isinstance(source_id, int) or media_type not in {"movie", "tv"}:
                raise ContractError("Seerr media identity is incomplete")
            kind = "movie" if media_type == "movie" else "season"
            source_name = "tmdb" if media_type == "movie" else "tvdb"
            request_id = item.get("id")
            if not isinstance(request_id, (int, str)):
                raise ContractError("Seerr request has no stable id")
            result.append(
                {
                    "source_id": str(request_id),
                    "media_key": f"{kind}:{source_name}:{source_id}",
                    "kind": kind,
                }
            )
        return result
