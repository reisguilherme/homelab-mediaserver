from __future__ import annotations

from collections.abc import Mapping

import httpx

from .contracts import MediaRef, ReleaseRef
from .http import (
    ContractError,
    CredentialError,
    EffectUncertain,
    RateLimitError,
    UpstreamError,
    endpoint,
    request_json,
)

__all__ = [
    "ArrAdapterClient",
    "ContractError",
    "CredentialError",
    "EffectUncertain",
    "RateLimitError",
    "UpstreamError",
]


class ArrAdapterClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        app: str = "radarr",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Arr API key is required")
        if app not in {"sonarr", "radarr"}:
            raise ValueError("Arr app must be sonarr or radarr")
        self.base_url = base_url.rstrip("/")
        self.app = app
        self.headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))

    async def search(self, media: MediaRef) -> list[ReleaseRef]:
        payload = await request_json(
            self.client,
            "GET",
            endpoint(self.base_url, "/api/v3/release"),
            headers=self.headers,
        )
        if not isinstance(payload, list):
            raise ContractError("Arr release response must be a list")
        releases: list[ReleaseRef] = []
        for item in payload:
            if not isinstance(item, Mapping):
                raise ContractError("Arr release item is not an object")
            guid = item.get("guid")
            indexer_id = item.get("indexerId")
            title = item.get("title")
            size = item.get("size")
            torrent_url = item.get("downloadUrl") or item.get("magnetUrl")
            if (
                not isinstance(guid, str)
                or not isinstance(indexer_id, int)
                or not isinstance(title, str)
                or not isinstance(size, int)
                or size <= 0
                or not isinstance(torrent_url, str)
            ):
                raise ContractError("Arr release is missing verified fields")
            releases.append(
                ReleaseRef(
                    guid=guid,
                    indexer_id=indexer_id,
                    app=self.app,
                    title=title,
                    reported_bytes=size,
                    torrent_url=torrent_url,
                )
            )
        return releases

    async def grab(self, release: ReleaseRef | Mapping[str, object]) -> str:
        if isinstance(release, Mapping):
            guid = release.get("guid")
            indexer_id = release.get("indexer_id", release.get("indexerId"))
        else:
            guid = release.guid
            indexer_id = release.indexer_id
        if not isinstance(guid, str) or not isinstance(indexer_id, int):
            raise ContractError("release identity is incomplete")
        payload = await request_json(
            self.client,
            "POST",
            endpoint(self.base_url, "/api/v3/command"),
            headers=self.headers,
            json_body={
                "name": "DownloadRelease",
                "release": {"guid": guid, "indexerId": indexer_id},
            },
            uncertain_on_timeout=True,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), (int, str)):
            raise ContractError("Arr grab response has no command id")
        return str(payload["id"])

    async def import_verified(self, job_id: str) -> str:
        if not job_id:
            raise ValueError("job id is required")
        command_name = "DownloadedMoviesScan" if self.app == "radarr" else "DownloadedEpisodesScan"
        payload = await request_json(
            self.client,
            "POST",
            endpoint(self.base_url, "/api/v3/command"),
            headers=self.headers,
            json_body={"name": command_name, "path": job_id},
            uncertain_on_timeout=True,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), (int, str)):
            raise ContractError("Arr import response has no command id")
        return str(payload["id"])

    async def unmonitor(self, media_key: str) -> None:
        if not media_key:
            raise ValueError("media key is required")
        # Entity lookup/update is deliberately explicit in the future adapter;
        # this endpoint is never exposed as an arbitrary proxy.
        await request_json(
            self.client,
            "POST",
            endpoint(self.base_url, "/api/v3/command"),
            headers=self.headers,
            json_body={"name": "Unmonitor", "mediaKey": media_key},
            uncertain_on_timeout=True,
        )
