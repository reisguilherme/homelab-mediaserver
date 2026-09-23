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
from .subdl import SubDLSource
from .subtitle_language import is_brazilian_portuguese_subtitle, is_english_subtitle

LOGGER = logging.getLogger(__name__)
_VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".mov"}
_SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt"}
_MAX_METADATA = 16 * 1024 * 1024
_TORRENT_CACHE = "https://itorrents.net/torrent"


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
        self._next_search: dict[str, float] = {}
        self._posted: set[str] = set()

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

    async def acquire(self, media_key: str, reservation_id: str) -> str:
        reservation = self.repository.active_reservation(reservation_id)
        if reservation is None or reservation["media_key"] != media_key:
            return "reservation_inactive"
        self.permits.retire_expired_authorized(reservation_id)
        existing = self.permits.get_for_reservation(reservation_id)
        if existing is not None:
            if existing.state != "authorized" or reservation_id in self._posted:
                return "already_permitted"
            if datetime.now(UTC) >= existing.expires_at:
                return "permit_expired"
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
            if release.get("rejected") is not False or release_rank(release) is None:
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
            response = await self.client.post(
                f"{self.radarr_url}/api/v3/release",
                headers=self.headers,
                json={**release, "downloadClientId": 1},
            )
            response.raise_for_status()
            self._posted.add(reservation_id)
            LOGGER.info("Radarr grab requested for %s, reservation %s", media_key, reservation_id)
            return "grabbed"
        return "waiting_space" if waiting_space else "no_eligible_release"
