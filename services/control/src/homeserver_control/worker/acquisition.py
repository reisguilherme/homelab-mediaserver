"""Choose an inspectable movie release and authorize one Radarr grab."""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from homeserver_common.download_profiles import PROFILE_NAMES
from homeserver_control.domain.magnet import magnet_infohash
from homeserver_control.domain.torrent_bytes import (
    TorrentBytesError,
    inspect_torrent,
    merge_magnet_trackers,
)
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore

from .audio_audit import record_audio_selection
from .audio_evidence import release_audio_languages
from .capacity_evidence import CapacityEvidence
from .dubbed_discovery import DubbedDiscovery, season_pack_queries
from .media_profiles import is_animation
from .release_quality import release_indexer, release_rank, release_seeders
from .source_health import SourceHealthStore, TorrentHealth
from .source_probe import (
    LiveSourceProbes,
    canonical_quality,
    edition_identity,
    infer_source_quality,
)
from .subdl import SubDLSource
from .subtitle_language import SubtitlePolicy, is_brazilian_portuguese_subtitle, is_english_subtitle

LOGGER = logging.getLogger(__name__)
_VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".mov"}
_SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt"}
_MAX_METADATA = 16 * 1024 * 1024
_TORRENT_CACHE = "https://itorrents.net/torrent"
AcquisitionCandidate = tuple[
    dict[str, object], tuple[str, str, tuple[str, ...], int], bytes | None, bytes
]


def _queue_only_rejection(release: dict[str, object]) -> bool:
    """Ignore only Arr's existing-queue veto while validating a replacement."""
    reasons = release.get("rejections")
    prefixes = (
        "release in queue already meets cutoff:",
        "quality for release in queue already meets cutoff:",
    )
    return (
        release.get("rejected") is True
        and isinstance(reasons, list)
        and bool(reasons)
        and all(isinstance(item, str) and item.casefold().startswith(prefixes) for item in reasons)
    )


def _replacement_has_peers(release: dict[str, object], reason: str, current_seeds: int) -> bool:
    reported = release.get("seeders")
    return (
        isinstance(reported, int)
        and not isinstance(reported, bool)
        and (
            reported >= 1
            if reason in {"stalled", "replacing"}
            else reported >= max(5, current_seeds * 2)
        )
    )


def _is_sample_video(path: str) -> bool:
    parsed = PurePosixPath(path)
    return any(part.lower() == "sample" for part in parsed.parts[:-1]) or bool(
        re.search(r"(?:^|[._ -])sample$", parsed.stem.lower())
    )


def _is_movie_extra_video(path: str) -> bool:
    """Explicitly labelled before/after-film supplements are not a second feature."""
    # A small file or a lower resolution alone never establishes that it is an extra.
    directories = {" ".join(part.casefold().split())
                   for part in PurePosixPath(path.replace("\\", "/")).parts[:-1]}
    return bool(directories & {"antes do filme", "depois do filme"})


class MovieAcquirer(LiveSourceProbes):
    def __init__(
        self,
        *,
        repository: ReservationRepository,
        permits: PermitRegistry,
        radarr_url: str,
        radarr_api_key: str,
        prowlarr_url: str,
        client: httpx.AsyncClient | None = None,
        retry_seconds: int = 900,
        subtitle_source: SubDLSource | None = None,
        subtitle_store: SubtitleArtifactStore | None = None,
        torrent_store: TorrentArtifactStore | None = None,
        capacity_provider: Callable[[], Awaitable[CapacityEvidence]] | None = None,
        health_store: SourceHealthStore | None = None,
        gateway_url: str | None = None,
        arr_token: str | None = None,
        availability_probe: Callable[[bytes, str], Awaitable[int | None]] | None = None,
        live_source_probes: bool = False,
        release_policy=None,
        subtitle_policy: SubtitlePolicy | None = None,
        source_retry_seconds: float = 300,
        search_timeout_seconds: float = 90,
        dubbed_discovery: DubbedDiscovery | None = None,
    ) -> None:
        if not radarr_api_key:
            raise ValueError("Radarr API key is required")
        if subtitle_source is not None and subtitle_store is None:
            raise ValueError("external subtitle storage is required")
        self.repository = repository
        self.permits = permits
        self.radarr_url = radarr_url.rstrip("/")
        self.prowlarr_url = prowlarr_url.rstrip("/")
        self.headers = {"X-Api-Key": radarr_api_key, "Accept": "application/json"}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self.retry_seconds = retry_seconds
        self.source_retry_seconds = source_retry_seconds
        self.search_timeout_seconds = search_timeout_seconds
        self.dubbed_discovery = dubbed_discovery
        self._dubbed_media = ContextVar("dubbed_media", default=None)
        self._dubbed_search_incomplete = ContextVar("dubbed_search_incomplete", default=False)
        self.subtitle_policy = subtitle_policy or SubtitlePolicy()
        self.subtitle_source = (
            subtitle_source if "subdl" in self.subtitle_policy.providers else None
        )
        self.subtitle_store = subtitle_store
        self.torrent_store = torrent_store
        self.capacity_provider = capacity_provider
        if health_store is not None and not all(
            value is not None for value in (gateway_url, arr_token, capacity_provider)
        ):
            raise ValueError("source replacement requires health, gateway and token")
        self.health_store = health_store
        self.gateway_url = gateway_url.rstrip("/") if gateway_url else None
        self.arr_token = arr_token
        self.availability_probe = availability_probe
        # Production injects the bounded, metadata-only peer resolver. Isolated
        # clients and tests do not start network sessions as a hidden side effect.
        self.metadata_resolver: Callable[[str], Awaitable[bytes | None]] | None = None
        self.live_source_probes = live_source_probes
        self._base_release_policy = release_policy
        self._selected_release_policy = ContextVar("selected_release_policy", default=None)
        self._source_titles: dict[str, str] = {}
        if live_source_probes and (health_store is None or torrent_store is None):
            raise ValueError("live source probes require durable health and torrent storage")
        self._next_search: dict[str, float] = {}
        self._next_health_check: dict[str, float] = {}
        self._posted: set[str] = set()
        self._manual_permit_id: str | None = None
        self._manual_scope_key: str | None = None

    @property
    def release_policy(self):
        return self._selected_release_policy.get() or self._base_release_policy

    @release_policy.setter
    def release_policy(self, value):
        self._base_release_policy = value
        if not hasattr(self, "_selected_release_policy"):
            self._selected_release_policy = ContextVar("selected_release_policy", default=None)

    async def acquire(self, media_key: str, reservation_id: str) -> str:
        """Keep native per-title choices scoped to this search, including on failure."""
        token = self._selected_release_policy.set(None)
        media_token = self._dubbed_media.set(None)
        search_token = self._dubbed_search_incomplete.set(False)
        try:
            return await self._acquire(media_key, reservation_id)
        finally:
            self._selected_release_policy.reset(token)
            self._dubbed_media.reset(media_token)
            self._dubbed_search_incomplete.reset(search_token)

    async def _prepare_download_profile(self, media) -> bool:
        self._dubbed_media.set(media)
        policy = self._base_release_policy
        if policy is None or policy.profile_name is None:
            return True
        profile_id = media.get("qualityProfileId")
        if type(profile_id) is not int or profile_id <= 0:
            return False
        response = await self.client.get(
            f"{self.radarr_url}/api/v3/qualityprofile", headers=self.headers,
        )
        response.raise_for_status()
        profiles = response.json()
        if not isinstance(profiles, list):
            return False
        matches = [
            item for item in profiles if isinstance(item, dict)
            and type(item.get("id")) is int and item["id"] == profile_id
        ]
        if len(matches) != 1 or matches[0].get("name") not in PROFILE_NAMES:
            return False
        name = matches[0]["name"]
        if sum(isinstance(item, dict) and item.get("name") == name for item in profiles) != 1:
            return False
        self._selected_release_policy.set(policy.for_profile(name))
        return True

    async def _discover_dubbed_releases(
        self, media, releases, *, season=None, indexers=None, complete_season_pack=False,
    ):
        if self.dubbed_discovery is None or self.release_policy is None:
            return releases
        policy = self.release_policy
        fallback_pack = (
            complete_season_pack and season is not None and indexers == ("1337x",)
            and "1337x" in policy.fallback_indexers
        )
        asc_pack = (
            complete_season_pack and season is not None and indexers is not None
            and "amigosshare" in indexers
            and "amigosshare" in policy.preferred_indexers
        )
        if not policy.dubbed and not fallback_pack and not asc_pack:
            return releases
        if policy.fallback_indexers and indexers is None:
            # Discovery is deferred until this source tier is actually consumed.
            return releases
        if indexers is not None:
            discovery_indexers = ("amigosshare",) if asc_pack and not policy.dubbed else indexers
            policy = replace(policy, indexer_priority=discovery_indexers, fallback_indexers=())
        result = await self.dubbed_discovery.enrich(
            media, releases, arr_url=self.radarr_url, arr_headers=self.headers,
            season=season, allow_supplemental=self._manual_permit_id is None,
            release_policy=policy,
            rejection_allowed=self._dubbed_rejection_allowed,
            **({"require_complete_season_pack": True} if complete_season_pack else {}),
            **({"queries_override": season_pack_queries(media, season)} if fallback_pack else {}),
        )
        if not result.complete:
            self._dubbed_search_incomplete.set(True)
            LOGGER.warning("PT-BR discovery incomplete for %s %s", media.get("id"), season)
            if not policy.dubbed:
                return result.releases
            # A failed/blocked source is not proof that PT-BR is unavailable.
            # Explicit PT-BR can proceed; original-only fallback waits for a full search.
            return [row for row in result.releases if (
                self._rank(row, original_language=media.get("originalLanguage"),
                           animation=is_animation(media)) is not None
                and "pt-br" in release_audio_languages(row)
            )]
        return result.releases

    def _dubbed_rejection_allowed(self, release) -> bool:
        return self._manual_permit_id is not None and _queue_only_rejection(release)

    async def _dispatch_release(self, release, permit, *, original_language=None) -> bool:
        supplemental = release.get("requiresNativeDecision") is True
        endpoint = "release/push" if supplemental else "release"
        payload = {key: value for key, value in release.items() if key not in {
            "requiresNativeDecision", "audioEvidence", "audioLanguagesInferred",
        }}
        response = await self.client.post(
            f"{self.radarr_url}/api/v3/{endpoint}", headers=self.headers,
            json={**payload, "downloadClientId": 1},
        )
        confirmed = False
        if supplemental and response.status_code in (400, 404, 409, 422):
            current = self.permits.get(permit.token)
            confirmed = current is not None and current.state == "confirmed"
            if not confirmed and self.permits.cancel_authorized(permit.token):
                return False
        if not confirmed:
            response.raise_for_status()
        if supplemental and not confirmed:
            decisions = response.json()
            if (not isinstance(decisions, list) or len(decisions) != 1
                    or not isinstance(decisions[0], dict)
                    or decisions[0].get("rejected") is not False):
                # Do not cancel a dispatch already confirmed by the gateway even
                # if Arr returned an inconsistent/ambiguous response.
                current = self.permits.get(permit.token)
                if current is None or current.state != "confirmed":
                    if not self.permits.cancel_authorized(permit.token):
                        raise ValueError("native release decision is uncertain")
                    return False
        if self.release_policy is not None:
            record_audio_selection(
                self.repository, permit, release, original_language=original_language,
                profile_name=self.release_policy.profile_name,
                preferences=self.release_policy.dubbed_audio_preferences,
            )
        return True

    async def manual_replace(
        self, media_key: str, reservation_id: str, permit_id: str,
        *, scope_key: str | None = None,
    ) -> str:
        """Authorize one source search from an exact durable operator command."""
        current = self.permits.get_for_reservation(reservation_id, scope_key=scope_key)
        if (
            current is None or current.permit_id != permit_id or current.state != 'confirmed'
            or current.season_pack_parent_id is not None
        ):
            return 'source_changed'
        if self.permits.get_probe(current.permit_id) is not None:
            return 'transition_pending'
        if self.health_store and self.health_store.is_protected(current.infohash):
            return 'source_protected'
        if not self.permits.probe_reservation_active(current):
            return 'reservation_inactive'
        if self._manual_permit_id is not None:
            return 'transition_pending'
        self._manual_permit_id = permit_id
        self._manual_scope_key = scope_key
        search_key = f'{reservation_id}:{scope_key}' if scope_key is not None else reservation_id
        self._next_search.pop(search_key, None)
        try:
            return await self.acquire(media_key, reservation_id)
        finally:
            self._manual_permit_id = None
            self._manual_scope_key = None

    async def _source_status(self, permit) -> tuple[str | None, TorrentHealth | None]:
        if self.health_store is None or self.gateway_url is None or self.arr_token is None:
            return None, None
        manual = self._manual_permit_id == permit.permit_id
        if not manual and self.health_store.is_protected(permit.infohash):
            return None, None
        now = time.monotonic()
        if not manual and now < self._next_health_check.get(permit.permit_id, 0):
            return None, None
        self._next_health_check[permit.permit_id] = now + 60
        response = await self.client.get(
            f"{self.gateway_url}/internal/torrent-health",
            headers={"X-Arr-Token": self.arr_token, "X-Admission-Permit": permit.token},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid gateway source health")
        health = TorrentHealth.from_mapping(payload)
        title = payload.get("name")
        if isinstance(title, str):
            self._source_titles[permit.permit_id] = title
            if permit.quality_rank is None:
                rank = infer_source_quality(title)
                if rank is not None:
                    self.permits.set_quality(permit.token, rank)
                    permit.quality_rank = rank
        if health.infohash != permit.infohash:
            raise ValueError("gateway source identity changed")
        if manual:
            # Manual permission bypasses time/velocity policy, never completion or identity.
            self.health_store.observe(permit.permit_id, health, now=time.time())
            return ('stalled' if health.amount_left > 0 and health.progress < 1 else None), health
        return self.health_store.observe(permit.permit_id, health, now=time.time()), health

    def _rank(self, release, *, original_language=None, animation=False):
        return (
            self.release_policy.rank(
                release, original_language=original_language, animation=animation,
            )
            if self.release_policy
            else release_rank(release, original_language=original_language, animation=animation)
        )

    async def _preferred_indexer_candidates(
        self, ordered, *, eligible, rank, transform=None, season=None, release_filter=None,
        complete_season_pack=False,
    ):
        """Exhaust each opted-in source tier before inspecting the next one."""
        priorities = self.release_policy.preferred_indexers if self.release_policy else ()
        fallback = self.release_policy.fallback_indexers if self.release_policy else ()
        tiers = (
            (tuple(name for name in priorities if name not in fallback),
             tuple(name for name in priorities if name in fallback))
            if fallback else (priorities,)
        )
        for tier in tiers:
            releases = ordered
            if (
                (
                    fallback and self.release_policy.dubbed
                    or complete_season_pack and season is not None
                    and ("amigosshare" in tier or tier == ("1337x",))
                )
                and getattr(self, "dubbed_discovery", None) is not None
                and (media := self._dubbed_media.get()) is not None
            ):
                releases = await self._discover_dubbed_releases(
                    media, [row for row in ordered if release_indexer(row) in tier],
                    season=season, indexers=tier,
                    complete_season_pack=complete_season_pack,
                )
                releases = sorted(
                    (row for row in releases if release_filter is None or release_filter(row)),
                    key=lambda row: rank(row) or (0,) * 8, reverse=True,
                )
            async with aclosing(self._compare_indexer_candidates(
                releases, eligible=eligible, rank=rank, priorities=tier,
            )) as candidates:
                if transform is None:
                    async for candidate in candidates:
                        yield candidate
                else:
                    # Live replacement probes and unknown swarms remain inside
                    # their source tier, including when admission rejects a result.
                    async with aclosing(transform(candidates)) as transformed:
                        async for candidate in transformed:
                            yield candidate

    async def _compare_indexer_candidates(self, ordered, *, eligible, rank, priorities):
        """Keep the legacy bounded merge and Dublado preference within a tier."""
        if self.release_policy is None:
            async with aclosing(eligible(ordered)) as candidates:
                async for candidate in candidates:
                    yield candidate
            return
        primary_releases = [item for item in ordered if release_indexer(item) == priorities[0]]
        fallback_releases = [item for item in ordered if release_indexer(item) in priorities[1:]]

        def preference(release):
            key = rank(release)
            if self.release_policy.dubbed and "amigosshare" in priorities:
                # Keep saved release ranks stable; source preference applies only
                # after audio and visual tiers, before swarm and extra features.
                return (*key[:2], int(release_indexer(release) == "amigosshare"), *key[2:])
            return key

        # Compare both allowed indexers before dispatch. Keep one candidate body
        # per iterator, so choosing the most seeded compatible source stays bounded.
        async with aclosing(eligible(primary_releases)) as primary:
            first = await anext(primary, None)
            async with aclosing(eligible(fallback_releases)) as fallback:
                second = await anext(fallback, None)
                while first is not None or second is not None:
                    if second is None or (
                        first is not None and preference(first[0]) >= preference(second[0])
                    ):
                        yield first
                        first = await anext(primary, None)
                    else:
                        yield second
                        second = await anext(fallback, None)

    def _acceptable_video_size(self, release, torrent, runtime_minutes):
        if self.release_policy is None:
            return True
        videos = [
            item
            for item in inspect_torrent(torrent).files
            if PurePosixPath(item.path).suffix.lower() in _VIDEO_SUFFIXES
            and not _is_sample_video(item.path)
            and not _is_movie_extra_video(item.path)
        ]
        return len(videos) == 1 and self.release_policy.accepts_size(
            release, video_bytes=videos[0].length, runtime_minutes=runtime_minutes
        )

    async def _source_state(self, permit, action: str) -> None:
        assert self.gateway_url is not None and self.arr_token is not None
        response = await self.client.post(
            f"{self.gateway_url}/internal/source-state",
            headers={"X-Arr-Token": self.arr_token},
            json={"permit_token": permit.token, "action": action},
        )
        response.raise_for_status()

    async def _resume_if_safe(self, permit) -> str:
        """Only restart a stopped source when its remaining bytes still fit."""
        assert self.capacity_provider is not None
        try:
            capacity = await self.capacity_provider()
        except Exception:
            LOGGER.warning("Cannot restart source %s without fresh capacity", permit.infohash)
            return "capacity_unavailable"
        if (
            self.permits.pending_bytes(
                capacity, include_infohash=permit.infohash, pool_id=permit.pool_id
            )
            > capacity.pool(permit.pool_id).usable_free_bytes
        ):
            LOGGER.warning(
                "Keeping source %s stopped because space is insufficient", permit.infohash
            )
            return "waiting_space"
        await self._source_state(permit, "start")
        assert self.health_store is not None
        self.health_store.clear_replacing(permit.permit_id)
        return "resumed"

    async def _dispatch_replacement(
        self,
        *,
        old,
        infohash: str,
        metadata_sha256: str,
        selected_files: tuple[str, ...],
        exact_bytes: int,
        torrent: bytes,
        reported_seeders: int | None = None,
        quality_rank=None,
        episode_files=None,
        episode_catalog=None,
        allow_absolute=False,
    ) -> str:
        assert self.health_store is not None
        assert self.gateway_url is not None and self.arr_token is not None
        assert self.capacity_provider is not None
        if self._manual_permit_id == old.permit_id:
            if not self.permits.probe_reservation_active(old):
                return 'reservation_inactive'
            health, _ = await self._read_source_health(old)
            if health.amount_left <= 0 or health.progress >= 1:
                return 'already_completed'
            capacity = await self.capacity_provider()
            if not self.permits.probe_reservation_active(old):
                return 'reservation_inactive'
            if exact_bytes > self.permits.available_bytes(capacity):
                raise PermissionError('waiting_space')
        if self.live_source_probes and self._manual_permit_id != old.permit_id:
            return await self._start_probe(
                old=old,
                infohash=infohash,
                metadata_sha256=metadata_sha256,
                selected_files=selected_files,
                exact_bytes=exact_bytes,
                torrent=torrent,
                reported_seeders=reported_seeders,
                quality_rank=quality_rank,
                episode_catalog=episode_catalog,
                allow_absolute=allow_absolute,
            )
        self.health_store.mark_replacing(old.permit_id)
        await self._source_state(old, "stop")
        try:
            capacity = await self.capacity_provider()
            if self._manual_permit_id == old.permit_id and not (
                self.permits.probe_reservation_active(old)
            ):
                return 'reservation_inactive'
            chosen = self.permits.replace_confirmed(
                old.token,
                infohash=infohash,
                metadata_sha256=metadata_sha256,
                selected_files=selected_files,
                budget_bytes=exact_bytes,
                capacity=capacity,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                reported_seeders=reported_seeders,
                torrent_content=torrent if self.torrent_store is not None else None,
                quality_rank=quality_rank,
                episode_files=episode_files,
                episode_catalog=episode_catalog,
                allow_absolute=allow_absolute,
                # Only an explicit operator request may select a different visual
                # tier. The selected named profile has already validated this source.
                allow_quality_change=(
                    self._manual_permit_id == old.permit_id
                    and self.release_policy is not None
                    and self.release_policy.profile_name is not None
                ),
            )
        except Exception:
            # The old permit still exists unless the atomic database transition
            # committed; only then would restarting it violate the new queue.
            current = self.permits.get(old.token)
            if current is not None and current.state == "confirmed":
                await self._resume_if_safe(old)
            raise
        await self._add_verified_torrent(chosen, torrent)
        LOGGER.info(
            "Replaced stalled source %s with %s for %s",
            old.infohash,
            chosen.infohash,
            old.reservation_id,
        )
        return "replaced"

    async def _add_verified_torrent(self, permit, torrent: bytes) -> None:
        assert self.gateway_url is not None and self.arr_token is not None
        response = await self.client.post(
            f"{self.gateway_url}/api/v2/torrents/add",
            headers={"X-Arr-Token": self.arr_token, "X-Admission-Permit": permit.token},
            data={"category": permit.category, "savepath": permit.destination, "stopped": "false"},
            files={"torrents": ("verified.torrent", torrent, "application/x-bittorrent")},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("accepted") is not True:
            raise ValueError("gateway did not confirm replacement torrent")

    async def _retry_replacement(self, permit) -> str | None:
        if (
            permit.state != "authorized"
            or permit.reservation_id is None
            or not self.permits.had_superseded(permit.reservation_id, scope_key=permit.scope_key)
        ):
            return None
        if self.torrent_store is None or self.capacity_provider is None:
            return None
        torrent = self.torrent_store.get(permit)
        if torrent is None:
            return None
        capacity = await self.capacity_provider()
        # Recheck retained sources on every retry, even before the permit expires.
        try:
            permit = self.permits.renew_replacement_authorized(
                permit.token,
                capacity=capacity,
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        except PermissionError as error:
            if str(error) == "waiting_space":
                return "waiting_space"
            raise
        await self._add_verified_torrent(permit, torrent)
        return "replaced"

    async def _reconcile_uncertain_source(self, permit) -> str | None:
        if permit.state not in {"unknown", "dispatching"} or permit.reservation_id is None:
            return None
        assert self.gateway_url is not None and self.arr_token is not None
        response = await self.client.post(
            f"{self.gateway_url}/internal/reconcile-source",
            headers={"X-Arr-Token": self.arr_token},
            json={"permit_token": permit.token},
        )
        response.raise_for_status()
        result = response.json()
        state = result.get("state") if isinstance(result, dict) else None
        replacement = self.permits.had_superseded(permit.reservation_id, scope_key=permit.scope_key)
        if state == "confirmed":
            LOGGER.info("Reconciled torrent %s", permit.infohash)
            return "replaced" if replacement else "reconciled"
        if state == "missing":
            key = f"unknown:{permit.permit_id}"
            if time.monotonic() >= self._next_search.get(key, 0):
                LOGGER.warning(
                    "Torrent %s is uncertain and absent upstream; manual reconciliation required",
                    permit.infohash,
                )
                self._next_search[key] = time.monotonic() + 900
            return "replacement_uncertain" if replacement else "source_uncertain"
        raise ValueError("invalid source reconciliation response")

    def _trusted_download_url(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        url = urlsplit(value)
        trusted = urlsplit(self.prowlarr_url)
        return (
            url.scheme == trusted.scheme
            and url.netloc == trusted.netloc
            and url.username is None
            and url.password is None
            and not url.fragment
            and bool(re.fullmatch(
                re.escape(trusted.path.rstrip("/")) + r"/[0-9]+/download", url.path,
            ))
        )

    @staticmethod
    def _eligible_manifest(
        torrent: bytes,
        budget: int | None = None,
        *,
        allow_external_subtitle: bool = False,
        subtitle_policy: SubtitlePolicy | None = None,
    ) -> tuple[str, str, tuple[str, ...]] | None:
        try:
            inspected = inspect_torrent(torrent)
        except TorrentBytesError:
            return None
        videos = [
            item
            for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _VIDEO_SUFFIXES
            and not _is_sample_video(item.path)
            and not _is_movie_extra_video(item.path)
        ]
        brazilian_subtitles = [
            item
            for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
            and not _is_movie_extra_video(item.path)
            and is_brazilian_portuguese_subtitle(item.path)
        ]
        english_subtitles = [
            item
            for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
            and not _is_movie_extra_video(item.path)
            and is_english_subtitle(item.path)
        ]
        policy = subtitle_policy or SubtitlePolicy()
        by_language = {"pt-BR": brazilian_subtitles, "en-US": english_subtitles}
        subtitles = next(
            (
                [item for item in by_language[language] if policy.matches(item.path)]
                for language in policy.languages
                if any(policy.matches(item.path) for item in by_language[language])
            ),
            [],
        )
        if len(videos) != 1 or not subtitles and not allow_external_subtitle:
            return None
        return (
            inspected.infohash,
            inspected.metadata_sha256,
            tuple(item.path for item in (*videos, *subtitles)),
        )

    async def _metadata(self, url: str, *, allow_magnet: bool = True) -> bytes | None:
        try:
            async with self.client.stream("GET", url, follow_redirects=False) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not allow_magnet:
                        return None
                    infohash = magnet_infohash(response.headers.get("location"))
                    if infohash is None:
                        return None
                    cache_url = f"{_TORRENT_CACHE}/{infohash.upper()}.torrent"
                    cached = await self._metadata(cache_url, allow_magnet=False)
                    if cached is not None:
                        try:
                            if inspect_torrent(cached).infohash == infohash:
                                return merge_magnet_trackers(cached, response.headers["location"])
                        except TorrentBytesError:
                            pass
                    LOGGER.info("HTTP torrent metadata unavailable or invalid for %s", infohash)
                    if self.metadata_resolver is None:
                        return None
                    recovered = await self.metadata_resolver(response.headers["location"])
                    if recovered is not None:
                        try:
                            if inspect_torrent(recovered).infohash == infohash:
                                return merge_magnet_trackers(
                                    recovered, response.headers["location"],
                                )
                        except TorrentBytesError:
                            pass
                    LOGGER.info("Peer metadata unavailable for %s; no payload admitted", infohash)
                    return None
                if response.status_code != 200:
                    return None
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > _MAX_METADATA:
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_METADATA:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except (httpx.HTTPError, ValueError):
            return None

    async def _hardlink_import_enabled(self) -> bool:
        response = await self.client.get(
            f"{self.radarr_url}/api/v3/config/mediamanagement", headers=self.headers
        )
        response.raise_for_status()
        payload = response.json()
        return isinstance(payload, dict) and payload.get("copyUsingHardlinks") is True

    async def _prepare_animation_profile(self, movie) -> bool:
        """Remove Radarr's original-only rejection for this animated movie."""
        if movie.get("hasFile") is not False:
            return False
        response = await self.client.get(
            f"{self.radarr_url}/api/v3/qualityprofile", headers=self.headers,
        )
        response.raise_for_status()
        profiles = response.json()
        if not isinstance(profiles, list):
            raise ValueError("Radarr quality profile response is invalid")
        matches = [
            item for item in profiles if isinstance(item, dict)
            and item.get("name") == "HomeServer Animation"
        ]
        if len(matches) != 1:
            return False
        profile = matches[0]
        language = profile.get("language")
        if (
            type(profile.get("id")) is not int or profile["id"] <= 0
            or not isinstance(language, dict) or language.get("id") != -1
            or str(language.get("name", "")).casefold() != "any"
        ):
            return False
        if movie.get("qualityProfileId") == profile["id"]:
            return True
        endpoint = f"{self.radarr_url}/api/v3/movie/{movie['id']}"
        response = await self.client.put(
            endpoint, headers=self.headers,
            json={**movie, "qualityProfileId": profile["id"]},
        )
        response.raise_for_status()
        response = await self.client.get(endpoint, headers=self.headers)
        response.raise_for_status()
        readback = response.json()
        return (
            isinstance(readback, dict) and readback.get("id") == movie["id"]
            and readback.get("tmdbId") == movie["tmdbId"]
            and readback.get("qualityProfileId") == profile["id"]
        )

    def _replacement_paths_available(self, old, candidate_torrent: bytes) -> bool:
        """Protect paths belonging to every retained source for this slot."""
        if self.torrent_store is None:
            return False
        try:
            candidate = inspect_torrent(candidate_torrent)
        except TorrentBytesError:
            return False
        candidate_paths = {item.path for item in candidate.files}
        retained = [
            old,
            *(
                item
                for item in self.permits.list_source_history(
                    old.reservation_id, scope_key=old.scope_key
                )
                if item.state == "superseded"
            ),
        ]
        for source in retained:
            torrent = self.torrent_store.get(source)
            if torrent is None:
                return False
            try:
                if not candidate_paths.isdisjoint(
                    item.path for item in inspect_torrent(torrent).files
                ):
                    return False
            except TorrentBytesError:
                return False
        return True

    async def _prioritize_replacements(
        self,
        candidates: AsyncIterator[AcquisitionCandidate],
        *,
        old,
        reason: str | None,
        health: TorrentHealth | None,
        rank=release_rank,
    ) -> AsyncIterator[AcquisitionCandidate]:
        if reason is None:
            async for candidate in candidates:
                yield candidate
            return
        assert old is not None and health is not None
        peer_reason = "stalled" if self.live_source_probes else reason
        named_manual = (
            self._manual_permit_id == old.permit_id
            and self.release_policy is not None
            and self.release_policy.profile_name is not None
        )
        attempted = {
            source.infohash
            for source in self.permits.list_source_history(
                old.reservation_id, scope_key=old.scope_key
            )
        }
        unknown: list[AcquisitionCandidate] = []
        batch: list[AcquisitionCandidate] = []

        async def probe_batch():
            assert self.availability_probe is not None
            results = await asyncio.gather(
                *(self.availability_probe(item[3], item[1][0]) for item in batch)
            )
            available = []
            for candidate, seeds in zip(batch, results, strict=True):
                if seeds is None:
                    if _replacement_has_peers(candidate[0], peer_reason, health.num_seeds):
                        unknown.append(candidate)
                elif seeds > 0:
                    release, manifest, external, torrent = candidate
                    verified = {**release, "seeders": seeds}
                    if (
                        _replacement_has_peers(verified, peer_reason, health.num_seeds)
                        and rank(verified) is not None
                    ):
                        available.append((verified, manifest, external, torrent))
            return sorted(available, key=lambda item: rank(item[0]), reverse=True)

        async for candidate in candidates:
            release, manifest, _external, torrent = candidate
            infohash = manifest[0]
            if infohash in attempted or not self._replacement_paths_available(old, torrent):
                continue
            if named_manual and rank(release) is None:
                continue
            if self.live_source_probes:
                quality = canonical_quality(release)
                current = old.quality_rank
                if (
                    not named_manual and (
                        current is None
                        or quality is None
                        or any(
                            new < floor for new, floor in zip(quality[:2], current[:2], strict=True)
                        )
                    )
                    or edition_identity(str(release.get("title", "")))
                    != edition_identity(self._source_titles.get(old.permit_id, ""))
                ):
                    continue
            elif (
                not named_manual and self._manual_permit_id == old.permit_id
                and old.quality_rank is not None
            ):
                quality = canonical_quality(release)
                if quality is None or quality[:2] < old.quality_rank[:2]:
                    continue
            attempted.add(infohash)
            if self.availability_probe is None:
                if _replacement_has_peers(release, peer_reason, health.num_seeds):
                    yield candidate
                continue
            batch.append(candidate)
            if len(batch) == 12:
                available = await probe_batch()
                if available:
                    for item in available:
                        yield item
                batch.clear()
        if batch:
            available = await probe_batch()
            if available:
                for item in available:
                    yield item
        # These are reached only if measured-live candidates could not be admitted.
        # Unsupported/unknown trackers are never classified as an empty swarm.
        for candidate in unknown:
            yield candidate

    async def _eligible_movie_releases(
        self,
        ordered: list[dict[str, object]],
        *,
        replacement_reason: str | None,
        excluded_infohashes: set[str],
        runtime_minutes: float | None = None,
        animation: bool = False,
        rank=None,
    ) -> AsyncIterator[AcquisitionCandidate]:
        candidate_rank = rank or self._rank
        for release in ordered:
            if (
                candidate_rank(release) is None
                or replacement_reason and release.get("requiresNativeDecision") is True
                or animation and (release_seeders(release) or 0) <= 0
                or release.get("rejected") is not False
                and not (replacement_reason and _queue_only_rejection(release))
            ):
                continue
            reported = release.get("size")
            if isinstance(reported, bool) or not isinstance(reported, int) or reported <= 0:
                continue
            claimed_hash = release.get("infoHash")
            if isinstance(claimed_hash, str) and claimed_hash.lower() in excluded_infohashes:
                continue
            url = release.get("downloadUrl")
            if not self._trusted_download_url(url):
                continue
            torrent = await self._metadata(url)
            if torrent is None:
                continue
            manifest = self._eligible_manifest(
                torrent,
                allow_external_subtitle=self.subtitle_source is not None,
                subtitle_policy=self.subtitle_policy,
            )
            if manifest is None:
                continue
            if not self._acceptable_video_size(release, torrent, runtime_minutes):
                continue
            infohash, digest, files = manifest
            if (
                infohash in excluded_infohashes
                or claimed_hash
                and (not isinstance(claimed_hash, str) or claimed_hash.lower() != infohash)
            ):
                continue
            yield (
                release,
                (infohash, digest, files, inspect_torrent(torrent).total_bytes),
                None,
                torrent,
            )

    async def _acquire(self, media_key: str, reservation_id: str) -> str:
        reservation = self.repository.active_reservation(reservation_id)
        if reservation is None or reservation["media_key"] != media_key:
            return "reservation_inactive"
        existing = self.permits.get_for_reservation(reservation_id)
        if existing is None or not (
            existing.state == "authorized" and self.permits.had_superseded(reservation_id)
        ):
            self.permits.retire_expired_authorized(reservation_id)
            existing = self.permits.get_for_reservation(reservation_id)
        if existing is None and self.permits.had_superseded(reservation_id):
            return "replacement_missing_manual"
        replacement_reason: str | None = None
        old_health: TorrentHealth | None = None
        if existing is not None:
            reconciled = await self._reconcile_uncertain_source(existing)
            if reconciled is not None:
                return reconciled
            if existing.state == "confirmed":
                probe_state = (
                    await self._monitor_probe(existing)
                    if self._manual_permit_id != existing.permit_id else None
                )
                if probe_state is not None:
                    return probe_state
                replacement_reason, old_health = await self._source_status(existing)
                if replacement_reason is None:
                    return "already_permitted"
            elif existing.state != "authorized" or reservation_id in self._posted:
                return "already_permitted"
            if (
                existing.state == "authorized"
                and datetime.now(UTC) >= existing.expires_at
                and not self.permits.had_superseded(reservation_id)
            ):
                return "permit_expired"
            retried = await self._retry_replacement(existing)
            if retried is not None:
                return retried
        now = time.monotonic()
        if now < self._next_search.get(reservation_id, 0):
            return "search_deferred"
        self._next_search[reservation_id] = now + (
            self.source_retry_seconds if replacement_reason else self.retry_seconds
        )
        match = re.fullmatch(r"movie:tmdb:([0-9]+)", media_key)
        if match is None:
            return "unsupported_media"
        settings = await self.client.get(
            f"{self.radarr_url}/api/v3/config/downloadclient", headers=self.headers
        )
        settings.raise_for_status()
        settings_payload = settings.json()
        if (
            not isinstance(settings_payload, dict)
            or settings_payload.get("enableCompletedDownloadHandling") is not False
        ):
            return "import_guard"
        if not await self._hardlink_import_enabled():
            return "import_guard"
        movies = await self.client.get(
            f"{self.radarr_url}/api/v3/movie",
            params={"tmdbId": match.group(1)},
            headers=self.headers,
        )
        movies.raise_for_status()
        payload = movies.json()
        if not isinstance(payload, list):
            raise ValueError("Radarr movie lookup response is invalid")
        movie = next(
            (
                item
                for item in payload
                if isinstance(item, dict)
                and item.get("tmdbId") == int(match.group(1))
                and isinstance(item.get("id"), int)
            ),
            None,
        )
        if movie is None:
            return "movie_not_in_radarr"
        if movie.get("hasFile"):
            return "already_imported"
        if not await self._prepare_download_profile(movie):
            return "quality_profile_unavailable"
        if (
            self.release_policy is not None and self.release_policy.profile_name is None
            and is_animation(movie)
            and not await self._prepare_animation_profile(movie)
        ):
            return "animation_profile_unavailable"

        def rank(release):
            return self._rank(
                release, original_language=movie.get("originalLanguage"),
                animation=is_animation(movie),
            )

        releases = await self.client.get(
            f"{self.radarr_url}/api/v3/release",
            params={"movieId": movie["id"]},
            headers=self.headers,
            timeout=self.search_timeout_seconds,
        )
        releases.raise_for_status()
        items = releases.json()
        if not isinstance(items, list):
            raise ValueError("Radarr release response is invalid")
        items = await self._discover_dubbed_releases(movie, items)
        ordered = sorted(
            (item for item in items if isinstance(item, dict)),
            key=lambda item: rank(item) or (0, 0, 0, 0, 0, 0, 0),
            reverse=True,
        )
        waiting_space = False
        excluded = (
            {source.infohash for source in self.permits.list_source_history(reservation_id)}
            if replacement_reason
            else set()
        )

        async def eligible_for_indexer(group):
            nonlocal waiting_space
            async for candidate in self._eligible_movie_releases(
                group,
                replacement_reason=replacement_reason,
                excluded_infohashes=excluded,
                runtime_minutes=movie.get("runtime"),
                animation=is_animation(movie),
                rank=rank,
            ):
                if existing is None and self.capacity_provider is not None:
                    capacity = await self.capacity_provider()
                    available = self.permits.available_bytes(capacity)
                    if candidate[1][3] > available:
                        waiting_space = True
                        continue
                yield candidate

        candidates = self._preferred_indexer_candidates(
            ordered,
            eligible=eligible_for_indexer,
            rank=rank,
            transform=lambda candidates: self._prioritize_replacements(
                candidates, old=existing, reason=replacement_reason, health=old_health, rank=rank,
            ),
        )
        async for candidate in candidates:
            release, manifest, _, torrent = candidate
            infohash, metadata_sha256, selected_files, exact_bytes = manifest
            if replacement_reason is not None:
                assert existing is not None
                try:
                    return await self._dispatch_replacement(
                        old=existing,
                        infohash=infohash,
                        metadata_sha256=metadata_sha256,
                        selected_files=selected_files,
                        exact_bytes=exact_bytes,
                        torrent=torrent,
                        reported_seeders=release_seeders(release),
                        quality_rank=canonical_quality(release),
                    )
                except PermissionError as error:
                    if str(error) == "waiting_space":
                        waiting_space = True
                        continue
                    return str(error)
            if existing is not None:
                if (
                    existing.infohash != infohash
                    or existing.metadata_sha256 != metadata_sha256
                    or existing.category != "radarr"
                    or not self.permits.placement_valid(existing)
                    or existing.budget_bytes is None
                    or existing.budget_bytes < exact_bytes
                    or existing.selected_files != selected_files
                ):
                    continue
                chosen_permit = existing
            else:
                try:
                    capacity = await self.capacity_provider() if self.capacity_provider else None
                    chosen_permit = self.permits.issue(
                        infohash=infohash,
                        metadata_sha256=metadata_sha256,
                        destination="/data/torrents",
                        category="radarr",
                        reservation_id=reservation_id,
                        selected_files=selected_files,
                        budget_bytes=exact_bytes,
                        capacity=capacity,
                        expires_at=datetime.now(UTC) + timedelta(minutes=30),
                        reported_seeders=release_seeders(release),
                    )
                except PermissionError as error:
                    if str(error) == "waiting_space":
                        waiting_space = True
                        continue
                    return str(error)
                except sqlite3.IntegrityError:
                    return "already_permitted"
            if self.torrent_store is not None:
                quality = canonical_quality(release)
                if quality is not None:
                    self.permits.set_quality(chosen_permit.token, quality)
                self.torrent_store.put(chosen_permit, torrent)
            if self.permits.had_superseded(reservation_id):
                retried = await self._retry_replacement(chosen_permit)
                return retried or "replacement_metadata_unavailable"
            if not await self._dispatch_release(
                release, chosen_permit, original_language=movie.get("originalLanguage"),
            ):
                continue
            self._posted.add(reservation_id)
            LOGGER.info("Radarr grab requested for %s, reservation %s", media_key, reservation_id)
            return "grabbed"
        if replacement_reason == "replacing" and existing is not None:
            resumed = await self._resume_if_safe(existing)
            if resumed != "resumed":
                return resumed
        return "waiting_space" if waiting_space else (
            "search_incomplete" if self._dubbed_search_incomplete.get() else "no_eligible_release"
        )
