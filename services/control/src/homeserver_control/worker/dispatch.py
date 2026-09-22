from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from homeserver_control.gateway.permits import PermitRegistry


class DownloadUpstream(Protocol):
    def add_torrent(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class DispatchResult:
    operation_id: str
    result: dict[str, Any]


class DownloadDispatcher:
    """Apply a persisted permit exactly once before a qBittorrent mutation."""

    def __init__(self, *, permits: PermitRegistry, upstream: DownloadUpstream) -> None:
        self.permits = permits
        self.upstream = upstream

    def dispatch(
        self,
        *,
        permit_token: str,
        infohash: str,
        destination: str,
        payload: dict[str, Any],
    ) -> DispatchResult:
        def effect(_permit: object) -> dict[str, Any]:
            return self.upstream.add_torrent(payload)

        result = self.permits.authorize(
            token=permit_token,
            infohash=infohash,
            destination=destination,
            effect=effect,
        )
        permit = self.permits.get(permit_token)
        if permit is None:
            raise RuntimeError("permit disappeared after authorization")
        return DispatchResult(operation_id=permit.operation_id, result=result)
