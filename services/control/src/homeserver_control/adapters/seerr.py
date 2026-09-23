from __future__ import annotations

import httpx

from .http import ContractError, CredentialError, UpstreamError, endpoint, request_json


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

    async def get_request_status(self, source_id: str) -> int | None:
        parts = source_id.split(":")
        if len(parts) not in {1, 2} or not all(part.isdecimal() for part in parts):
            raise ValueError("Seerr request ID is invalid")
        try:
            response = await self.client.get(
                endpoint(self.base_url, f"/api/v1/request/{parts[0]}"),
                headers=self.headers,
            )
        except httpx.HTTPError as error:
            raise UpstreamError("Seerr request lookup failed") from error
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise CredentialError("Seerr credential rejected")
        if response.status_code >= 400:
            raise UpstreamError(f"Seerr returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise ContractError("Seerr request response is not JSON") from error
        if (
            not isinstance(payload, dict)
            or payload.get("id") != int(parts[0])
            or not isinstance(payload.get("status"), int)
        ):
            raise ContractError("Seerr request response is invalid")
        if len(parts) == 1 or payload["status"] != 2:
            return payload["status"]
        seasons = payload.get("seasons")
        if not isinstance(seasons, list):
            raise ContractError("Seerr request is missing seasons")
        selected = next(
            (season for season in seasons if isinstance(season, dict)
             and season.get("seasonNumber") == int(parts[1])),
            None,
        )
        if selected is None:
            return None
        if not isinstance(selected.get("status"), int):
            raise ContractError("Seerr season status is invalid")
        return selected["status"]

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
            source_id = media.get("tmdbId")
            media_type = media.get("mediaType")
            if (
                not isinstance(source_id, int) or source_id <= 0
                or media_type not in {"movie", "tv"}
            ):
                raise ContractError("Seerr media identity is incomplete")
            request_id = item.get("id")
            if not isinstance(request_id, (int, str)):
                raise ContractError("Seerr request has no stable id")
            if media_type == "movie":
                result.append({
                    "source_id": str(request_id),
                    "media_key": f"movie:tmdb:{source_id}",
                    "kind": "movie",
                })
                continue
            seasons = item.get("seasons")
            if not isinstance(seasons, list):
                raise ContractError("Seerr TV request is missing seasons")
            for season in seasons:
                if not isinstance(season, dict):
                    raise ContractError("Seerr TV season is invalid")
                number = season.get("seasonNumber")
                if not isinstance(number, int) or number < 0:
                    raise ContractError("Seerr TV season number is invalid")
                if season.get("status") == 2:
                    result.append({
                        "source_id": f"{request_id}:{number}",
                        "media_key": f"season:tmdb:{source_id}:{number}",
                        "kind": "season",
                    })
        return result
