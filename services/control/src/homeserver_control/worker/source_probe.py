"""Measured live source trials; the primary transfer stays active until promotion."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta

import httpx

from .source_health import TorrentHealth
from .subdl import _movie_editions


def infer_source_quality(title: str) -> tuple[int, ...] | None:
    resolution = re.search(r"(?<![0-9])(720|1080|2160)p(?![0-9])", title, re.I)
    if resolution is None:
        return None
    if re.search(r"\bremux\b", title, re.I):
        tier = 3
    elif re.search(r"blu[ ._-]?ray|\bbdrip\b", title, re.I):
        tier = 2
    elif re.search(r"web[ ._-]?dl", title, re.I):
        tier = 1
    else:
        return None
    return tier, int(resolution[1]), 0, 0, 0, 0


def canonical_quality(release: dict) -> tuple[int, ...] | None:
    """Physical source/resolution, independent of the operator's preference order."""
    quality = release.get("quality")
    detail = quality.get("quality") if isinstance(quality, dict) else None
    if not isinstance(detail, dict):
        return None
    resolution = detail.get("resolution")
    if isinstance(resolution, bool) or resolution not in {720, 1080, 2160}:
        return None
    source, modifier = detail.get("source"), detail.get("modifier", "none")
    if source == "blurayRaw":
        tier = 3
    elif source == "bluray":
        tier = (
            3
            if modifier == "remux" or re.search(r"\bremux\b", str(release.get("title", "")), re.I)
            else 2
        )
    elif (
        source == "webdl"
        or source == "web"
        and str(detail.get("name", "")).lower().startswith("webdl")
    ):
        tier = 1
    else:
        return None
    return tier, resolution, 0, 0, 0, 0


def edition_identity(title: str) -> str:
    return ",".join(sorted(_movie_editions(title))) or "standard"


class LiveSourceProbes:
    async def _read_source_health(self, permit):
        response = await self.client.get(
            f"{self.gateway_url}/internal/torrent-health",
            headers={"X-Arr-Token": self.arr_token, "X-Admission-Permit": permit.token},
        )
        response.raise_for_status()
        payload = response.json()
        health = TorrentHealth.from_mapping(payload)
        if health.infohash != permit.infohash:
            raise ValueError("probe source identity changed")
        return health, payload

    async def _decide_probe(self, permit, decision):
        response = await self.client.post(
            f"{self.gateway_url}/internal/probe-decision",
            headers={"X-Arr-Token": self.arr_token},
            json={"permit_token": permit.token, "decision": decision},
        )
        response.raise_for_status()
        return response.json()["state"]

    async def _monitor_probe(self, current) -> str | None:
        if not self.live_source_probes:
            return None
        if self.permits.pending_handover(current.token) is not None:
            if self.permits.probe_reservation_active(current):
                await self.capacity_provider()
            return await self._decide_probe(current, "promote")
        probe = self.permits.get_probe(current.permit_id)
        if probe is None:
            return None
        if (
            not self.permits.probe_reservation_active(probe)
            or datetime.now(UTC) >= probe.expires_at
        ):
            return await self._decide_probe(probe, "reject")
        if probe.state in {"dispatching", "unknown"}:
            response = await self.client.post(
                f"{self.gateway_url}/internal/reconcile-source",
                headers={"X-Arr-Token": self.arr_token},
                json={"permit_token": probe.token},
            )
            response.raise_for_status()
            if response.json().get("state") != "confirmed":
                return "probe_uncertain"
            probe = self.permits.get(probe.token)
        if probe.state == "authorized":
            capacity = await self.capacity_provider()
            if self.permits.pending_bytes(capacity) > capacity.free_bytes:
                return await self._decide_probe(probe, "reject")
            torrent = self.torrent_store.get(probe) if self.torrent_store else None
            if torrent is None or datetime.now(UTC) >= probe.expires_at:
                self.permits.reject_probe(probe.token)
                return "probe_unavailable"
            await self._add_verified_torrent(probe, torrent)
            probe = self.permits.get(probe.token)
        try:
            old_health, _ = await self._read_source_health(current)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 409:
                return await self._decide_probe(probe, "reject")
            raise
        new_health, _ = await self._read_source_health(probe)
        if not self.permits.probe_reservation_active(probe):
            return await self._decide_probe(probe, "reject")
        if datetime.now(UTC) >= probe.expires_at:
            return await self._decide_probe(probe, "reject")
        decision = self.health_store.probe_decision(
            current.permit_id, probe.permit_id, old_health, new_health, now=time.time()
        )
        if decision == "observing":
            return "probing"
        if decision == "promote":
            capacity = await self.capacity_provider()
            if self.permits.pending_bytes(capacity) > capacity.free_bytes:
                decision = "reject"
        state = await self._decide_probe(probe, decision)
        return "replaced" if state == "promoted" else "probe_rejected"

    async def _start_probe(
        self,
        *,
        old,
        infohash,
        metadata_sha256,
        selected_files,
        exact_bytes,
        torrent,
        reported_seeders=None,
        quality_rank=None,
    ):
        if self.health_store.is_protected(old.infohash):
            return "source_protected"
        if old.quality_rank is None:
            return "source_quality_unknown"
        capacity = await self.capacity_provider()
        candidate = self.permits.issue_probe(
            old.token,
            infohash=infohash,
            metadata_sha256=metadata_sha256,
            selected_files=selected_files,
            budget_bytes=exact_bytes,
            capacity=capacity,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            reported_seeders=reported_seeders,
            quality_rank=quality_rank,
        )
        self.torrent_store.put(candidate, torrent)
        await self._add_verified_torrent(candidate, torrent)
        return await self._monitor_probe(old) or "probing"
