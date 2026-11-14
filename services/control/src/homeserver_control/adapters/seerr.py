from __future__ import annotations

import time

import httpx

from homeserver_common.seerr_csrf import SeerrClient

from .http import (
    ContractError,
    CredentialError,
    RateLimitError,
    UpstreamError,
    endpoint,
    request_json,
)


class SeerrAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        page_size: int = 20,
        include_series_titles: bool = False,
    ) -> None:
        if not api_key:
            raise ValueError("Seerr API key is required")
        if not 1 <= page_size <= 100:
            raise ValueError("Seerr page size must be between 1 and 100")
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self.client = SeerrClient(
            client or httpx.AsyncClient(timeout=httpx.Timeout(10.0)), base_url=self.base_url,
        )
        self.page_size = page_size
        self.include_series_titles = include_series_titles
        self._series_titles: dict[int, tuple[float, str | None]] = {}

    async def _localized_series_title(self, tmdb_id: int) -> str | None:
        cached = self._series_titles.get(tmdb_id)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        title = None
        try:
            response = await self.client.get(
                endpoint(self.base_url, f"/api/v1/tv/{tmdb_id}"), headers=self.headers,
                params={"language": "pt-BR"}, follow_redirects=False,
            )
            if response.status_code == 200:
                payload = response.json()
                if (isinstance(payload, dict) and type(payload.get("id")) is int
                        and payload["id"] == tmdb_id
                        and isinstance(payload.get("name"), str) and payload["name"].strip()):
                    title = payload["name"].strip()
        except (httpx.HTTPError, ValueError):
            pass  # Optional metadata must never fabricate an alias or hide approved requests.
        if len(self._series_titles) >= 512:
            self._series_titles.pop(next(iter(self._series_titles)))
        self._series_titles[tmdb_id] = (time.monotonic() + (43200 if title else 300), title)
        return title

    async def get_series_request(self, source_id: str) -> dict[str, object] | None:
        """Refresh one approved/available season's authenticated identity for retries."""
        parts = source_id.split(":") if isinstance(source_id, str) else []
        if (len(parts) != 2 or not all(part.isascii() and part.isdecimal() for part in parts)
                or int(parts[0]) <= 0):
            raise ValueError("Seerr TV request ID is invalid")
        request_id, season_number = map(int, parts)
        try:
            response = await self.client.get(
                endpoint(self.base_url, f"/api/v1/request/{parts[0]}"),
                headers=self.headers, follow_redirects=False,
            )
        except httpx.HTTPError as error:
            raise UpstreamError("Seerr TV request lookup failed") from error
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise CredentialError("Seerr credential rejected")
        if response.status_code == 429:
            raise RateLimitError("Seerr rate limit", response.headers.get("retry-after"))
        if response.status_code != 200:
            raise UpstreamError(f"Seerr returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise ContractError("Seerr TV request response is not JSON") from error
        if (not isinstance(payload, dict) or type(payload.get("id")) is not int
                or payload["id"] != request_id or type(payload.get("status")) is not int
                or payload["status"] not in {1, 2, 3, 4, 5}):
            raise ContractError("Seerr TV request response is invalid")
        if payload["status"] not in {2, 5}:
            return None
        media, seasons = payload.get("media"), payload.get("seasons")
        if (not isinstance(media, dict) or media.get("mediaType") != "tv"
                or any(type(media.get(field)) is not int or media[field] <= 0
                       for field in ("tmdbId", "tvdbId", "externalServiceId"))
                or not isinstance(seasons, list)):
            raise ContractError("Seerr TV request identity is incomplete")
        seen = set()
        selected = None
        for season in seasons:
            if (not isinstance(season, dict) or type(season.get("seasonNumber")) is not int
                    or season["seasonNumber"] < 0 or season["seasonNumber"] in seen
                    or type(season.get("status")) is not int
                    or season["status"] not in {1, 2, 3, 4, 5}):
                raise ContractError("Seerr TV request season is invalid")
            seen.add(season["seasonNumber"])
            if season["seasonNumber"] == season_number:
                selected = season
        if selected is None or selected["status"] not in {2, 5}:
            return None
        result: dict[str, object] = {
            "source_id": source_id, "media_key": f"season:tmdb:{media['tmdbId']}:{season_number}",
            "kind": "season", "tvdb_id": media["tvdbId"],
            "external_service_id": media["externalServiceId"],
        }
        if self.include_series_titles:
            title = await self._localized_series_title(media["tmdbId"])
            if title is not None:
                result["title"] = title
        return result

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

    async def list_arr_servers(self, service: str) -> list[dict]:
        if service not in {"radarr", "sonarr"}:
            raise ValueError("unsupported Seerr native service")
        payload = await request_json(
            self.client, "GET", endpoint(self.base_url, f"/api/v1/settings/{service}"),
            headers=self.headers,
        )
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ContractError("Seerr native server settings are invalid")
        return payload

    async def list_approved(self, page: int) -> list[dict[str, object]]:
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
        result: list[dict[str, object]] = []
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
                type(source_id) is not int or source_id <= 0
                or media_type not in {"movie", "tv"}
            ):
                raise ContractError("Seerr media identity is incomplete")
            request_id = item.get("id")
            if not (
                (type(request_id) is int and request_id > 0)
                or (
                    isinstance(request_id, str)
                    and request_id.isascii()
                    and request_id.isdecimal()
                    and request_id.lstrip("0")
                )
            ):
                raise ContractError("Seerr request has no stable id")
            selection = ({
                "profile_id": item["profileId"], "server_id": item.get("serverId"),
                "is_4k": item.get("is4k", False),
            } if item.get("profileId") is not None else {})
            if media_type == "movie":
                result.append({
                    "source_id": str(request_id),
                    "media_key": f"movie:tmdb:{source_id}",
                    "kind": "movie",
                    **selection,
                })
                continue
            seasons = item.get("seasons")
            if not isinstance(seasons, list):
                raise ContractError("Seerr TV request is missing seasons")
            binding = {}
            for upstream, normalized in (
                ("tvdbId", "tvdb_id"), ("externalServiceId", "external_service_id"),
            ):
                value = media.get(upstream)
                if value is not None:
                    if type(value) is not int or value <= 0:
                        raise ContractError("Seerr TV service identity is invalid")
                    binding[normalized] = value
            if self.include_series_titles and any(
                isinstance(season, dict) and season.get("status") == 2 for season in seasons
            ):
                title = await self._localized_series_title(source_id)
                if title is not None:
                    binding["title"] = title
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
                        **binding,
                        **selection,
                    })
        return result
