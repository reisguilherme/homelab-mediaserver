"""Authorize one inspected Sonarr episode at a time under a season reservation."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import httpx

from homeserver_control.domain.policy import EPISODE_LIMIT_BYTES
from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore

from .acquisition import _SUBTITLE_SUFFIXES, _VIDEO_SUFFIXES, MovieAcquirer
from .release_quality import release_rank
from .subdl import SubDLSource
from .subtitle_language import is_brazilian_portuguese_subtitle

LOGGER = logging.getLogger(__name__)
_SEASON_KEY = re.compile(r"season:tmdb:([1-9][0-9]*):([0-9]+)")
_WEB_DL = re.compile(r"(?<![a-z0-9])web[ ._-]*dl(?![a-z0-9])", re.I)
_WEB = re.compile(r"(?<![a-z0-9])web(?![a-z0-9])", re.I)
_REMUX = re.compile(r"(?<![a-z0-9])remux(?![a-z0-9])", re.I)


def _episode_tag(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def _single_episode_name(value: str, season: int, episode: int) -> bool:
    tag = _episode_tag(season, episode)
    return bool(re.search(rf"(?<![a-z0-9]){tag}(?![a-z0-9]|[-_. ]?e[0-9])", value, re.I))


def _series_rank(release: dict[str, object]) -> tuple[int, int, int, int, int] | None:
    quality = release.get("quality")
    detail = quality.get("quality") if isinstance(quality, dict) else None
    title = release.get("title")
    if not isinstance(detail, dict) or not isinstance(title, str):
        return None
    normalized = dict(detail)
    if detail.get("source") == "web":
        if not str(detail.get("name", "")).lower().startswith("webdl"):
            return None
        if not (_WEB_DL.search(title) or _WEB.search(title)):
            return None
        normalized.update(source="webdl", modifier="none")
    elif detail.get("source") == "bluray":
        normalized["modifier"] = "remux" if _REMUX.search(title) else "none"
    return release_rank({**release, "quality": {"quality": normalized}})


class SeriesAcquirer(MovieAcquirer):
    """Use MovieAcquirer's bounded torrent fetch, with Sonarr episode policy."""

    def __init__(
        self, *, repository: ReservationRepository, permits: PermitRegistry,
        sonarr_url: str, sonarr_api_key: str, prowlarr_url: str,
        client: httpx.AsyncClient | None = None, retry_seconds: int = 900,
        subtitle_source: SubDLSource | None = None,
        subtitle_store: SubtitleArtifactStore | None = None,
    ) -> None:
        super().__init__(
            repository=repository, permits=permits, radarr_url=sonarr_url,
            radarr_api_key=sonarr_api_key, prowlarr_url=prowlarr_url,
            client=client, retry_seconds=retry_seconds,
            subtitle_source=subtitle_source, subtitle_store=subtitle_store,
        )
        self.sonarr_url = sonarr_url.rstrip("/")

    @staticmethod
    def _eligible_episode_manifest(
        torrent: bytes, *, season: int, episode: int,
        allow_external_subtitle: bool = False,
    ) -> tuple[str, str, tuple[str, ...], int] | None:
        try:
            inspected = inspect_torrent(torrent)
        except TorrentBytesError:
            return None
        if inspected.total_bytes > EPISODE_LIMIT_BYTES:
            return None
        videos = [item for item in inspected.files
                  if PurePosixPath(item.path).suffix.lower() in _VIDEO_SUFFIXES]
        subtitles = [item for item in inspected.files
                     if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
                     and is_brazilian_portuguese_subtitle(item.path)]
        if (
            len(videos) != 1 or not 0 < videos[0].length <= EPISODE_LIMIT_BYTES
            or not _single_episode_name(videos[0].path, season, episode)
            or not subtitles and not allow_external_subtitle
        ):
            return None
        return (
            inspected.infohash, inspected.metadata_sha256,
            tuple(item.path for item in (*videos, *subtitles)),
            inspected.total_bytes,
        )

    async def _eligible_release(
        self, *, series_id: int, episode_id: int, season: int, episode: int,
        tmdb_id: int,
    ) -> tuple[dict[str, object], tuple[str, str, tuple[str, ...], int], bytes | None] | None:
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/release",
            params={"seriesId": series_id, "episodeId": episode_id},
            headers=self.headers, timeout=90.0,
        )
        response.raise_for_status()
        releases = response.json()
        if not isinstance(releases, list):
            raise ValueError("Sonarr release response is invalid")
        ranked = sorted(
            (item for item in releases if isinstance(item, dict)),
            key=lambda item: _series_rank(item) or (0, 0, 0, 0, 0),
            reverse=True,
        )
        for release in ranked:
            if (
                release.get("rejected") is not False
                or _series_rank(release) is None
                or not isinstance(release.get("title"), str)
                or not _single_episode_name(release["title"], season, episode)
                or not isinstance(release.get("size"), int)
                or not 0 < release["size"] <= EPISODE_LIMIT_BYTES
                or not self._trusted_download_url(release.get("downloadUrl"))
            ):
                continue
            torrent = await self._metadata(release["downloadUrl"])
            if torrent is None:
                continue
            manifest = self._eligible_episode_manifest(
                torrent, season=season, episode=episode,
                allow_external_subtitle=self.subtitle_source is not None,
            )
            if manifest is None:
                continue
            claimed = release.get("infoHash")
            if claimed and (not isinstance(claimed, str) or claimed.lower() != manifest[0]):
                continue
            external: bytes | None = None
            if not any(PurePosixPath(name).suffix.lower() in _SUBTITLE_SUFFIXES
                       for name in manifest[2]):
                assert self.subtitle_source is not None
                external = await self.subtitle_source.fetch(
                    tmdb_id=tmdb_id, release_title=release["title"],
                    season=season, episode=episode,
                )
                if external is None:
                    continue
            return release, manifest, external
        return None

    async def acquire(self, media_key: str, reservation_id: str) -> str:
        reservation = self.repository.active_reservation(reservation_id)
        if reservation is None or reservation["media_key"] != media_key:
            return "reservation_inactive"
        match = _SEASON_KEY.fullmatch(media_key)
        if match is None:
            return "unsupported_media"
        tmdb_id, season = int(match.group(1)), int(match.group(2))
        settings = await self.client.get(
            f"{self.sonarr_url}/api/v3/config/downloadclient", headers=self.headers
        )
        settings.raise_for_status()
        if settings.json().get("enableCompletedDownloadHandling") is not False:
            return "import_guard"
        series_response = await self.client.get(
            f"{self.sonarr_url}/api/v3/series", headers=self.headers
        )
        series_response.raise_for_status()
        series_payload = series_response.json()
        if not isinstance(series_payload, list):
            raise ValueError("Sonarr series response is invalid")
        series = next(
            (item for item in series_payload if isinstance(item, dict)
             and item.get("tmdbId") == tmdb_id and item.get("monitored") is True
             and isinstance(item.get("id"), int)),
            None,
        )
        if series is None:
            return "series_not_monitored"
        episodes_response = await self.client.get(
            f"{self.sonarr_url}/api/v3/episode",
            params={"seriesId": series["id"]}, headers=self.headers,
        )
        episodes_response.raise_for_status()
        episodes_payload = episodes_response.json()
        if not isinstance(episodes_payload, list):
            raise ValueError("Sonarr episode response is invalid")
        episode_rows = sorted(
            (item for item in episodes_payload if isinstance(item, dict)
             and item.get("seasonNumber") == season
             and isinstance(item.get("episodeNumber"), int)
             and isinstance(item.get("id"), int)
             and item.get("monitored") is True),
            key=lambda item: item["episodeNumber"],
        )
        if not episode_rows:
            return "season_not_monitored"
        now_utc = datetime.now(UTC)
        pending: list[dict[str, object]] = []
        future = False
        for item in episode_rows:
            air_date = item.get("airDateUtc")
            if not isinstance(air_date, str):
                future = True
                continue
            try:
                aired = datetime.fromisoformat(air_date.replace("Z", "+00:00")) <= now_utc
            except ValueError:
                future = True
                continue
            if not aired:
                future = True
            elif item.get("hasFile") is not True:
                pending.append(item)
        if not pending:
            return "waiting_episodes" if future else "already_imported"
        for item in pending:
            number = item["episodeNumber"]
            scope = _episode_tag(season, number)
            existing = self.permits.get_for_reservation(reservation_id, scope_key=scope)
            if existing is not None and existing.state != "authorized":
                continue
            if (reservation_id, scope) in self._posted:
                continue
            if time.monotonic() < self._next_search.get(f"{reservation_id}:{scope}", 0):
                continue
            self._next_search[f"{reservation_id}:{scope}"] = time.monotonic() + self.retry_seconds
            candidate = await self._eligible_release(
                series_id=series["id"], episode_id=item["id"],
                season=season, episode=number, tmdb_id=tmdb_id,
            )
            if candidate is None:
                return "no_eligible_release"
            release, (infohash, digest, files, bytes_total), external = candidate
            if external is not None:
                assert self.subtitle_store is not None
                self.subtitle_store.put(reservation_id, scope, infohash, external)
            if existing is not None:
                if (
                    existing.infohash != infohash
                    or existing.metadata_sha256 != digest
                    or existing.selected_files != files
                    or existing.budget_bytes != bytes_total
                ):
                    return "permit_conflict"
            else:
                try:
                    self.permits.issue(
                        infohash=infohash, metadata_sha256=digest,
                        destination="/data/torrents", category="sonarr",
                        reservation_id=reservation_id, scope_key=scope,
                        selected_files=files, budget_bytes=bytes_total,
                        expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    )
                except sqlite3.IntegrityError:
                    return "already_permitted"
            response = await self.client.post(
                f"{self.sonarr_url}/api/v3/release", headers=self.headers,
                json={**release, "downloadClientId": 1},
            )
            response.raise_for_status()
            self._posted.add((reservation_id, scope))
            LOGGER.info("Sonarr grab requested for %s %s", media_key, scope)
            return "grabbed"
        return "waiting_episodes"
