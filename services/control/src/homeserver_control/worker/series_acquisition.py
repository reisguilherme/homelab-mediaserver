"""Authorize one inspected Sonarr episode at a time under a season reservation."""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import httpx

from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore

from .acquisition import _SUBTITLE_SUFFIXES, _VIDEO_SUFFIXES, MovieAcquirer, _is_sample_video
from .capacity_evidence import CapacityEvidence
from .release_quality import release_rank
from .subdl import SubDLSource
from .subtitle_language import is_brazilian_portuguese_subtitle, is_english_subtitle

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
        torrent_store: TorrentArtifactStore | None = None,
        capacity_provider: Callable[[], Awaitable[CapacityEvidence]] | None = None,
        gateway_url: str | None = None, arr_token: str | None = None,
    ) -> None:
        super().__init__(
            repository=repository, permits=permits, radarr_url=sonarr_url,
            radarr_api_key=sonarr_api_key, prowlarr_url=prowlarr_url,
            client=client, retry_seconds=retry_seconds,
            subtitle_source=subtitle_source, subtitle_store=subtitle_store,
            torrent_store=torrent_store,
            capacity_provider=capacity_provider,
        )
        self.sonarr_url = sonarr_url.rstrip("/")
        if bool(gateway_url) != bool(arr_token):
            raise ValueError("gateway URL and worker token must be configured together")
        self.gateway_url = gateway_url.rstrip("/") if gateway_url else None
        self.arr_token = arr_token

    async def _reconcile_existing_queue(
        self, *, episodes: list[dict[str, object]], season: int,
        reservation_id: str, active_episode_id: int | None,
        imported_episode_ids: set[int],
    ) -> str | None:
        if self.gateway_url is None or self.arr_token is None:
            return None
        changes: list[tuple[str, str, str]] = []
        seen_scopes: set[str] = set()
        for item in episodes:
            if item["seasonNumber"] != season or item["id"] in imported_episode_ids:
                continue
            scope = _episode_tag(season, item["episodeNumber"])
            if scope in seen_scopes:
                continue
            seen_scopes.add(scope)
            permit = self.permits.get_for_reservation(reservation_id, scope_key=scope)
            if permit is None or permit.state != "confirmed":
                continue
            action = "start" if item["id"] == active_episode_id else "stop"
            changes.append((action, permit.token, permit.infohash))
        # Stop later torrents before considering a capacity-checked start.
        for action, token, infohash in sorted(changes, key=lambda item: item[0] != "stop"):
            blocked_status = None
            if action == "start":
                if self.capacity_provider is None:
                    blocked_status = "capacity_unavailable"
                else:
                    try:
                        capacity = await self.capacity_provider()
                        pending_bytes = self.permits.pending_bytes(
                            capacity, include_infohash=infohash
                        )
                    except Exception as error:
                        LOGGER.warning(
                            "series capacity evidence unavailable: %s", type(error).__name__
                        )
                        blocked_status = "capacity_unavailable"
                    else:
                        if pending_bytes > capacity.free_bytes:
                            blocked_status = "waiting_space"
                if blocked_status is not None:
                    action = "stop"
            response = await self.client.post(
                f"{self.gateway_url}/internal/series-queue-state",
                headers={"X-Arr-Token": self.arr_token},
                json={"permit_token": token, "action": action},
            )
            response.raise_for_status()
            if blocked_status is not None:
                return blocked_status
        return None

    def _requested_seasons(self, tmdb_id: int) -> dict[int, str]:
        seasons: dict[int, str] = {}
        for reservation_id, _ in self.repository.active_seerr_requests():
            reservation = self.repository.active_reservation(reservation_id)
            if reservation is None:
                continue
            media_key = reservation.get("media_key")
            match = _SEASON_KEY.fullmatch(media_key) if isinstance(media_key, str) else None
            if match is not None and int(match.group(1)) == tmdb_id:
                number = int(match.group(2))
                if number > 0:
                    seasons[number] = reservation_id
        return seasons

    def _episode_imported(
        self, item: dict[str, object], reservations_by_season: dict[int, str]
    ) -> bool:
        if item.get("hasFile") is not True:
            return False
        season = item["seasonNumber"]
        episode = item["episodeNumber"]
        reservation_id = reservations_by_season.get(season)
        if reservation_id is None:
            return False
        permit = self.permits.get_for_reservation(
            reservation_id, scope_key=_episode_tag(season, episode)
        )
        return (
            permit is None
            or permit.state != "confirmed"
            or self.repository.episode_import_state(permit.permit_id) == "complete"
        )

    @staticmethod
    def _eligible_episode_manifest(
        torrent: bytes, *, season: int, episode: int,
        allow_external_subtitle: bool = False,
    ) -> tuple[str, str, tuple[str, ...], int] | None:
        try:
            inspected = inspect_torrent(torrent)
        except TorrentBytesError:
            return None
        videos = [item for item in inspected.files
                  if PurePosixPath(item.path).suffix.lower() in _VIDEO_SUFFIXES
                  and not _is_sample_video(item.path)]
        pt_br_subtitles = [item for item in inspected.files
                           if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
                           and is_brazilian_portuguese_subtitle(item.path)
                           and _single_episode_name(
                               PurePosixPath(item.path).name, season, episode
                           )]
        english_subtitles = [item for item in inspected.files
                             if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
                             and is_english_subtitle(item.path)
                             and _single_episode_name(
                                 PurePosixPath(item.path).name, season, episode
                             )]
        subtitles = pt_br_subtitles or english_subtitles
        if (
            len(videos) != 1 or videos[0].length <= 0
            or not _single_episode_name(PurePosixPath(videos[0].path).name, season, episode)
            or not subtitles and not allow_external_subtitle
        ):
            return None
        return (
            inspected.infohash, inspected.metadata_sha256,
            tuple(item.path for item in (*videos, *subtitles)),
            inspected.total_bytes,
        )

    async def _eligible_releases(
        self, *, series_id: int, episode_id: int, season: int, episode: int,
        tmdb_id: int,
    ) -> AsyncIterator[
        tuple[dict[str, object], tuple[str, str, tuple[str, ...], int], bytes | None, bytes]
    ]:
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
                or release["size"] <= 0
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
            yield release, manifest, external, torrent

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
        if not await self._hardlink_import_enabled():
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
        reservations_by_season = self._requested_seasons(tmdb_id)
        reservations_by_season.setdefault(season, reservation_id)
        chronological = sorted(
            (item for item in episodes_payload if isinstance(item, dict)
             and isinstance(item.get("seasonNumber"), int)
             and not isinstance(item["seasonNumber"], bool)
             and item["seasonNumber"] in reservations_by_season
             and isinstance(item.get("episodeNumber"), int)
             and not isinstance(item["episodeNumber"], bool)
             and item["episodeNumber"] > 0
             and isinstance(item.get("id"), int)),
            key=lambda item: (item["seasonNumber"], item["episodeNumber"]),
        )
        known_seasons = {item["seasonNumber"] for item in chronological}
        earlier_catalog_complete = all(
            number in known_seasons for number in reservations_by_season if number < season
        )
        imported_episode_ids = {
            item["id"] for item in chronological
            if self._episode_imported(item, reservations_by_season)
        }
        first_missing = next(
            (item for item in chronological if item["id"] not in imported_episode_ids), None
        )
        episode_rows = sorted(
            (item for item in episodes_payload if isinstance(item, dict)
             and item.get("seasonNumber") == season
             and isinstance(item.get("episodeNumber"), int)
             and isinstance(item.get("id"), int)
             and item.get("monitored") is True),
            key=lambda item: item["episodeNumber"],
        )
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
            elif item["id"] not in imported_episode_ids:
                pending.append(item)
        active_episode_id = (
            first_missing["id"]
            if earlier_catalog_complete and first_missing is not None
            and first_missing["seasonNumber"] == season
            and pending and pending[0]["id"] == first_missing["id"]
            else None
        )
        queue_status = await self._reconcile_existing_queue(
            episodes=chronological, season=season, reservation_id=reservation_id,
            active_episode_id=active_episode_id,
            imported_episode_ids=imported_episode_ids,
        )
        if queue_status is not None:
            return queue_status
        if (
            not earlier_catalog_complete
            or first_missing is not None and first_missing["seasonNumber"] < season
        ):
            return "waiting_previous_season"
        if not episode_rows:
            return "season_not_monitored"
        if not pending:
            return "waiting_episodes" if future else "already_imported"
        if active_episode_id is None:
            return "waiting_episodes"
        waiting_space = False
        no_source = False
        # A later episode can start only after Sonarr has imported the earliest missing one.
        for item in pending[:1]:
            number = item["episodeNumber"]
            scope = _episode_tag(season, number)
            self.permits.retire_expired_authorized(reservation_id, scope_key=scope)
            existing = self.permits.get_for_reservation(reservation_id, scope_key=scope)
            if item.get("hasFile") is True:
                return "waiting_episodes"
            if existing is not None and existing.state != "authorized":
                continue
            if (reservation_id, scope) in self._posted:
                continue
            if time.monotonic() < self._next_search.get(f"{reservation_id}:{scope}", 0):
                continue
            self._next_search[f"{reservation_id}:{scope}"] = time.monotonic() + self.retry_seconds
            found_candidate = False
            async for candidate in self._eligible_releases(
                series_id=series["id"], episode_id=item["id"],
                season=season, episode=number, tmdb_id=tmdb_id,
            ):
                found_candidate = True
                release, (infohash, digest, files, bytes_total), external, torrent = candidate
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
                        continue
                    chosen_permit = existing
                else:
                    try:
                        capacity = (
                            await self.capacity_provider() if self.capacity_provider else None
                        )
                        chosen_permit = self.permits.issue(
                            infohash=infohash, metadata_sha256=digest,
                            destination="/data/torrents", category="sonarr",
                            reservation_id=reservation_id, scope_key=scope,
                            selected_files=files, budget_bytes=bytes_total,
                            capacity=capacity,
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
                    f"{self.sonarr_url}/api/v3/release", headers=self.headers,
                    json={**release, "downloadClientId": 1},
                )
                response.raise_for_status()
                self._posted.add((reservation_id, scope))
                LOGGER.info("Sonarr grab requested for %s %s", media_key, scope)
                return "grabbed"
            if not found_candidate:
                no_source = True
        if waiting_space:
            return "waiting_space"
        return "no_eligible_release" if no_source else "waiting_episodes"
