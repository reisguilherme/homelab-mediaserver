"""Choose an inspectable movie release and authorize one Radarr grab."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from homeserver_control.domain.magnet import magnet_infohash
from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore

from .capacity_evidence import CapacityEvidence
from .release_quality import release_rank
from .source_health import SourceHealthStore, TorrentHealth
from .subdl import SubDLSource
from .subtitle_language import is_brazilian_portuguese_subtitle, is_english_subtitle

LOGGER = logging.getLogger(__name__)
_VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".mov"}
_SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt"}
_MAX_METADATA = 16 * 1024 * 1024
_TORRENT_CACHE = "https://itorrents.net/torrent"


def _queue_only_rejection(release: dict[str, object]) -> bool:
    """Ignore only Arr's existing-queue veto while validating a replacement."""
    reasons = release.get("rejections")
    return (
        release.get("rejected") is True
        and isinstance(reasons, list) and bool(reasons)
        and all(isinstance(item, str)
                and item.startswith("Release in queue already meets cutoff:")
                for item in reasons)
    )


def _replacement_has_peers(release: dict[str, object], reason: str,
                           current_seeds: int) -> bool:
    reported = release.get("seeders")
    return (
        isinstance(reported, int) and not isinstance(reported, bool)
        and (reported >= 1 if reason in {"stalled", "replacing"}
             else reported >= max(5, current_seeds * 2))
    )


def _is_sample_video(path: str) -> bool:
    parsed = PurePosixPath(path)
    return (
        any(part.lower() == "sample" for part in parsed.parts[:-1])
        or bool(re.search(r"(?:^|[._ -])sample$", parsed.stem.lower()))
    )


class MovieAcquirer:
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
        self.subtitle_source = subtitle_source
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
        self._next_search: dict[str, float] = {}
        self._next_health_check: dict[str, float] = {}
        self._posted: set[str] = set()

    async def _source_status(self, permit) -> tuple[str | None, TorrentHealth | None]:
        if self.health_store is None or self.gateway_url is None or self.arr_token is None:
            return None, None
        now = time.monotonic()
        if now < self._next_health_check.get(permit.permit_id, 0):
            return None, None
        self._next_health_check[permit.permit_id] = now + 60
        response = await self.client.get(
            f"{self.gateway_url}/internal/torrent-health",
            headers={"X-Arr-Token": self.arr_token,
                     "X-Admission-Permit": permit.token},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("invalid gateway source health")
        health = TorrentHealth.from_mapping(payload)
        if health.infohash != permit.infohash:
            raise ValueError("gateway source identity changed")
        return self.health_store.observe(permit.permit_id, health, now=time.time()), health

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
        if self.permits.pending_bytes(
            capacity, include_infohash=permit.infohash
        ) > capacity.free_bytes:
            LOGGER.warning("Keeping source %s stopped because space is insufficient",
                           permit.infohash)
            return "waiting_space"
        await self._source_state(permit, "start")
        assert self.health_store is not None
        self.health_store.clear_replacing(permit.permit_id)
        return "resumed"

    async def _dispatch_replacement(
        self, *, old, infohash: str, metadata_sha256: str,
        selected_files: tuple[str, ...], exact_bytes: int, torrent: bytes,
    ) -> str:
        assert self.health_store is not None
        assert self.gateway_url is not None and self.arr_token is not None
        assert self.capacity_provider is not None
        self.health_store.mark_replacing(old.permit_id)
        await self._source_state(old, "stop")
        try:
            capacity = await self.capacity_provider()
            chosen = self.permits.replace_confirmed(
                old.token, infohash=infohash, metadata_sha256=metadata_sha256,
                selected_files=selected_files, budget_bytes=exact_bytes,
                capacity=capacity, expires_at=datetime.now(UTC) + timedelta(minutes=30),
            )
        except Exception:
            # The old permit still exists unless the atomic database transition
            # committed; only then would restarting it violate the new queue.
            current = self.permits.get(old.token)
            if current is not None and current.state == "confirmed":
                await self._resume_if_safe(old)
            raise
        if self.torrent_store is not None:
            self.torrent_store.put(chosen, torrent)
        await self._add_verified_torrent(chosen, torrent)
        LOGGER.info("Replaced stalled source %s with %s for %s",
                    old.infohash, chosen.infohash, old.reservation_id)
        return "replaced"

    async def _add_verified_torrent(self, permit, torrent: bytes) -> None:
        assert self.gateway_url is not None and self.arr_token is not None
        response = await self.client.post(
            f"{self.gateway_url}/api/v2/torrents/add",
            headers={"X-Arr-Token": self.arr_token,
                     "X-Admission-Permit": permit.token},
            data={"category": permit.category, "savepath": permit.destination,
                  "stopped": "false"},
            files={"torrents": ("verified.torrent", torrent, "application/x-bittorrent")},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("accepted") is not True:
            raise ValueError("gateway did not confirm replacement torrent")

    async def _retry_replacement(self, permit) -> str | None:
        if (
            permit.state != "authorized" or permit.reservation_id is None
            or not self.permits.had_superseded(
                permit.reservation_id, scope_key=permit.scope_key
            )
        ):
            return None
        if self.torrent_store is None or self.capacity_provider is None:
            return None
        torrent = self.torrent_store.get(permit)
        if torrent is None:
            return None
        capacity = await self.capacity_provider()
        if permit.expires_at <= datetime.now(UTC) + timedelta(minutes=2):
            try:
                permit = self.permits.renew_replacement_authorized(
                    permit.token, capacity=capacity,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                )
            except PermissionError as error:
                if str(error) == "waiting_space":
                    return "waiting_space"
                raise
        elif self.permits.pending_bytes(capacity) > capacity.free_bytes:
            return "waiting_space"
        await self._add_verified_torrent(permit, torrent)
        return "replaced"

    async def _reconcile_uncertain_source(self, permit) -> str | None:
        if (
            permit.state not in {"unknown", "dispatching"}
            or permit.reservation_id is None
        ):
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
        replacement = self.permits.had_superseded(
            permit.reservation_id, scope_key=permit.scope_key
        )
        if state == "confirmed":
            LOGGER.info("Reconciled torrent %s", permit.infohash)
            return "replaced" if replacement else "reconciled"
        if state == "missing":
            key = f"unknown:{permit.permit_id}"
            if time.monotonic() >= self._next_search.get(key, 0):
                LOGGER.warning(
                    "Torrent %s is uncertain and absent upstream; "
                    "manual reconciliation required", permit.infohash
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
            and bool(re.fullmatch(r"/[0-9]+/download", url.path))
        )

    @staticmethod
    def _eligible_manifest(
        torrent: bytes, budget: int | None = None, *, allow_external_subtitle: bool = False
    ) -> tuple[str, str, tuple[str, ...]] | None:
        try:
            inspected = inspect_torrent(torrent)
        except TorrentBytesError:
            return None
        videos = [
            item for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _VIDEO_SUFFIXES
            and not _is_sample_video(item.path)
        ]
        brazilian_subtitles = [
            item for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
            and is_brazilian_portuguese_subtitle(item.path)
        ]
        english_subtitles = [
            item for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
            and is_english_subtitle(item.path)
        ]
        subtitles = brazilian_subtitles or english_subtitles
        if (
            len(videos) != 1
            or not subtitles and not allow_external_subtitle
        ):
            return None
        return inspected.infohash, inspected.metadata_sha256, tuple(
            item.path for item in (*videos, *subtitles)
        )

    async def _metadata(
        self, url: str, *, allow_magnet: bool = True
    ) -> bytes | None:
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
                    if cached is None:
                        return None
                    try:
                        if inspect_torrent(cached).infohash != infohash:
                            return None
                    except TorrentBytesError:
                        return None
                    return cached
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

    def _replacement_paths_available(self, old, candidate_torrent: bytes) -> bool:
        """Do not let a replacement write any path held by the stopped torrent."""
        if self.torrent_store is None:
            return False
        old_torrent = self.torrent_store.get(old)
        if old_torrent is None:
            return False
        try:
            previous = inspect_torrent(old_torrent)
            candidate = inspect_torrent(candidate_torrent)
        except TorrentBytesError:
            return False
        return {item.path for item in previous.files}.isdisjoint(
            item.path for item in candidate.files
        )

    async def acquire(self, media_key: str, reservation_id: str) -> str:
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
                if self.permits.had_superseded(reservation_id):
                    return "already_permitted"
                replacement_reason, old_health = await self._source_status(existing)
                if replacement_reason is None:
                    return "already_permitted"
            elif existing.state != "authorized" or reservation_id in self._posted:
                return "already_permitted"
            if (existing.state == "authorized" and datetime.now(UTC) >= existing.expires_at
                    and not self.permits.had_superseded(reservation_id)):
                return "permit_expired"
            retried = await self._retry_replacement(existing)
            if retried is not None:
                return retried
        now = time.monotonic()
        if now < self._next_search.get(reservation_id, 0):
            return "search_deferred"
        self._next_search[reservation_id] = now + self.retry_seconds
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
        movies = (await self.client.get(
            f"{self.radarr_url}/api/v3/movie", params={"tmdbId": match.group(1)},
            headers=self.headers,
        ))
        movies.raise_for_status()
        payload = movies.json()
        if not isinstance(payload, list):
            raise ValueError("Radarr movie lookup response is invalid")
        movie = next(
            (item for item in payload if isinstance(item, dict)
             and item.get("tmdbId") == int(match.group(1)) and isinstance(item.get("id"), int)),
            None,
        )
        if movie is None:
            return "movie_not_in_radarr"
        if movie.get("hasFile"):
            return "already_imported"
        releases = await self.client.get(
            f"{self.radarr_url}/api/v3/release", params={"movieId": movie["id"]},
            headers=self.headers, timeout=90.0,
        )
        releases.raise_for_status()
        items = releases.json()
        if not isinstance(items, list):
            raise ValueError("Radarr release response is invalid")
        ordered = sorted(
            (item for item in items if isinstance(item, dict)),
            key=lambda item: release_rank(item) or (0, 0, 0, 0, 0),
            reverse=True,
        )
        waiting_space = False
        for release in ordered:
            if (release_rank(release) is None or
                    release.get("rejected") is not False and not (
                        replacement_reason and _queue_only_rejection(release)
                    )):
                continue
            if replacement_reason and (
                existing is None or old_health is None or not _replacement_has_peers(
                    release, replacement_reason, old_health.num_seeds
                )
            ):
                continue
            reported = release.get("size")
            if isinstance(reported, bool) or not isinstance(reported, int) or reported <= 0:
                continue
            url = release.get("downloadUrl")
            if not self._trusted_download_url(url):
                continue
            torrent = await self._metadata(url)
            if torrent is None:
                continue
            manifest = self._eligible_manifest(
                torrent, allow_external_subtitle=self.subtitle_source is not None
            )
            if manifest is None:
                continue
            infohash, metadata_sha256, selected_files = manifest
            if replacement_reason and existing is not None and not (
                self._replacement_paths_available(existing, torrent)
            ):
                continue
            exact_bytes = inspect_torrent(torrent).total_bytes
            claimed_hash = release.get("infoHash")
            if claimed_hash and (
                not isinstance(claimed_hash, str) or claimed_hash.lower() != infohash
            ):
                continue
            has_ptbr_sidecar = any(
                PurePosixPath(name).suffix.lower() in _SUBTITLE_SUFFIXES
                and is_brazilian_portuguese_subtitle(name)
                for name in selected_files
            )
            if not has_ptbr_sidecar and self.subtitle_source is not None:
                assert self.subtitle_store is not None
                title = release.get("title")
                if not isinstance(title, str):
                    if len(selected_files) == 1:
                        continue
                else:
                    content = await self.subtitle_source.fetch(
                        tmdb_id=int(match.group(1)), release_title=title
                    )
                    if content is not None:
                        self.subtitle_store.put(reservation_id, None, infohash, content)
            if replacement_reason is not None:
                assert existing is not None
                if infohash == existing.infohash:
                    continue
                try:
                    return await self._dispatch_replacement(
                        old=existing, infohash=infohash,
                        metadata_sha256=metadata_sha256,
                        selected_files=selected_files, exact_bytes=exact_bytes,
                        torrent=torrent,
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
                    or existing.destination != "/data/torrents"
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
                        infohash=infohash, metadata_sha256=metadata_sha256,
                        destination="/data/torrents", category="radarr",
                        reservation_id=reservation_id, selected_files=selected_files,
                        budget_bytes=exact_bytes, capacity=capacity,
                        expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    )
                except PermissionError as error:
                    if str(error) == "waiting_space":
                        waiting_space = True
                        continue
                    return str(error)
                except sqlite3.IntegrityError:
                    return "already_permitted"
            if self.torrent_store is not None:
                self.torrent_store.put(chosen_permit, torrent)
            if self.permits.had_superseded(reservation_id):
                retried = await self._retry_replacement(chosen_permit)
                return retried or "replacement_metadata_unavailable"
            response = await self.client.post(
                f"{self.radarr_url}/api/v3/release",
                headers=self.headers,
                json={**release, "downloadClientId": 1},
            )
            response.raise_for_status()
            self._posted.add(reservation_id)
            LOGGER.info("Radarr grab requested for %s, reservation %s", media_key, reservation_id)
            return "grabbed"
        if replacement_reason == "replacing" and existing is not None:
            resumed = await self._resume_if_safe(existing)
            if resumed != "resumed":
                return resumed
        return "waiting_space" if waiting_space else "no_eligible_release"
