from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class MediaRef:
    media_key: str
    kind: Literal["movie", "episode", "season"]
    source_id: int


@dataclass(frozen=True)
class ReleaseRef:
    guid: str
    indexer_id: int
    app: Literal["sonarr", "radarr"]
    title: str
    reported_bytes: int
    torrent_url: str


class RequestAdapter(Protocol):
    async def list_approved(self, page: int) -> list[dict[str, str]]: ...


class ArrAdapter(Protocol):
    async def search(self, media: MediaRef) -> list[ReleaseRef]: ...

    async def grab(self, release: ReleaseRef) -> str: ...

    async def import_verified(self, job_id: str) -> str: ...

    async def unmonitor(self, media_key: str) -> None: ...
