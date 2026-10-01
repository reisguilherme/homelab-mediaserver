"""Prefetch a bounded episode window while reserving each torrent's actual bytes."""

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

from .acquisition import (
    _SUBTITLE_SUFFIXES,
    _VIDEO_SUFFIXES,
    AcquisitionCandidate,
    MovieAcquirer,
    _is_sample_video,
    _queue_only_rejection,
)
from .capacity_evidence import CapacityEvidence
from .release_quality import media_runtime_minutes, release_indexer, release_rank, release_seeders
from .season_packs import inspect_season_pack
from .source_health import SourceHealthStore
from .source_probe import canonical_quality
from .subdl import SubDLSource
from .subtitle_language import SubtitlePolicy, is_brazilian_portuguese_subtitle, is_english_subtitle
from .subtitle_recovery import recover_subtitle

LOGGER = logging.getLogger(__name__)
_SEASON_KEY = re.compile(r"season:tmdb:([1-9][0-9]*):([0-9]+)")
_WEB_DL = re.compile(r"(?<![a-z0-9])web[ ._-]*dl(?![a-z0-9])", re.I)
_WEB = re.compile(r"(?<![a-z0-9])web(?![a-z0-9])", re.I)
_REMUX = re.compile(r"(?<![a-z0-9])remux(?![a-z0-9])", re.I)
_PROVIDER = re.compile(r"(?<![a-z0-9])(amzn|nf|atvp|dsnp|hmax|hulu|pmtp|peacock)(?![a-z0-9])", re.I)


def _episode_tag(season: int, episode: int) -> str:
    return f"S{season:02d}E{episode:02d}"


def _season_active(
    repository: ReservationRepository,
    reservation_id: str,
    media_key: str,
    is_tombstoned: Callable[[str], bool],
) -> bool:
    reservation = repository.active_reservation(reservation_id)
    return (
        reservation is not None and reservation["media_key"] == media_key
        and not is_tombstoned(media_key)
    )


def _single_episode_name(value: str, season: int, episode: int) -> bool:
    tag = _episode_tag(season, episode)
    return bool(re.search(rf"(?<![a-z0-9]){tag}(?![a-z0-9]|[-_. ]?e[0-9])", value, re.I))


def _episode_aired(item: dict[str, object]) -> bool | None:
    """Known future episodes are excluded; unknown dates cannot justify a download."""
    air_date = item.get("airDateUtc")
    if not isinstance(air_date, str):
        return None
    try:
        return datetime.fromisoformat(air_date.replace("Z", "+00:00")) <= datetime.now(UTC)
    except (ValueError, TypeError):
        return None


def _release_family(release: dict[str, object]) -> str | None:
    title = str(release.get("title", ""))
    group = release.get("releaseGroup")
    if not isinstance(group, str) or not group.strip():
        match = re.search(r"-([a-z0-9][a-z0-9._]{0,63})$", title, re.I)
        group = match[1] if match else ""
    provider = _PROVIDER.search(title)
    if not group and provider is None:
        return None
    return f"{group.strip().casefold()}|{provider[1].casefold() if provider else ''}"


def _pack_cutoff_rejection(release: dict[str, object]) -> bool:
    """A partial season may need a pack despite some episodes meeting cutoff."""
    reasons = release.get("rejections")
    prefixes = ("existing file meets cutoff:", "release in queue already meets cutoff:")
    return (
        release.get("rejected") is True
        and isinstance(reasons, list)
        and bool(reasons)
        and all(isinstance(item, str) and item.casefold().startswith(prefixes) for item in reasons)
    )


def _series_rank(
    release: dict[str, object], policy=None, *, original_language=None
) -> tuple[int, int, int, int, int, int, int] | None:
    quality = release.get("quality")
    detail = quality.get("quality") if isinstance(quality, dict) else None
    title = release.get("title")
    if not isinstance(detail, dict) or not isinstance(title, str):
        return None
    normalized = dict(detail)
    if normalized.get("resolution") != 1080:
        return None
    if detail.get("source") == "web":
        if not str(detail.get("name", "")).lower().startswith("webdl"):
            return None
        if not (_WEB_DL.search(title) or _WEB.search(title)):
            return None
        normalized.update(source="webdl", modifier="none")
    elif detail.get("source") in ("bluray", "blurayRaw"):
        normalized.update(
            source="bluray",
            modifier="remux"
            if detail.get("source") == "blurayRaw" or _REMUX.search(title)
            else "none",
        )
    release = {**release, "quality": {"quality": normalized}}
    return (
        policy.rank(release, original_language=original_language)
        if policy
        else release_rank(release, original_language=original_language)
    )


class SeriesAcquirer(MovieAcquirer):
    """Use MovieAcquirer's bounded torrent fetch, with Sonarr episode policy."""

    def __init__(
        self,
        *,
        repository: ReservationRepository,
        permits: PermitRegistry,
        sonarr_url: str,
        sonarr_api_key: str,
        prowlarr_url: str,
        client: httpx.AsyncClient | None = None,
        retry_seconds: int = 900,
        subtitle_source: SubDLSource | None = None,
        subtitle_store: SubtitleArtifactStore | None = None,
        torrent_store: TorrentArtifactStore | None = None,
        capacity_provider: Callable[[], Awaitable[CapacityEvidence]] | None = None,
        gateway_url: str | None = None,
        arr_token: str | None = None,
        health_store: SourceHealthStore | None = None,
        is_tombstoned: Callable[[str], bool] | None = None,
        availability_probe: Callable[[bytes, str], Awaitable[int | None]] | None = None,
        live_source_probes: bool = False,
        release_policy=None,
        subtitle_policy: SubtitlePolicy | None = None,
        source_retry_seconds: float = 300,
        search_timeout_seconds: float = 90,
        download_window: int = 10,
        max_active_downloads: int = 10,
        prefer_season_pack: bool = False,
        release_affinity: bool = False,
    ) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in (download_window, max_active_downloads)
        ):
            raise ValueError("series download window and global download limit must be positive")
        if not isinstance(prefer_season_pack, bool) or not isinstance(release_affinity, bool):
            raise ValueError("series source preferences must be boolean")
        super().__init__(
            repository=repository,
            permits=permits,
            radarr_url=sonarr_url,
            radarr_api_key=sonarr_api_key,
            prowlarr_url=prowlarr_url,
            client=client,
            retry_seconds=retry_seconds,
            subtitle_source=subtitle_source,
            subtitle_store=subtitle_store,
            torrent_store=torrent_store,
            capacity_provider=capacity_provider,
            health_store=health_store,
            gateway_url=gateway_url,
            arr_token=arr_token,
            availability_probe=availability_probe,
            live_source_probes=live_source_probes,
            release_policy=release_policy,
            subtitle_policy=subtitle_policy,
            source_retry_seconds=source_retry_seconds,
            search_timeout_seconds=search_timeout_seconds,
        )
        self.sonarr_url = sonarr_url.rstrip("/")
        if bool(gateway_url) != bool(arr_token):
            raise ValueError("gateway URL and worker token must be configured together")
        self.gateway_url = gateway_url.rstrip("/") if gateway_url else None
        self.arr_token = arr_token
        self.is_tombstoned = is_tombstoned or (lambda _key: False)
        # qBittorrent additionally enforces the global active limit across all media.
        self.download_window = min(download_window, max_active_downloads)
        self.prefer_season_pack = prefer_season_pack
        self.release_affinity = release_affinity
        self._next_pack_search: dict[str, float] = {}
        if release_affinity:
            with sqlite3.connect(repository.path) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS series_release_affinity ("
                    "reservation_id TEXT NOT NULL, scope_key TEXT NOT NULL, "
                    "indexer TEXT NOT NULL, family TEXT NOT NULL, "
                    "PRIMARY KEY (reservation_id, scope_key))"
                )

    def _rank(self, release, *, original_language=None):
        return _series_rank(release, self.release_policy, original_language=original_language)

    def _affinity_anchor(self, reservation_id: str) -> tuple[str, str] | None:
        if not self.release_affinity:
            return None
        reservation = self.repository.active_reservation(reservation_id)
        if reservation is None:
            return None
        match = _SEASON_KEY.fullmatch(reservation["media_key"])
        if match is None:
            return None
        with sqlite3.connect(self.repository.path) as connection:
            row = connection.execute(
                "SELECT a.indexer, a.family FROM series_release_affinity a "
                "JOIN reservations r ON r.id=a.reservation_id WHERE r.media_key LIKE ? "
                "ORDER BY a.scope_key LIMIT 1", (f"season:tmdb:{match[1]}:%",),
            ).fetchone()
        return (row[0], row[1]) if row else None

    def _remember_affinity(self, reservation_id: str, scope: str, release) -> None:
        if not self.release_affinity:
            return
        indexer, family = release_indexer(release), _release_family(release)
        if indexer is None or family is None:
            return
        with sqlite3.connect(self.repository.path) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO series_release_affinity "
                "(reservation_id, scope_key, indexer, family) VALUES (?, ?, ?, ?)",
                (reservation_id, scope, indexer, family),
            )

    def _candidate_rank(self, release, *, reservation_id: str, original_language=None):
        rank = self._rank(release, original_language=original_language)
        if rank is None:
            return None
        anchor = self._affinity_anchor(reservation_id)
        minimum = self.release_policy.indexer_fallback_min_seeders if self.release_policy else 5
        seeds = release_seeders(release)
        same_family = int(
            anchor is not None
            and anchor == (release_indexer(release), _release_family(release))
            and seeds is not None and seeds >= minimum
        )
        # Indexer priority is enforced by the shared selector outside this rank.
        # Preserve resolution and legitimate audio before the healthy-family bonus.
        return (*rank[:2], same_family, *rank[2:])

    async def _reconcile_existing_queue(
        self,
        *,
        episodes: list[dict[str, object]],
        season: int,
        reservation_id: str,
        media_key: str,
        active_episode_ids: set[int],
        imported_episode_ids: set[int],
    ) -> str | None:
        if self.gateway_url is None or self.arr_token is None:
            return None
        changes: dict[str, tuple[str, str, bool]] = {}
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
            if self.health_store is not None and self.health_store.is_protected(permit.infohash):
                continue
            action = "start" if item["id"] in active_episode_ids else "stop"
            previous = changes.get(permit.infohash)
            if previous is None or action == "start":
                changes[permit.infohash] = (
                    action, permit.token, permit.season_pack_parent_id is not None,
                )
        known_capacity = None
        if changes and self.capacity_provider is not None:
            try:
                known_capacity = await self.capacity_provider()
            except Exception:
                # Stops remain safe; every start below still requires fresh evidence.
                pass
        # Stop later torrents before considering a capacity-checked start.
        ordered = sorted(changes.items(), key=lambda item: item[1][0] != "stop")
        for infohash, (action, token, is_pack) in ordered:
            if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
                return "reservation_inactive"
            if known_capacity is not None and known_capacity.remaining_by_hash.get(infohash) == 0:
                # Leave completed torrents seeding while ordered import catches up.
                continue
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
                        if capacity.remaining_by_hash.get(infohash) == 0:
                            continue
                        if pending_bytes > capacity.free_bytes:
                            blocked_status = "waiting_space"
                if blocked_status is not None:
                    action = "stop"
            if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
                return "reservation_inactive"
            response = await self.client.post(
                f"{self.gateway_url}/internal/"
                f"{'source-state' if is_pack else 'series-queue-state'}",
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
            if (
                match is not None and int(match.group(1)) == tmdb_id
                and not self.is_tombstoned(media_key)
            ):
                number = int(match.group(2))
                if number > 0:
                    seasons[number] = reservation_id
        return seasons

    def _episode_imported(
        self,
        item: dict[str, object],
        reservations_by_season: dict[int, str],
        series_tmdb_id: int,
    ) -> bool:
        season = item["seasonNumber"]
        episode = item["episodeNumber"]
        if (
            self.is_tombstoned(f"season:tmdb:{series_tmdb_id}:{season}")
            or self.is_tombstoned(f"episode:tmdb:{series_tmdb_id}:{_episode_tag(season, episode)}")
        ):
            return True
        if item.get("hasFile") is not True:
            return False
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

    async def _download_pending(self, pending, reservations_by_season):
        """Completed payloads await ordered import without occupying download slots."""
        if self.capacity_provider is None:
            return pending
        sources = {
            item["id"]: self.permits.get_for_reservation(
                reservations_by_season[item["seasonNumber"]],
                scope_key=_episode_tag(item["seasonNumber"], item["episodeNumber"]),
            )
            for item in pending
        }
        if not any(
            source is not None and source.state == "confirmed" for source in sources.values()
        ):
            return pending
        try:
            capacity = await self.capacity_provider()
        except Exception as error:
            LOGGER.warning("series progress evidence unavailable: %s", type(error).__name__)
            return pending
        return [
            item for item in pending
            if not (
                (source := sources[item["id"]]) is not None and source.state == "confirmed"
                and capacity.remaining_by_hash.get(source.infohash) == 0
            )
        ]

    def _select_download_window(self, pending, reservations_by_season):
        """Count each validated physical season pack once, including its siblings."""
        selected, identities = [], set()
        for item in pending:
            reservation = reservations_by_season[item["seasonNumber"]]
            scope = _episode_tag(item["seasonNumber"], item["episodeNumber"])
            identity = ("episode", reservation, scope)
            source = self.permits.get_for_reservation(reservation, scope_key=scope)
            if source is not None and source.season_pack_parent_id is not None:
                parent = self.permits.get(source.token)
                if (
                    parent is not None and parent.permit_id == source.season_pack_parent_id
                    and parent.reservation_id == reservation and parent.infohash == source.infohash
                    and parent.state in {"authorized", "dispatching", "unknown", "confirmed"}
                ):
                    identity = ("pack", parent.permit_id)
            if identity not in identities:
                if len(identities) >= self.download_window:
                    continue
                identities.add(identity)
            selected.append(item)
        return selected

    @staticmethod
    def _eligible_episode_manifest(
        torrent: bytes,
        *,
        season: int,
        episode: int,
        allow_external_subtitle: bool = False,
        subtitle_policy: SubtitlePolicy | None = None,
    ) -> tuple[str, str, tuple[str, ...], int] | None:
        try:
            inspected = inspect_torrent(torrent)
        except TorrentBytesError:
            return None
        videos = [
            item
            for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _VIDEO_SUFFIXES
            and not _is_sample_video(item.path)
        ]
        pt_br_subtitles = [
            item
            for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
            and is_brazilian_portuguese_subtitle(item.path)
            and _single_episode_name(PurePosixPath(item.path).name, season, episode)
        ]
        english_subtitles = [
            item
            for item in inspected.files
            if PurePosixPath(item.path).suffix.lower() in _SUBTITLE_SUFFIXES
            and is_english_subtitle(item.path)
            and _single_episode_name(PurePosixPath(item.path).name, season, episode)
        ]
        policy = subtitle_policy or SubtitlePolicy()
        by_language = {"pt-BR": pt_br_subtitles, "en-US": english_subtitles}
        subtitles = next(
            (
                [item for item in by_language[language] if policy.matches(item.path)]
                for language in policy.languages
                if any(policy.matches(item.path) for item in by_language[language])
            ),
            [],
        )
        if (
            len(videos) != 1
            or videos[0].length <= 0
            or not _single_episode_name(PurePosixPath(videos[0].path).name, season, episode)
            or not subtitles
            and not allow_external_subtitle
        ):
            return None
        return (
            inspected.infohash,
            inspected.metadata_sha256,
            tuple(item.path for item in (*videos, *subtitles)),
            inspected.total_bytes,
        )

    async def _eligible_releases(
        self,
        *,
        series_id: int,
        episode_id: int,
        season: int,
        episode: int,
        tmdb_id: int,
        replacement_reason: str | None = None,
        excluded_infohashes: set[str] | None = None,
        runtime_minutes: float | None = None,
        original_language=None,
        reservation_id: str | None = None,
    ) -> AsyncIterator[AcquisitionCandidate]:
        excluded_infohashes = excluded_infohashes or set()
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/release",
            params={"seriesId": series_id, "episodeId": episode_id},
            headers=self.headers,
            timeout=self.search_timeout_seconds,
        )
        response.raise_for_status()
        releases = response.json()
        if not isinstance(releases, list):
            raise ValueError("Sonarr release response is invalid")
        def rank(item):
            if reservation_id is not None:
                return self._candidate_rank(
                    item, reservation_id=reservation_id, original_language=original_language,
                )
            return self._rank(item, original_language=original_language)

        ranked = sorted(
            (item for item in releases if isinstance(item, dict)),
            key=lambda item: rank(item) or (0,) * 8,
            reverse=True,
        )
        async for candidate in self._preferred_indexer_candidates(
            ranked,
            eligible=lambda group: self._inspect_episode_releases(
                group, season=season, episode=episode, replacement_reason=replacement_reason,
                excluded_infohashes=excluded_infohashes, runtime_minutes=runtime_minutes,
            ),
            rank=rank,
        ):
            yield candidate

    async def _inspect_episode_releases(
        self, ranked, *, season, episode, replacement_reason, excluded_infohashes, runtime_minutes,
    ) -> AsyncIterator[AcquisitionCandidate]:
        for release in ranked:
            if (
                release.get("rejected") is not False
                and not (replacement_reason and _queue_only_rejection(release))
                or self._rank(release) is None
                or not isinstance(release.get("title"), str)
                or not _single_episode_name(release["title"], season, episode)
                or not isinstance(release.get("size"), int)
                or release["size"] <= 0
                or not self._trusted_download_url(release.get("downloadUrl"))
            ):
                continue
            claimed = release.get("infoHash")
            if isinstance(claimed, str) and claimed.lower() in excluded_infohashes:
                continue
            torrent = await self._metadata(release["downloadUrl"])
            if torrent is None:
                continue
            manifest = self._eligible_episode_manifest(
                torrent,
                season=season,
                episode=episode,
                allow_external_subtitle=self.subtitle_source is not None,
                subtitle_policy=self.subtitle_policy,
            )
            if manifest is None:
                continue
            # Sonarr names its WEB-DL source "web"; use the same normalized quality
            # as ranking while enforcing the size of the inspected main video.
            quality = dict(release["quality"]["quality"])
            if quality.get("source") == "web":
                quality.update(source="webdl", modifier="none")
            elif quality.get("source") in ("bluray", "blurayRaw"):
                quality.update(
                    source="bluray",
                    modifier="remux"
                    if quality.get("source") == "blurayRaw" or _REMUX.search(release["title"])
                    else "none",
                )
            if not self._acceptable_video_size(
                release | {"quality": {"quality": quality}}, torrent, runtime_minutes
            ):
                continue
            if (
                manifest[0] in excluded_infohashes
                or claimed
                and (not isinstance(claimed, str) or claimed.lower() != manifest[0])
            ):
                continue
            yield release, manifest, None, torrent

    async def _inspect_pack_releases(self, ranked, *, season, episode_runtimes):
        minimum = self.release_policy.indexer_fallback_min_seeders if self.release_policy else 5
        for release in ranked:
            seeds = release_seeders(release)
            native_season = release.get("seasonNumber")
            if (
                release.get("fullSeason") is False
                or isinstance(native_season, int) and not isinstance(native_season, bool)
                and native_season != season
                or self._rank(release) is None
                or release.get("rejected") is not False and not _pack_cutoff_rejection(release)
                or seeds is None or seeds < minimum
                or not isinstance(release.get("size"), int)
                or isinstance(release["size"], bool) or release["size"] <= 0
                or not self._trusted_download_url(release.get("downloadUrl"))
            ):
                continue
            torrent = await self._metadata(release["downloadUrl"])
            if torrent is None:
                continue
            manifest = inspect_season_pack(
                torrent, season=season, episode_runtimes=episode_runtimes,
                release=release, release_policy=self.release_policy,
                allow_external_subtitle=self.subtitle_source is not None,
                subtitle_policy=self.subtitle_policy,
            )
            claimed = release.get("infoHash")
            if manifest is None or claimed and (
                not isinstance(claimed, str) or claimed.lower() != manifest.infohash
            ):
                continue
            yield release, manifest, torrent

    def _pack_paths_collide(self, selected_files: tuple[str, ...]) -> bool:
        """Protect every retained payload, including files outside episode bindings."""
        candidate_paths = [
            (PurePosixPath("/data/torrents") / path).parts for path in selected_files
        ]
        with sqlite3.connect(self.repository.path) as connection:
            tokens = connection.execute(
                "SELECT token FROM gateway_permits WHERE state NOT IN ('retired', 'revoked')",
            ).fetchall()
        artifact_ids = set()
        if self.torrent_store is not None:
            with sqlite3.connect(self.torrent_store.database) as connection:
                artifact_ids = {
                    row[0] for row in connection.execute("SELECT permit_id FROM torrent_artifacts")
                }
        for (token,) in tokens:
            source = self.permits.get(token)
            if source is None or source.state in {"retired", "revoked"}:
                continue
            destination = PurePosixPath(source.destination).parts
            if not any(
                destination[:len(candidate)] == candidate
                or candidate[:len(destination)] == destination for candidate in candidate_paths
            ):
                continue
            files = source.selected_files
            metadata = self.torrent_store.get(source) if self.torrent_store is not None else None
            if metadata is None and source.permit_id in artifact_ids:
                # An invalid stored artifact can hide extras; only an absent
                # legacy artifact permits the selected-files fallback.
                return True
            if metadata is not None:
                files = tuple(entry.path for entry in inspect_torrent(metadata).files)
            for path in files:
                retained = (PurePosixPath(source.destination) / path).parts
                if any(
                    retained[:len(candidate)] == candidate
                    or candidate[:len(retained)] == retained
                    for candidate in candidate_paths
                ):
                    # Linux paths are case sensitive. Compare whole components,
                    # so a file also cannot be replaced by a directory or its parent.
                    return True
        return False

    async def _acquire_season_pack(
        self, *, series, season, reservation_id, episode_rows, imported_episode_ids,
    ) -> str | None:
        if not self.prefer_season_pack:
            return None
        media_key = f"season:tmdb:{series['tmdbId']}:{season}"
        if self.capacity_provider is None:
            return "capacity_unavailable"
        scope = f"S{season:02d}PACK"
        self.permits.retire_expired_authorized(reservation_id, scope_key=scope)
        parent = self.permits.get_for_reservation(reservation_id, scope_key=scope)
        if parent is not None:
            if parent.state == "confirmed":
                return None
            reconciled = await self._reconcile_uncertain_source(parent)
            return reconciled or "waiting_pack_confirmation"

        def has_deleted_episode():
            return any(
                self.is_tombstoned(
                    f"episode:tmdb:{series['tmdbId']}:"
                    f"{_episode_tag(season, item['episodeNumber'])}"
                ) for item in episode_rows
            )

        # A fresh complete pack would download a deliberately deleted episode again.
        # Existing shared packs above remain available for their surviving siblings.
        if has_deleted_episode():
            return None
        if time.monotonic() < self._next_pack_search.get(reservation_id, 0):
            return None
        # Unknown dates or runtimes cannot justify admission of a complete season.
        if any(_episode_aired(item) is None for item in episode_rows):
            return None
        aired = [item for item in episode_rows if _episode_aired(item) is True]
        runtimes = {
            item["episodeNumber"]: media_runtime_minutes(item.get("runtime"), series.get("runtime"))
            for item in aired
        }
        if not runtimes or any(value is None for value in runtimes.values()):
            return None
        missing_scopes = {
            _episode_tag(season, item["episodeNumber"])
            for item in aired if item["id"] not in imported_episode_ids
            and self.permits.get_for_reservation(
                reservation_id, scope_key=_episode_tag(season, item["episodeNumber"]),
            ) is None
        }
        if not missing_scopes:
            return None
        self._next_pack_search[reservation_id] = time.monotonic() + self.retry_seconds
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/release",
            params={"seriesId": series["id"], "seasonNumber": season},
            headers=self.headers, timeout=self.search_timeout_seconds,
        )
        response.raise_for_status()
        releases = response.json()
        if not isinstance(releases, list):
            raise ValueError("Sonarr season release response is invalid")

        def rank(release):
            return self._candidate_rank(
                release, reservation_id=reservation_id,
                original_language=series.get("originalLanguage"),
            )

        ranked = sorted(
            (release for release in releases if isinstance(release, dict)),
            key=lambda release: rank(release) or (0,) * 8, reverse=True,
        )
        async for release, manifest, torrent in self._preferred_indexer_candidates(
            ranked,
            eligible=lambda group: self._inspect_pack_releases(
                group, season=season, episode_runtimes=runtimes,
            ),
            rank=rank,
        ):
            if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
                return "reservation_inactive"
            if has_deleted_episode():
                return None
            if self._pack_paths_collide(manifest.selected_files):
                LOGGER.info("Skipping season pack with retained payload path collision")
                continue
            bindings = {
                episode_scope: files for episode_scope, files in manifest.episode_files.items()
                if episode_scope in missing_scopes
            }
            try:
                capacity = await self.capacity_provider() if self.capacity_provider else None
                if not _season_active(
                    self.repository, reservation_id, media_key, self.is_tombstoned,
                ):
                    return "reservation_inactive"
                if has_deleted_episode():
                    return None
                parent = self.permits.issue(
                    infohash=manifest.infohash, metadata_sha256=manifest.metadata_sha256,
                    selected_files=manifest.selected_files, budget_bytes=manifest.budget_bytes,
                    reservation_id=reservation_id, scope_key=scope,
                    destination="/data/torrents", category="sonarr", capacity=capacity,
                    expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    reported_seeders=release_seeders(release),
                )
            except PermissionError as error:
                if str(error) == "waiting_space":
                    continue
                return str(error)
            except sqlite3.IntegrityError:
                return "already_permitted"
            try:
                self.permits.bind_season_pack(parent.token, episode_files=bindings)
            except (PermissionError, ValueError):
                self.permits.cancel_authorized(parent.token)
                return "pack_binding_conflict"
            quality = canonical_quality(release)
            if quality is not None:
                self.permits.set_quality(parent.token, quality)
            if self.torrent_store is not None:
                self.torrent_store.put(parent, torrent)
            response = await self.client.post(
                f"{self.sonarr_url}/api/v3/release", headers=self.headers,
                json={**release, "downloadClientId": 1},
            )
            response.raise_for_status()
            self._remember_affinity(reservation_id, scope, release)
            LOGGER.info("Sonarr season pack requested for %s %s", reservation_id, scope)
            return "grabbed"
        return None

    async def acquire(self, media_key: str, reservation_id: str) -> str:
        if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
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
            (
                item
                for item in series_payload
                if isinstance(item, dict)
                and item.get("tmdbId") == tmdb_id
                and item.get("monitored") is True
                and isinstance(item.get("id"), int)
            ),
            None,
        )
        if series is None:
            return "series_not_monitored"
        episodes_response = await self.client.get(
            f"{self.sonarr_url}/api/v3/episode",
            params={"seriesId": series["id"]},
            headers=self.headers,
        )
        episodes_response.raise_for_status()
        episodes_payload = episodes_response.json()
        if not isinstance(episodes_payload, list):
            raise ValueError("Sonarr episode response is invalid")
        if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
            return "reservation_inactive"
        reservations_by_season = self._requested_seasons(tmdb_id)
        reservations_by_season.setdefault(season, reservation_id)
        chronological = sorted(
            (
                item
                for item in episodes_payload
                if isinstance(item, dict)
                and isinstance(item.get("seasonNumber"), int)
                and not isinstance(item["seasonNumber"], bool)
                and item["seasonNumber"] in reservations_by_season
                and isinstance(item.get("episodeNumber"), int)
                and not isinstance(item["episodeNumber"], bool)
                and item["episodeNumber"] > 0
                and isinstance(item.get("id"), int)
            ),
            key=lambda item: (item["seasonNumber"], item["episodeNumber"]),
        )
        known_seasons = {item["seasonNumber"] for item in chronological}
        earlier_catalog_complete = all(
            number in known_seasons for number in reservations_by_season if number < season
        )
        imported_episode_ids = {
            item["id"]
            for item in chronological
            if self._episode_imported(item, reservations_by_season, tmdb_id)
        }
        episode_rows = sorted(
            (
                item
                for item in episodes_payload
                if isinstance(item, dict)
                and item.get("seasonNumber") == season
                and isinstance(item.get("episodeNumber"), int)
                and not isinstance(item["episodeNumber"], bool)
                and item["episodeNumber"] > 0
                and isinstance(item.get("id"), int)
                and not isinstance(item["id"], bool)
                and item.get("monitored") is True
            ),
            key=lambda item: item["episodeNumber"],
        )
        pending = [
            item for item in chronological
            if item.get("monitored") is True
            and _episode_aired(item) is True
            and item["id"] not in imported_episode_ids
        ]
        download_pending = await self._download_pending(pending, reservations_by_season)
        if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
            return "reservation_inactive"
        window = self._select_download_window(
            download_pending, reservations_by_season,
        ) if earlier_catalog_complete else []
        active_episode_ids = {item["id"] for item in window}
        queue_status = await self._reconcile_existing_queue(
            episodes=chronological,
            season=season,
            reservation_id=reservation_id,
            media_key=media_key,
            active_episode_ids=active_episode_ids,
            imported_episode_ids=imported_episode_ids,
        )
        if queue_status is not None:
            return queue_status
        if not earlier_catalog_complete:
            return "waiting_previous_season"
        if not episode_rows:
            return "season_not_monitored"
        season_pending = [item for item in pending if item["seasonNumber"] == season]
        future = any(_episode_aired(item) is not True for item in episode_rows)
        if not season_pending:
            return "waiting_episodes" if future else "already_imported"
        selected = [item for item in window if item["seasonNumber"] == season]
        if not selected:
            return "waiting_previous_season" if window and window[0]["seasonNumber"] < season else (
                "waiting_download_window"
            )
        pack_result = await self._acquire_season_pack(
            series=series, season=season, reservation_id=reservation_id,
            episode_rows=episode_rows, imported_episode_ids=imported_episode_ids,
        )
        if pack_result is not None:
            return pack_result
        waiting_space = False
        no_source = False
        # Downloads may overlap; SeriesFinalizer alone controls ordered library publication.
        for item in selected:
            if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
                return "reservation_inactive"
            number = item["episodeNumber"]
            scope = _episode_tag(season, number)
            existing = self.permits.get_for_reservation(reservation_id, scope_key=scope)
            if existing is not None and existing.season_pack_parent_id is not None:
                # One physical torrent owns admission and health; these identities
                # exist only for strict per-episode validation and ordered import.
                continue
            if existing is None or not (
                existing.state == "authorized"
                and self.permits.had_superseded(reservation_id, scope_key=scope)
            ):
                self.permits.retire_expired_authorized(reservation_id, scope_key=scope)
                existing = self.permits.get_for_reservation(reservation_id, scope_key=scope)
            if existing is None and self.permits.had_superseded(reservation_id, scope_key=scope):
                return "replacement_missing_manual"
            replacement_reason = None
            old_health = None
            if item.get("hasFile") is True:
                continue
            if existing is not None and existing.state == "confirmed":
                probe_state = await self._monitor_probe(existing)
                if probe_state is not None:
                    return probe_state
                replacement_reason, old_health = await self._source_status(existing)
                if replacement_reason is None:
                    continue
            elif existing is not None and existing.state != "authorized":
                reconciled = await self._reconcile_uncertain_source(existing)
                if reconciled is not None:
                    return reconciled
                continue
            if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
                return "reservation_inactive"
            if existing is not None and existing.state == "authorized":
                retried = await self._retry_replacement(existing)
                if retried is not None:
                    return retried
            if replacement_reason is None and (reservation_id, scope) in self._posted:
                continue
            if time.monotonic() < self._next_search.get(f"{reservation_id}:{scope}", 0):
                continue
            self._next_search[f"{reservation_id}:{scope}"] = time.monotonic() + (
                self.source_retry_seconds if replacement_reason else self.retry_seconds
            )
            found_candidate = False
            excluded = (
                {
                    source.infohash
                    for source in self.permits.list_source_history(reservation_id, scope_key=scope)
                }
                if replacement_reason
                else set()
            )
            candidates = self._eligible_releases(
                series_id=series["id"],
                episode_id=item["id"],
                season=season,
                episode=number,
                tmdb_id=tmdb_id,
                replacement_reason=replacement_reason,
                excluded_infohashes=excluded,
                runtime_minutes=media_runtime_minutes(item.get("runtime"), series.get("runtime")),
                original_language=series.get("originalLanguage"),
                reservation_id=reservation_id,
            )
            async for candidate in self._prioritize_replacements(
                candidates,
                old=existing,
                reason=replacement_reason,
                health=old_health,
                rank=lambda release: self._rank(
                    release, original_language=series.get("originalLanguage")
                ),
            ):
                if not _season_active(
                    self.repository, reservation_id, media_key, self.is_tombstoned,
                ):
                    return "reservation_inactive"
                found_candidate = True
                release, (infohash, digest, files, bytes_total), external, torrent = candidate
                recovered = None
                if not any(
                    PurePosixPath(name).suffix.lower() in _SUBTITLE_SUFFIXES for name in files
                ):
                    assert self.subtitle_source is not None

                    async def fetch(language, mode, title=release["title"], episode_number=number):
                        return await self.subtitle_source.fetch(
                            tmdb_id=tmdb_id,
                            release_title=title,
                            season=season,
                            episode=episode_number,
                            language=language,
                            match_mode=mode,
                        )

                    recovered = await recover_subtitle(self.subtitle_policy, fetch)
                    external = recovered.content if recovered else None
                if not _season_active(
                    self.repository, reservation_id, media_key, self.is_tombstoned,
                ):
                    return "reservation_inactive"
                if external is not None:
                    assert self.subtitle_store is not None
                    self.subtitle_store.put(
                        reservation_id,
                        scope,
                        infohash,
                        external,
                        language=recovered.language if recovered else "BR_PT",
                        replace_language=True,
                    )
                if replacement_reason is not None:
                    assert existing is not None
                    try:
                        return await self._dispatch_replacement(
                            old=existing,
                            infohash=infohash,
                            metadata_sha256=digest,
                            selected_files=files,
                            exact_bytes=bytes_total,
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
                        if not _season_active(
                            self.repository, reservation_id, media_key, self.is_tombstoned,
                        ):
                            return "reservation_inactive"
                        chosen_permit = self.permits.issue(
                            infohash=infohash,
                            metadata_sha256=digest,
                            destination="/data/torrents",
                            category="sonarr",
                            reservation_id=reservation_id,
                            scope_key=scope,
                            selected_files=files,
                            budget_bytes=bytes_total,
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
                if self.permits.had_superseded(reservation_id, scope_key=scope):
                    retried = await self._retry_replacement(chosen_permit)
                    return retried or "replacement_metadata_unavailable"
                response = await self.client.post(
                    f"{self.sonarr_url}/api/v3/release",
                    headers=self.headers,
                    json={**release, "downloadClientId": 1},
                )
                response.raise_for_status()
                self._posted.add((reservation_id, scope))
                self._remember_affinity(reservation_id, scope, release)
                LOGGER.info("Sonarr grab requested for %s %s", media_key, scope)
                return "grabbed"
            if not found_candidate:
                no_source = True
            if replacement_reason == "replacing" and existing is not None:
                if not _season_active(
                    self.repository, reservation_id, media_key, self.is_tombstoned,
                ):
                    return "reservation_inactive"
                resumed = await self._resume_if_safe(existing)
                if resumed != "resumed":
                    return resumed
        if waiting_space:
            return "waiting_space"
        return "no_eligible_release" if no_source else "waiting_episodes"
