"""Sanitized download monitoring and durable manual source replacement admission."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
from fastapi import HTTPException

from homeserver_control.persistence.source_requests import SourceReplacementRequests


class DownloadControl:
    def __init__(
        self,
        *,
        db_path: str | Path,
        requests: SourceReplacementRequests,
        gateway_url: str,
        arr_token: str,
        transport=None,
    ) -> None:
        self.db_path = str(db_path)
        self.requests = requests
        self.gateway_url = gateway_url.rstrip("/")
        self.arr_token = arr_token
        self.transport = transport

    async def _entries(self) -> list[dict]:
        async with httpx.AsyncClient(timeout=20, transport=self.transport) as client:
            response = await client.get(
                f"{self.gateway_url}/api/v2/torrents/info",
                headers={"X-Arr-Token": self.arr_token},
            )
            response.raise_for_status()
            entries = response.json()
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise ValueError("invalid download inventory")
        return entries

    @staticmethod
    def _unfinished(entry) -> bool:
        return (
            isinstance(entry.get("hash"), str)
            and re.fullmatch(r"[0-9a-fA-F]{40}", entry["hash"]) is not None
            and type(entry.get("amount_left")) is int
            and entry["amount_left"] > 0
            and type(entry.get("total_size")) is int
            and entry["total_size"] > 0
            and type(entry.get("progress")) in {int, float}
            and 0 <= entry["progress"] < 1
        )

    async def list(self) -> dict:
        items = []
        for entry in await self._entries():
            if not self._unfinished(entry):
                continue
            source = self.requests.source(entry["hash"].lower())
            if source is None:
                continue
            allowed = self.requests.source_allowed(source)
            command = self.requests.latest(source["permit_id"])
            reason = (
                "A solicitação foi cancelada, protegida ou a importação já começou."
                if not allowed
                else ""
            )
            items.append(
                {
                    "infohash": source["infohash"],
                    "permit_id": source["permit_id"],
                    "name": str(entry.get("name", "Download"))[:500],
                    "state": entry.get("state"),
                    "progress": entry["progress"],
                    "remaining_bytes": entry["amount_left"],
                    "total_bytes": entry["total_size"],
                    "download_bps": entry.get("dlspeed", 0),
                    "seeds": entry.get("num_seeds", 0),
                    "pool_id": source["pool_id"],
                    "scope_key": source["scope_key"],
                    "replace_allowed": allowed,
                    "replace_unavailable_reason": reason,
                    "replacement": self.public_request(command) if command else None,
                }
            )
        return {
            "items": items,
            "recent_requests": [self.public_request(command) for command in self.requests.recent()],
        }

    @staticmethod
    def public_request(command: dict) -> dict:
        return {key: command.get(key) for key in ("request_id", "state", "result", "updated_at")}

    async def request_replacement(self, infohash: str, permit_id: str) -> dict:
        if re.fullmatch(r"[0-9a-fA-F]{40}", infohash) is None:
            raise HTTPException(422, "invalid source identity")
        source = self.requests.source(infohash.lower())
        if (
            source is None
            or source["permit_id"] != permit_id
            or not self.requests.source_allowed(source)
        ):
            raise HTTPException(409, "source changed or is inactive")
        entries = await self._entries()
        if not any(
            entry.get("hash", "").lower() == infohash.lower() and self._unfinished(entry)
            for entry in entries
        ):
            raise HTTPException(409, "source is complete or missing")
        return self.public_request(self.requests.enqueue(source))
