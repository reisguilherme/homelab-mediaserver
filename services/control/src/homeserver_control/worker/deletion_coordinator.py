"""Resume explicit Jellyfin deletions while preserving unrelated media."""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from homeserver_common.storage import StorageRegistry, StorageUnavailable
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.storage_paths import physical_path, physical_paths

LOGGER = logging.getLogger(__name__)
_SOURCE_UNLINK_GRACE_SECONDS = 30
_MOVIE_KEY = re.compile(r"movie:tmdb:([1-9][0-9]*)$")
_EPISODE_KEY = re.compile(r"episode:tmdb:([1-9][0-9]*):S([0-9]{2})E([0-9]{2})$")
_SIDECAR = re.compile(
    r"^(?:\.(?:pt[-_]BR|en|eng)(?:\.(?:forced|hi|sdh|cc))?)?"
    r"\.(?:srt|ass|ssa|vtt|nfo|jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_SAFE_PREFLIGHT_DETAILS = {
    "worker credential required", "confirmed media source required",
    "source identity is not deletable", "torrent is shared by another active permit",
    "verified torrent metadata unavailable", "verified torrent metadata changed",
    "torrent contains other media", "torrent identity changed", "torrent files changed",
    "season pack episode files changed", "source permit changed",
}


class DeletionBlocked(ValueError):
    """Identity or source scope is ambiguous and needs operator review."""


class DeletionRetryable(RuntimeError):
    """A dependency or fresh mount proof may become available later."""


class DeletionCoordinator:
    """Process one durable job per call, removing its source before Arr media."""

    def __init__(
        self,
        *,
        jobs: DeletionJobStore,
        media_root: str | Path,
        data_root: str | Path,
        snapshot_path: str | Path,
        filesystem_id: str,
        radarr_url: str,
        radarr_api_key: str,
        sonarr_url: str,
        sonarr_api_key: str,
        seerr_url: str,
        seerr_api_key: str,
        gateway_url: str,
        arr_token: str,
        jellyfin_url: str,
        jellyfin_api_key: str,
        client: httpx.AsyncClient | None = None,
        mount_check: Callable[[Path], bool] | None = None,
        storage_registry: StorageRegistry | None = None,
    ) -> None:
        if not all(
            (
                filesystem_id,
                radarr_url,
                radarr_api_key,
                sonarr_url,
                sonarr_api_key,
                seerr_url,
                seerr_api_key,
                gateway_url,
                arr_token,
                jellyfin_url,
                jellyfin_api_key,
            )
        ):
            raise ValueError("deletion coordinator requires credentials and filesystem identity")
        self.jobs = jobs
        self.storage_registry = storage_registry
        self._active_job: dict | None = None
        self.media_root = Path(media_root)
        self.data_root = Path(data_root)
        self.snapshot_path = Path(snapshot_path)
        self.filesystem_id = filesystem_id
        self.radarr_url = radarr_url.rstrip("/")
        self.sonarr_url = sonarr_url.rstrip("/")
        self.seerr_url = seerr_url.rstrip("/")
        self.gateway_url = gateway_url.rstrip("/")
        self.jellyfin_url = jellyfin_url.rstrip("/")
        self.radarr_headers = {"X-Api-Key": radarr_api_key}
        self.sonarr_headers = {"X-Api-Key": sonarr_api_key}
        self.seerr_headers = {"X-Api-Key": seerr_api_key}
        self.gateway_headers = {"X-Arr-Token": arr_token}
        self.jellyfin_headers = {"X-Emby-Token": jellyfin_api_key}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self.mount_check = mount_check or os.path.ismount
        self._jellyfin_notify_at: dict[str, float] = {}
        self._jellyfin_refresh_at: dict[str, float] = {}
        self._source_unlink_wait_since: dict[str, float] = {}

    def _guard_mount(self) -> None:
        if self.storage_registry is not None:
            if self._active_job is None:
                raise DeletionBlocked('deletion has no captured physical identity')
            payload = self._active_job['payload']
            identities = ([payload.get('file_identity', {})]
                          if self._active_job['item_type'] != 'Season' else
                          payload.get('directory_identity', {}).get('physical_directories', []))
            if not identities:
                raise DeletionBlocked('deletion has no captured physical identity')
            for identity in identities:
                self._guard_identity(identity)
            return
        try:
            snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DeletionRetryable("media mount evidence unavailable") from error
        measured = snapshot.get("measured_at") if isinstance(snapshot, dict) else None
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("filesystem_id") != self.filesystem_id
            or type(measured) not in (int, float)
            or not 0 <= time.time() - measured <= 30
            or not self.mount_check(self.data_root)
        ):
            raise DeletionRetryable("media mount evidence is stale or mismatched")

    def _guard_identity(self, identity: dict) -> str:
        pool_id, filesystem_id = identity.get('pool_id'), identity.get('filesystem_id')
        if pool_id not in self.storage_registry.pools or not filesystem_id:
            raise DeletionBlocked('captured physical identity requires migration proof')
        if self.storage_registry.pools[pool_id].filesystem_id != filesystem_id:
            raise DeletionBlocked('captured physical filesystem identity changed')
        try:
            self.storage_registry.inspect(pool_id, writable=True)
        except StorageUnavailable as error:
            raise DeletionRetryable('captured physical pool is unavailable') from error
        return pool_id

    def _captured_path(self, path: Path, identity: dict) -> Path:
        if self.storage_registry is None:
            return path
        pool_id = self._guard_identity(identity)
        logical = Path('/data') / path.relative_to(self.data_root)
        try:
            physical = self.storage_registry.resolve(pool_id, logical, writable=True)
            if path.exists():
                actual_pool, actual = physical_path(
                    self.storage_registry, logical, union_path=path, writable=True,
                )
                if actual_pool != pool_id or actual != physical:
                    raise DeletionBlocked('captured media moved to another physical pool')
            elif physical.exists():
                raise DeletionRetryable('physical file is hidden from the media view')
            return physical
        except StorageUnavailable as error:
            raise DeletionRetryable('physical media path cannot be verified') from error

    def _reservation_identity_valid(self, filesystem_id: str) -> bool:
        if self.storage_registry is None:
            return filesystem_id == self.filesystem_id
        # A season reservation predates (and does not choose) each episode pool.
        # Accept its legacy SSD device only after verifying the registered SSD.
        try:
            sample = self.storage_registry.inspect('ssd')
            device = self.storage_registry.pools['ssd'].root.stat().st_dev
        except (StorageUnavailable, OSError) as error:
            raise DeletionRetryable('reservation physical SSD identity unavailable') from error
        return filesystem_id in {sample.filesystem_id, f'device:{device}'}

    @staticmethod
    def _payload(job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload")
        item_type = job.get("item_type")
        if not isinstance(payload, dict) or item_type not in {"Movie", "Episode"}:
            raise DeletionBlocked("unsupported or incomplete captured item")
        media_key = payload.get("media_key")
        if item_type == "Movie":
            match = _MOVIE_KEY.fullmatch(media_key) if isinstance(media_key, str) else None
            ids = ("radarr_id", "radarr_file_id", "tmdb_id")
            if match is None or payload.get("tmdb_id") != int(match.group(1)):
                raise DeletionBlocked("movie identity is invalid")
        else:
            match = _EPISODE_KEY.fullmatch(media_key) if isinstance(media_key, str) else None
            ids = (
                "sonarr_series_id",
                "sonarr_episode_id",
                "sonarr_episode_file_id",
                "series_tmdb_id",
                "season",
                "episode",
            )
            if match is None or (
                payload.get("series_tmdb_id"),
                payload.get("season"),
                payload.get("episode"),
            ) != tuple(map(int, match.groups())):
                raise DeletionBlocked("episode identity is invalid")
        if any(type(payload.get(name)) is not int or payload[name] <= 0 for name in ids[:3]):
            raise DeletionBlocked("Arr identity is incomplete")
        identity = payload.get("file_identity")
        if not isinstance(identity, dict) or any(
            type(identity.get(name)) is not int or identity[name] < 0
            for name in ("device", "inode", "size", "mtime_ns")
        ):
            raise DeletionBlocked("captured file identity is incomplete")
        return payload

    def _file(self, job: dict[str, Any], *, must_exist: bool) -> tuple[Path, os.stat_result | None]:
        payload = self._payload(job)
        raw_path = payload.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            raise DeletionBlocked("captured file path is absent")
        path = Path(raw_path)
        library = self.media_root / ("movies" if job["item_type"] == "Movie" else "tv")
        try:
            safe = (
                path.is_absolute()
                and path != library
                and path.is_relative_to(library)
                and path.resolve(strict=False).is_relative_to(library.resolve(strict=True))
            )
        except OSError as error:
            raise DeletionRetryable("media library cannot be inspected") from error
        if not safe:
            raise DeletionBlocked("captured file path escapes the expected library")
        for component in (path, *path.parents):
            if component.is_symlink():
                raise DeletionBlocked("captured media path contains a symlink")
            if component == self.data_root:
                break
        physical = self._captured_path(path, payload['file_identity'])
        try:
            current = physical.stat()
        except FileNotFoundError:
            if must_exist:
                raise DeletionBlocked("captured file disappeared before source cleanup") from None
            return path, None
        except OSError as error:
            raise DeletionRetryable("captured file cannot be inspected") from error
        if not stat.S_ISREG(current.st_mode):
            raise DeletionBlocked("captured file is not a regular file")
        identity = payload["file_identity"]
        actual = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
        captured = tuple(identity[name] for name in ("device", "inode", "size", "mtime_ns"))
        if actual != captured:
            raise DeletionBlocked("captured file changed after Jellyfin deletion request")
        return path, current

    def _association(self, job: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
        """Find only the reservation and permit with the captured media/scope."""
        payload = self._payload(job)
        if job["item_type"] == "Movie":
            reservation_key = payload["media_key"]
            scope = None
        else:
            reservation_key = f"season:tmdb:{payload['series_tmdb_id']}:{payload['season']}"
            scope = f"S{payload['season']:02d}E{payload['episode']:02d}"
        with sqlite3.connect(self.jobs.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """SELECT r.id, r.filesystem_id, q.source_id FROM reservations r
                JOIN requests q ON q.id = r.request_id WHERE r.media_key = ?""",
                (reservation_key,),
            ).fetchall()
            if len(rows) > 1:
                raise DeletionBlocked("more than one reservation matches captured item")
            if not rows:
                return None, None, None
            if not self._reservation_identity_valid(rows[0]['filesystem_id']):
                raise DeletionBlocked("reservation belongs to another media filesystem")
            permit_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='gateway_permits' AND type='table'"
            ).fetchone()
            columns = {row[1] for row in connection.execute("PRAGMA table_info(gateway_permits)")}
            primary_filter = " AND probe_parent_id IS NULL" if "probe_parent_id" in columns else ""
            permits = (
                []
                if permit_table is None
                else connection.execute(
                    """SELECT token, state FROM gateway_permits
                WHERE reservation_id = ? AND scope_key IS ?
                AND state IN ('authorized','dispatching','unknown','confirmed')"""
                    + primary_filter,
                    (rows[0]["id"], scope),
                ).fetchall()
            )
            pack_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='season_pack_episodes' AND type='table'"
            ).fetchone()
            if permit_table is not None and pack_table is not None and scope is not None:
                permits.extend(
                    connection.execute(
                        "SELECT p.token, p.state FROM gateway_permits p "
                        "JOIN season_pack_episodes e ON e.parent_permit_id=p.permit_id "
                        "WHERE p.reservation_id=? AND e.scope_key=? "
                        "AND p.state IN ('authorized','dispatching','unknown','confirmed')"
                        + (
                            " AND p.probe_parent_id IS NULL" if "probe_parent_id" in columns else ""
                        ),
                        (rows[0]["id"], scope),
                    ).fetchall()
                )
        if len(permits) > 1:
            raise DeletionBlocked("multiple active torrent permits match captured item")
        if permits and permits[0]["state"] != "confirmed":
            raise DeletionBlocked("torrent permit is not confirmed")
        if permits and self.storage_registry is not None:
            registry = PermitRegistry(self.jobs.path, storage_registry=self.storage_registry)
            permit = registry.get(permits[0]['token'])
            if permit is None or not registry.placement_valid(permit):
                raise DeletionRetryable('source physical pool is unavailable')
            identity = payload['file_identity']
            if permit.pool_id != identity.get('pool_id') or (
                permit.filesystem_id is not None
                and permit.filesystem_id != identity.get('filesystem_id')
            ):
                raise DeletionBlocked('torrent and library physical identities differ')
        return reservation_key, rows[0]["source_id"], permits[0]["token"] if permits else None

    @staticmethod
    def _status(response: httpx.Response, *, allow_missing: bool = False) -> bool:
        if response.status_code == 404 and allow_missing:
            return False
        if response.status_code in {400, 401, 403, 409, 422}:
            reason = f"HTTP {response.status_code}"
            try:
                body = response.json()
            except ValueError:
                body = None
            detail = body.get("detail") if isinstance(body, dict) else None
            if isinstance(detail, str) and detail in _SAFE_PREFLIGHT_DETAILS:
                reason += f": {detail}"
            raise DeletionBlocked(
                f"{response.request.url.host} rejected deletion preflight ({reason})"
            )
        if response.status_code >= 400:
            raise DeletionRetryable(
                f"{response.request.url.host} returned HTTP {response.status_code}"
            )
        return True

    @classmethod
    def _json(cls, response: httpx.Response, *, allow_missing: bool = False) -> Any:
        if not cls._status(response, allow_missing=allow_missing):
            return None
        try:
            return response.json()
        except ValueError as error:
            raise DeletionRetryable("media service returned invalid JSON") from error

    async def _movie_preflight(self, payload: dict[str, Any], *, file_exists: bool) -> bool:
        response = await self.client.get(
            f"{self.radarr_url}/api/v3/movie/{payload['radarr_id']}",
            headers=self.radarr_headers,
        )
        movie = self._json(response, allow_missing=True)
        if movie is None:
            if file_exists:
                # A lost DELETE response can remove the record before this
                # worker commits its stage. Never delete a replacement record.
                matches_response = await self.client.get(
                    f"{self.radarr_url}/api/v3/movie",
                    params={"tmdbId": payload["tmdb_id"]},
                    headers=self.radarr_headers,
                )
                matches = self._json(matches_response)
                if not isinstance(matches, list) or matches:
                    raise DeletionBlocked("Radarr movie changed while delete was pending")
            return False
        if not isinstance(movie, dict) or (
            movie.get("id") != payload["radarr_id"] or movie.get("tmdbId") != payload["tmdb_id"]
        ):
            raise DeletionBlocked("Radarr movie identity changed")
        movie_file = movie.get("movieFile")
        if movie_file is None and not file_exists:
            return True
        if not isinstance(movie_file, dict) or (
            movie_file.get("id") != payload["radarr_file_id"]
            or movie_file.get("path") != payload["file_path"]
            or movie_file.get("size") != payload["file_identity"]["size"]
        ):
            raise DeletionBlocked("Radarr movie file no longer matches capture")
        return True

    async def _episode_preflight(self, payload: dict[str, Any], *, file_exists: bool) -> bool:
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/episode",
            params={"seriesId": payload["sonarr_series_id"]},
            headers=self.sonarr_headers,
        )
        episodes = self._json(response)
        if not isinstance(episodes, list):
            raise DeletionRetryable("Sonarr episode list is invalid")
        matches = [
            item
            for item in episodes
            if isinstance(item, dict) and item.get("id") == payload["sonarr_episode_id"]
        ]
        if len(matches) != 1 or (
            matches[0].get("seasonNumber") != payload["season"]
            or matches[0].get("episodeNumber") != payload["episode"]
            or matches[0].get("seriesId") != payload["sonarr_series_id"]
        ):
            raise DeletionBlocked("Sonarr episode identity changed")
        episode_file_id = payload["sonarr_episode_file_id"]
        if any(
            item.get("episodeFileId") == episode_file_id
            for item in episodes
            if item is not matches[0] and isinstance(item, dict)
        ):
            raise DeletionBlocked("Sonarr episode file is shared with another episode")
        current_file_id = matches[0].get("episodeFileId")
        if current_file_id in (None, 0) and not file_exists:
            return False
        if current_file_id != episode_file_id:
            raise DeletionBlocked("Sonarr episode file changed")
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/episodefile/{episode_file_id}",
            headers=self.sonarr_headers,
        )
        episode_file = self._json(response, allow_missing=not file_exists)
        if episode_file is None and not file_exists:
            return False
        if not isinstance(episode_file, dict) or (
            episode_file.get("id") != episode_file_id
            or episode_file.get("path") != payload["file_path"]
            or episode_file.get("size") != payload["file_identity"]["size"]
        ):
            raise DeletionBlocked("Sonarr episode file no longer matches capture")
        return True

    async def _arr_preflight(self, job: dict[str, Any], *, file_exists: bool) -> bool:
        payload = self._payload(job)
        if job["item_type"] == "Movie":
            return await self._movie_preflight(payload, file_exists=file_exists)
        return await self._episode_preflight(payload, file_exists=file_exists)

    async def _delete_source(self, job: dict[str, Any]) -> None:
        reservation_key, _source_id, permit_token = self._association(job)
        _path, current = self._file(job, must_exist=True)
        if permit_token is None:
            if reservation_key is not None:
                raise DeletionBlocked("managed media has no confirmed torrent source")
            if current is not None and current.st_nlink > 1:
                raise DeletionBlocked("hardlinked torrent source has no confirmed permit")
            return
        self._guard_mount()
        with sqlite3.connect(self.jobs.path) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute("PRAGMA table_info(gateway_permits)")}
            probes = (
                []
                if "probe_parent_id" not in columns
                else connection.execute(
                    "SELECT n.token FROM gateway_permits n JOIN gateway_permits p "
                    "ON n.probe_parent_id=p.permit_id WHERE p.token=? "
                    "AND n.state IN ('authorized','dispatching','unknown','confirmed')",
                    (permit_token,),
                ).fetchall()
            )
        for probe in probes:
            stopped = await self.client.post(
                f"{self.gateway_url}/internal/probe-decision",
                headers=self.gateway_headers,
                json={"permit_token": probe["token"], "decision": "reject"},
            )
            stopped.raise_for_status()
        response = await self.client.post(
            f"{self.gateway_url}/internal/delete-source",
            headers=self.gateway_headers,
            json={
                "permit_token": permit_token,
                "media_key": reservation_key,
                "scope_key": (
                    None
                    if job["item_type"] == "Movie"
                    else f"S{job['payload']['season']:02d}E{job['payload']['episode']:02d}"
                ),
            },
        )
        result = self._json(response)
        if not isinstance(result, dict) or result.get("state") not in {
            "deleted",
            "missing",
            "retained_shared",
        }:
            raise DeletionRetryable("gateway source removal was not confirmed")
        retained = result["state"] == "retained_shared"
        if retained and job["item_type"] != "Episode":
            raise DeletionBlocked("only an episode may retain a shared season source")
        self._guard_mount()
        _path, remaining = self._file(job, must_exist=True)
        if not retained and remaining is not None and remaining.st_nlink > 1:
            # qBit can remove its catalog entry before libtorrent unlinks the
            # payload. Wait for that one source link while keeping Arr untouched.
            now = time.monotonic()
            since = self._source_unlink_wait_since.setdefault(job["item_id"], now)
            if remaining.st_nlink == 2 and now - since < _SOURCE_UNLINK_GRACE_SECONDS:
                raise DeletionRetryable("torrent payload unlink is still processing")
            raise DeletionBlocked("media has another hardlink after torrent cleanup")
        self._source_unlink_wait_since.pop(job["item_id"], None)

    async def _delete_arr(self, job: dict[str, Any]) -> None:
        path, current = self._file(job, must_exist=False)
        payload = self._payload(job)
        self._guard_mount()
        has_arr_file = await self._arr_preflight(job, file_exists=current is not None)
        if job["item_type"] == "Movie":
            if has_arr_file:
                if current is not None:
                    self._guard_mount()
                    self._file(job, must_exist=True)
                    response = await self.client.delete(
                        f"{self.radarr_url}/api/v3/moviefile/{payload['radarr_file_id']}",
                        headers=self.radarr_headers,
                    )
                    self._status(response, allow_missing=True)
                    self._guard_mount()
                    _path, remaining_file = self._file(job, must_exist=False)
                    if remaining_file is not None:
                        raise DeletionBlocked("Radarr did not remove the captured movie file")
                self._guard_mount()
                response = await self.client.delete(
                    f"{self.radarr_url}/api/v3/movie/{payload['radarr_id']}",
                    params={"deleteFiles": "false", "addImportExclusion": "true"},
                    headers=self.radarr_headers,
                )
                self._status(response, allow_missing=True)
        else:
            self._guard_mount()
            response = await self.client.put(
                f"{self.sonarr_url}/api/v3/episode/monitor",
                json={"episodeIds": [payload["sonarr_episode_id"]], "monitored": False},
                headers=self.sonarr_headers,
            )
            self._status(response)
            if has_arr_file:
                self._guard_mount()
                self._file(job, must_exist=False)
                response = await self.client.delete(
                    f"{self.sonarr_url}/api/v3/episodefile/{payload['sonarr_episode_file_id']}",
                    headers=self.sonarr_headers,
                )
                self._status(response, allow_missing=True)
        self._guard_mount()
        _path, remaining = self._file(job, must_exist=False)
        if remaining is not None:
            raise DeletionBlocked("Arr did not remove the captured media file")
        self._cleanup_sidecars(path, job=job)

    def _cleanup_sidecars(self, video: Path, *, job: dict | None = None) -> None:
        """Remove only files sharing the selected video's complete stem."""
        if self.storage_registry is not None:
            if job is None:
                raise DeletionBlocked('sidecar capture missing')
            for entry in job['payload'].get('sidecar_files', []):
                sidecar = Path(entry['file_path'])
                if sidecar.parent != video.parent or not sidecar.name.startswith(video.stem) or (
                    not _SIDECAR.fullmatch(sidecar.name[len(video.stem):])
                ):
                    raise DeletionBlocked('sidecar escaped captured file scope')
                for source in entry.get('source_files', []):
                    source_path = Path(source['file_path'])
                    if not source_path.is_relative_to(self.data_root / 'torrents') or (
                        source_path.suffix.lower() not in {'.srt', '.ass', '.ssa', '.vtt'}
                    ) or any(source['file_identity'].get(key) != entry['file_identity'].get(key)
                             for key in ('pool_id', 'filesystem_id', 'device', 'inode')):
                        raise DeletionBlocked('captured source sidecar escaped media identity')
                    physical_source = self._captured_path(source_path, source['file_identity'])
                    if physical_source.exists():
                        status = physical_source.stat()
                        actual = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
                        if actual != tuple(
                            source['file_identity'][key]
                            for key in ('device', 'inode', 'size', 'mtime_ns')
                        ):
                            raise DeletionBlocked('captured source subtitle changed')
                        source_path.unlink()
                physical = self._captured_path(sidecar, entry['file_identity'])
                if not physical.exists():
                    continue
                status = physical.stat()
                if (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns) != tuple(
                    entry['file_identity'][key] for key in ('device', 'inode', 'size', 'mtime_ns')
                ):
                    raise DeletionBlocked('captured sidecar changed')
                sidecar.unlink()
            return
        parent = video.parent
        if not parent.exists():
            return
        if parent.is_symlink() or not parent.is_dir():
            raise DeletionBlocked("media folder changed before sidecar cleanup")
        try:
            candidates = tuple(parent.iterdir())
        except OSError as error:
            raise DeletionRetryable("media sidecars cannot be inspected") from error
        for sidecar in candidates:
            prefix = video.stem
            if not sidecar.name.startswith(prefix):
                continue
            suffix = sidecar.name[len(prefix) :]
            if not _SIDECAR.fullmatch(suffix):
                continue
            self._guard_mount()
            try:
                before = sidecar.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_size > 50_000_000:
                raise DeletionBlocked("matching media sidecar is not a small regular file")
            try:
                current = sidecar.lstat()
                if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ):
                    raise DeletionBlocked("matching media sidecar changed before removal")
                sidecar.unlink()
            except FileNotFoundError:
                continue
            except OSError as error:
                raise DeletionRetryable("matching media sidecar cannot be removed") from error

    async def _delete_seerr(self, job: dict[str, Any]) -> None:
        if job["item_type"] != "Movie":
            return  # A single episode is not the whole Seerr season request.
        _reservation_key, source_id, _permit = self._association(job)
        if source_id is None:
            return
        if not source_id.isdecimal():
            raise DeletionBlocked("Seerr movie request identity is invalid")
        self._guard_mount()
        response = await self.client.get(
            f"{self.seerr_url}/api/v1/request/{source_id}",
            headers=self.seerr_headers,
        )
        request = self._json(response, allow_missing=True)
        if request is None:
            return
        media = request.get("media") if isinstance(request, dict) else None
        if not isinstance(media, dict) or (
            request.get("id") != int(source_id)
            or media.get("tmdbId") != job["payload"]["tmdb_id"]
            or media.get("mediaType") != "movie"
        ):
            raise DeletionBlocked("Seerr request no longer matches captured movie")
        self._guard_mount()
        response = await self.client.delete(
            f"{self.seerr_url}/api/v1/request/{source_id}",
            headers=self.seerr_headers,
        )
        self._status(response, allow_missing=True)

    async def _seerr_rows(self, resource: str) -> list[dict[str, Any]]:
        """Read a complete, stable Seerr listing before deciding what to remove."""
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        total: int | None = None
        skip = 0
        while True:
            self._guard_mount()
            params: dict[str, str | int] = {"take": 100, "skip": skip, "filter": "all"}
            if resource == "request":
                params["mediaType"] = "movie"
            response = await self.client.get(
                f"{self.seerr_url}/api/v1/{resource}",
                headers=self.seerr_headers,
                params=params,
            )
            result = self._json(response)
            page = result.get("pageInfo") if isinstance(result, dict) else None
            batch = result.get("results") if isinstance(result, dict) else None
            count = page.get("results") if isinstance(page, dict) else None
            expected_pages = (count + 99) // 100 if type(count) is int and count >= 0 else None
            if (
                not isinstance(page, dict)
                or type(count) is not int or count < 0
                or type(page.get("pageSize")) is not int
                or page.get("pageSize") != 100
                or type(page.get("page")) is not int
                or page.get("page") != skip // 100 + 1
                or type(page.get("pages")) is not int
                or page.get("pages") != expected_pages
                or not isinstance(batch, list)
                or len(batch) != min(100, max(count - skip, 0))
            ):
                raise DeletionBlocked("Seerr listing is incomplete or malformed")
            if total is None:
                total = count
            elif count != total:
                raise DeletionBlocked("Seerr listing changed during deletion preflight")
            for row in batch:
                row_id = row.get("id") if isinstance(row, dict) else None
                if type(row_id) is not int or row_id <= 0 or row_id in seen:
                    raise DeletionBlocked("Seerr listing has ambiguous identities")
                seen.add(row_id)
                media = row if resource == "media" else row.get("media")
                if (
                    not isinstance(media, dict)
                    or type(media.get("id")) is not int or media["id"] <= 0
                    or type(media.get("tmdbId")) is not int or media["tmdbId"] <= 0
                    or media.get("mediaType") not in {"movie", "tv"}
                ):
                    raise DeletionBlocked("Seerr listing has incomplete media identity")
                rows.append(row)
            skip += len(batch)
            if skip == total:
                return rows

    async def _seerr_movie_cache(self, tmdb_id: int) -> tuple[int | None, list[dict[str, Any]]]:
        rows = await self._seerr_rows("media")
        matches = [row for row in rows
                   if row["tmdbId"] == tmdb_id and row["mediaType"] == "movie"]
        if len(matches) > 1:
            raise DeletionBlocked("Seerr has multiple cache entries for captured movie")
        return (matches[0]["id"] if matches else None), rows

    async def _delete_seerr_media_cache(self, job: dict[str, Any]) -> None:
        """Forget only a managed movie's metadata after Jellyfin confirms removal."""
        if job["item_type"] != "Movie":
            return
        _reservation_key, source_id, _permit = self._association(job)
        if source_id is None:
            return
        if not source_id.isdecimal():
            raise DeletionBlocked("Seerr movie request identity is invalid")
        tmdb_id = self._payload(job)["tmdb_id"]
        media_id, _ = await self._seerr_movie_cache(tmdb_id)
        if media_id is None:
            return
        requests = await self._seerr_rows("request")
        if any(
            request["media"]["id"] == media_id
            or (request["media"]["tmdbId"] == tmdb_id
                and request["media"]["mediaType"] == "movie")
            for request in requests
        ):
            raise DeletionBlocked("Seerr movie has another request")
        fresh_id, fresh_rows = await self._seerr_movie_cache(tmdb_id)
        if fresh_id != media_id or any(
            row["id"] == media_id and (
                row["tmdbId"] != tmdb_id or row["mediaType"] != "movie"
            ) for row in fresh_rows
        ):
            raise DeletionBlocked("Seerr movie cache identity changed before removal")
        self._guard_mount()
        _path, remaining = self._file(job, must_exist=False)
        if remaining is not None:
            raise DeletionBlocked("media file exists before Seerr cache cleanup")
        if await self._jellyfin_item_present(job["item_id"]):
            raise DeletionRetryable("Jellyfin movie reappeared before Seerr cache cleanup")
        response = await self.client.delete(
            f"{self.seerr_url}/api/v1/media/{media_id}",
            headers=self.seerr_headers,
        )
        self._status(response, allow_missing=True)
        remaining_id, remaining_rows = await self._seerr_movie_cache(tmdb_id)
        if remaining_id is not None and remaining_id != media_id:
            raise DeletionBlocked("Seerr movie cache identity changed after removal")
        if any(row["id"] == media_id and (
            row["tmdbId"] != tmdb_id or row["mediaType"] != "movie"
        ) for row in remaining_rows):
            raise DeletionBlocked("Seerr movie cache identity changed after removal")
        if remaining_id is not None:
            raise DeletionRetryable("Seerr movie cache still exists after removal")

    async def _jellyfin_item_present(self, item_id: str) -> bool:
        response = await self.client.get(
            f"{self.jellyfin_url}/Items",
            params={"Ids": item_id, "Recursive": "true"},
            headers=self.jellyfin_headers,
        )
        result = self._json(response)
        items = result.get("Items") if isinstance(result, dict) else None
        if (
            not isinstance(items, list)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("Id"), str)
                or item["Id"].lower() != item_id.lower()
                for item in items
            )
            or len(items) > 1
        ):
            raise DeletionRetryable("Jellyfin returned an invalid item lookup")
        return bool(items)

    async def _delete_jellyfin(self, job: dict[str, Any]) -> None:
        self._guard_mount()
        path, remaining = self._file(job, must_exist=False)
        if remaining is not None:
            raise DeletionBlocked("media file still exists before Jellyfin cleanup")
        await self._forget_jellyfin_item(job["item_id"], path)

    async def _forget_jellyfin_item(self, item_id: str, path: Path) -> None:
        job = {"item_id": item_id}
        if not await self._jellyfin_item_present(job["item_id"]):
            self._jellyfin_notify_at.pop(job["item_id"], None)
            self._jellyfin_refresh_at.pop(job["item_id"], None)
            return

        now = time.monotonic()
        last_refresh = self._jellyfin_refresh_at.get(job["item_id"])
        if last_refresh is not None:
            if now - last_refresh < 120:
                raise DeletionRetryable("Jellyfin library scan is still processing")
            self._jellyfin_refresh_at.pop(job["item_id"], None)
            self._jellyfin_notify_at.pop(job["item_id"], None)

        # Jellyfin's DELETE /Items may recursively remove a movie directory.
        # Its media mount is read-only, so ask its scanner to forget the absent
        # file instead of granting it write access to the library.
        notified_at = self._jellyfin_notify_at.get(job["item_id"])
        if notified_at is None:
            self._guard_mount()
            self._jellyfin_notify_at[job["item_id"]] = now
            response = await self.client.post(
                f"{self.jellyfin_url}/Library/Media/Updated",
                headers=self.jellyfin_headers,
                json={"Updates": [{"Path": str(path), "UpdateType": "Deleted"}]},
            )
            self._status(response, allow_missing=True)
            if not await self._jellyfin_item_present(job["item_id"]):
                self._jellyfin_notify_at.pop(job["item_id"], None)
                return
            raise DeletionRetryable("Jellyfin targeted catalog update is still processing")
        # Jellyfin 10.10.7 defaults to a 60-second filesystem-monitor delay.
        # Leave room for that debounce and the targeted scan to finish.
        if now - notified_at < 90:
            raise DeletionRetryable("Jellyfin targeted catalog update is still processing")

        self._guard_mount()
        # A timed-out request can still have started a scan. Back off before
        # issuing another full-library refresh for this item.
        self._jellyfin_refresh_at[job["item_id"]] = time.monotonic()
        response = await self.client.post(
            f"{self.jellyfin_url}/Library/Refresh",
            headers=self.jellyfin_headers,
        )
        self._status(response)
        if await self._jellyfin_item_present(job["item_id"]):
            raise DeletionRetryable("Jellyfin library scan has not removed item")
        self._jellyfin_notify_at.pop(job["item_id"], None)
        self._jellyfin_refresh_at.pop(job["item_id"], None)

    def _season_payload(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload")
        if not isinstance(payload, dict):
            raise DeletionBlocked("season capture is incomplete")
        tmdb, tvdb, series, season = (
            payload.get(key) for key in
            ("series_tmdb_id", "series_tvdb_id", "sonarr_series_id", "season")
        )
        if (
            any(type(value) is not int or value <= 0 for value in (tmdb, tvdb, series))
            or type(season) is not int or season < 0
            or payload.get("media_key") != f"season:tmdb:{tmdb}:{season}"
        ):
            raise DeletionBlocked("season identity is invalid")
        folder = payload.get("file_path")
        directory_identity = payload.get("directory_identity")
        metadata = payload.get("metadata_files", [])
        if (
            not isinstance(folder, str) or not folder
            or not isinstance(directory_identity, dict)
            or any(type(directory_identity.get(k)) is not int or directory_identity[k] < 0
                   for k in ("device", "inode"))
            or not isinstance(metadata, list)
        ):
            raise DeletionBlocked("season directory capture is incomplete")
        for entry in metadata:
            if not isinstance(entry, dict) or not isinstance(entry.get("file_path"), str):
                raise DeletionBlocked("season metadata capture is incomplete")
            identity = entry.get("file_identity")
            if not isinstance(identity, dict) or any(
                type(identity.get(k)) is not int or identity[k] < 0
                for k in ("device", "inode", "size", "mtime_ns")
            ):
                raise DeletionBlocked("season metadata identity is incomplete")
        ids = payload.get("sonarr_episode_ids")
        children = payload.get("episodes")
        if (
            not isinstance(ids, list) or not ids
            or any(type(value) is not int or value <= 0 for value in ids)
            or len(set(ids)) != len(ids)
            or not isinstance(children, list) or not children
        ):
            raise DeletionBlocked("season episode inventory is incomplete")
        seen = set()
        for entry in children:
            if not isinstance(entry, dict) or not isinstance(entry.get("item_id"), str):
                raise DeletionBlocked("season child identity is invalid")
            child = dict(
                item_id=entry["item_id"], item_type="Episode", payload=entry.get("payload"),
            )
            selected = self._payload(child)
            child_path = selected.get("file_path")
            if (
                entry["item_id"] in seen or not re.fullmatch(r"[0-9a-fA-F]{32}", entry["item_id"])
                or selected.get("parent_item_id") != job["item_id"]
                or selected["series_tmdb_id"] != tmdb or selected["sonarr_series_id"] != series
                or selected["season"] != season or selected["sonarr_episode_id"] not in ids
                or not isinstance(child_path, str) or not child_path
                or not Path(child_path).is_relative_to(Path(folder))
            ):
                raise DeletionBlocked("season child escaped captured scope")
            seen.add(entry["item_id"])
        return payload

    def _season_folder(self, job: dict[str, Any], *, must_exist: bool) -> Path:
        payload = self._season_payload(job)
        raw = payload.get("file_path")
        if not isinstance(raw, str) or not raw:
            raise DeletionBlocked("season folder is absent")
        path = Path(raw)
        library = self.media_root / "tv"
        if (
            not path.is_absolute() or path == library or not path.is_relative_to(library)
            or not path.resolve(strict=False).is_relative_to(library.resolve(strict=True))
        ):
            raise DeletionBlocked("season folder escapes the TV library")
        for part in (path, *path.parents):
            if part.is_symlink():
                raise DeletionBlocked("season folder contains a symlink")
            if part == self.data_root:
                break
        if self.storage_registry is not None:
            identities = payload.get('directory_identity', {}).get('physical_directories')
            if not isinstance(identities, list) or not identities:
                raise DeletionBlocked('season physical identity requires migration proof')
            expected = set()
            logical = Path('/data/media') / path.relative_to(self.media_root)
            for identity in identities:
                pool_id = self._guard_identity(identity)
                if pool_id in expected:
                    raise DeletionBlocked('duplicate season physical directory')
                expected.add(pool_id)
                physical = self.storage_registry.resolve(pool_id, logical, writable=True)
                if not physical.exists():
                    if must_exist:
                        raise DeletionBlocked('physical season directory disappeared')
                    continue
                status = physical.stat()
                if not stat.S_ISDIR(status.st_mode) or (status.st_dev, status.st_ino) != (
                    identity['device'], identity['inode'],
                ):
                    raise DeletionBlocked('physical season directory changed')
            if path.exists():
                try:
                    found = physical_paths(self.storage_registry, logical, union_path=path,
                                           writable=True)
                except StorageUnavailable as error:
                    raise DeletionRetryable('physical season paths unavailable') from error
                if any(name not in expected for name, _ in found):
                    raise DeletionBlocked('season directory appeared on another physical pool')
            return path
        if not path.exists():
            if must_exist:
                raise DeletionBlocked("season folder disappeared before cleanup")
            return path
        identity = payload.get("directory_identity")
        actual = path.stat()
        if (
            not path.is_dir() or not isinstance(identity, dict)
            or (actual.st_dev, actual.st_ino) != (identity.get("device"), identity.get("inode"))
        ):
            raise DeletionBlocked("season folder changed after capture")
        return path

    def _season_children(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for entry in self._season_payload(job)["episodes"]:
            child = self.jobs.get(entry["item_id"])
            if (
                child is None or child["item_type"] != "Episode"
                or child["payload"] != entry["payload"]
            ):
                raise DeletionBlocked("season child job changed or disappeared")
            if child["stage"] == "blocked":
                raise DeletionBlocked("a season episode requires review")
            result.append(child)
        return result

    async def _season_preflight(self, job: dict[str, Any]) -> tuple[dict, list[dict]]:
        payload = self._season_payload(job)
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/series/{payload['sonarr_series_id']}",
            headers=self.sonarr_headers,
        )
        series = self._json(response)
        if not isinstance(series, dict) or (
            series.get("id") != payload["sonarr_series_id"]
            or series.get("tvdbId") != payload["series_tvdb_id"]
        ):
            raise DeletionBlocked("Sonarr series changed after season capture")
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/episode", headers=self.sonarr_headers,
            params={"seriesId": payload["sonarr_series_id"]},
        )
        episodes = self._json(response)
        if not isinstance(episodes, list) or any(not isinstance(e, dict) for e in episodes):
            raise DeletionRetryable("Sonarr season inventory is invalid")
        selected = [e for e in episodes if e.get("seasonNumber") == payload["season"]]
        if {e.get("id") for e in selected} != set(payload["sonarr_episode_ids"]):
            raise DeletionBlocked("Sonarr season episodes changed after capture")
        captured = {e["payload"]["sonarr_episode_id"]: e["payload"]["sonarr_episode_file_id"]
                    for e in payload["episodes"]}
        for episode in selected:
            if episode.get("seriesId") != payload["sonarr_series_id"]:
                raise DeletionBlocked("episode belongs to another series")
            file_id = episode.get("episodeFileId")
            if file_id and captured.get(episode["id"]) != file_id:
                raise DeletionBlocked("new or changed season file is outside the capture")
        return series, selected

    async def _cancel_season(self, job: dict[str, Any]) -> None:
        payload = self._season_payload(job)
        self.jobs.tombstone(payload["media_key"], job["item_id"])
        with sqlite3.connect(self.jobs.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT request_id, filesystem_id FROM reservations WHERE media_key=?",
                (payload["media_key"],),
            ).fetchall()
            if any(not self._reservation_identity_valid(row[1]) for row in rows) or len(rows) > 1:
                raise DeletionBlocked("season reservation identity is ambiguous")
            # Keep remaining torrent bytes in the capacity ledger until purge is confirmed.
            # The tombstone and cancelled request close acquisition/import gates immediately.
            for request_id, _ in rows:
                connection.execute(
                    "UPDATE requests SET state='cancelled' WHERE id=?", (request_id,),
                )
        series, episodes = await self._season_preflight(job)
        seasons = series.get("seasons")
        if not isinstance(seasons, list) or any(not isinstance(s, dict) for s in seasons) or sum(
            isinstance(s, dict) and s.get("seasonNumber") == payload["season"] for s in seasons
        ) != 1:
            raise DeletionBlocked("Sonarr season identity changed")
        for season in seasons:
            if season.get("seasonNumber") == payload["season"]:
                season["monitored"] = False
        self._guard_mount()
        self._status(await self.client.put(
            f"{self.sonarr_url}/api/v3/series/{payload['sonarr_series_id']}",
            headers=self.sonarr_headers, json=series,
        ))
        self._status(await self.client.put(
            f"{self.sonarr_url}/api/v3/episode/monitor", headers=self.sonarr_headers,
            json={"episodeIds": [e["id"] for e in episodes], "monitored": False},
        ))

    def _season_source_inventory(self, job: dict[str, Any]) -> list[tuple[dict, str, list[str]]]:
        payload = self._season_payload(job)
        with sqlite3.connect(self.jobs.path) as connection:
            connection.row_factory = sqlite3.Row
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='gateway_permits' AND type='table'"
            ).fetchone()
            if table is None:
                return []
            rows = connection.execute(
                "SELECT p.*,r.filesystem_id AS reservation_filesystem_id FROM gateway_permits p "
                "JOIN reservations r ON r.id=p.reservation_id "
                "WHERE r.media_key=? "
                "AND p.state IN "
                "('authorized','dispatching','unknown','confirmed','superseded','probe_rejected')",
                (payload["media_key"],),
            ).fetchall()
            sources = []
            for row in rows:
                scope = row["scope_key"]
                match = re.fullmatch(r"S([0-9]{2,})(?:E[0-9]{2,}|PACK)", scope or "")
                if match is None or int(match[1]) != payload["season"] or (
                    row["category"] != "sonarr" or (
                        self.storage_registry is None and (
                            row["destination"] != "/data/torrents"
                        )
                    )
                    or not self._reservation_identity_valid(row['reservation_filesystem_id'])
                ):
                    raise DeletionBlocked("torrent source escaped selected season")
                if self.storage_registry is not None:
                    permits = PermitRegistry(self.jobs.path, storage_registry=self.storage_registry)
                    permit = permits.get(row['token'])
                    if permit is None or not permits.placement_valid(permit):
                        raise DeletionRetryable('season source physical pool is unavailable')
                if row["state"] in ("unknown", "dispatching"):
                    raise DeletionBlocked("season torrent admission is uncertain")
                if scope.endswith("PACK"):
                    if row["state"] not in {"authorized", "confirmed"}:
                        raise DeletionBlocked("historical season pack requires review")
                    bindings = connection.execute(
                        "SELECT scope_key FROM season_pack_episodes WHERE parent_permit_id=?",
                        (row["permit_id"],),
                    ).fetchall()
                    if not bindings:
                        raise DeletionBlocked("season pack has no verified episode bindings")
                    for binding in bindings:
                        bound = re.fullmatch(r"S([0-9]{2,})E[0-9]{2,}", binding[0])
                        if bound is None or int(bound[1]) != payload["season"]:
                            raise DeletionBlocked("season pack serves another season")
                    scope = bindings[0][0]
                else:
                    bindings = [(scope,)]
                sources.append((dict(row), scope, [b[0] for b in bindings]))
        return sources

    async def _delete_pending_season_sources(self, job: dict[str, Any]) -> None:
        payload = self._season_payload(job)
        _, episodes = await self._season_preflight(job)
        if any(e.get("episodeFileId") for e in episodes):
            raise DeletionBlocked("season still has imported files before torrent purge")
        sources = self._season_source_inventory(job)
        # Library children are gone before remaining pack bindings are tombstoned.
        # This preserves shared hardlinks during each child's existing deletion flow.
        for episode in episodes:
            self.jobs.tombstone(
                f"episode:tmdb:{payload['series_tmdb_id']}:S{payload['season']:02d}"
                f"E{episode['episodeNumber']:02d}", job["item_id"],
            )
        for row, scope, bindings in sources:
            self._guard_mount()
            if row["state"] == "authorized":
                if not PermitRegistry(
                    self.jobs.path, storage_registry=self.storage_registry,
                ).cancel_authorized(row["token"]):
                    raise DeletionRetryable("season admission changed during cancellation")
                continue
            for binding in bindings:
                self.jobs.tombstone(
                    f"episode:tmdb:{payload['series_tmdb_id']}:{binding}", job["item_id"],
                )
            result = self._json(await self.client.post(
                f"{self.gateway_url}/internal/delete-source", headers=self.gateway_headers,
                json={"permit_token": row["token"], "media_key": payload["media_key"],
                      "scope_key": scope},
            ))
            if not isinstance(result, dict) or result.get("state") not in {"deleted", "missing"}:
                raise DeletionBlocked("season source is still shared or unconfirmed")
        with sqlite3.connect(self.jobs.path) as connection:
            connection.execute(
                "UPDATE reservations SET state='cancelled', budget_bytes=0 WHERE media_key=?",
                (payload["media_key"],),
            )

    async def _delete_season_seerr(self, job: dict[str, Any]) -> None:
        payload = self._season_payload(job)
        with sqlite3.connect(self.jobs.path) as connection:
            sources = connection.execute(
                "SELECT q.source_id FROM requests q JOIN reservations r ON r.request_id=q.id "
                "WHERE r.media_key=?", (payload["media_key"],),
            ).fetchall()
        for (source,) in sources:
            match = re.fullmatch(r"([1-9][0-9]*):([0-9]+)", source)
            if match is None or int(match[2]) != payload["season"]:
                raise DeletionBlocked("Seerr season request identity is invalid")
            response = await self.client.get(
                f"{self.seerr_url}/api/v1/request/{match[1]}", headers=self.seerr_headers,
            )
            request = self._json(response, allow_missing=True)
            if request is None:
                continue
            media = request.get("media") if isinstance(request, dict) else None
            seasons = request.get("seasons") if isinstance(request, dict) else None
            if (
                not isinstance(media, dict) or request.get("id") != int(match[1])
                or media.get("tmdbId") != payload["series_tmdb_id"]
                or media.get("mediaType") != "tv" or not isinstance(seasons, list) or not seasons
                or any(not isinstance(s, dict) or type(s.get("seasonNumber")) is not int
                       for s in seasons)
            ):
                raise DeletionBlocked("Seerr request changed after season selection")
            if all(s["seasonNumber"] == payload["season"] for s in seasons):
                self._guard_mount()
                self._status(await self.client.delete(
                    f"{self.seerr_url}/api/v1/request/{match[1]}", headers=self.seerr_headers,
                ), allow_missing=True)

    def _season_metadata(self, job: dict[str, Any], *, must_exist: bool) -> list[Path]:
        path = self._season_folder(job, must_exist=False)
        metadata = job["payload"].get("metadata_files", [])
        result = []
        for entry in metadata:
            file = Path(entry["file_path"])
            if file.parent != path or not re.fullmatch(
                r"(?:season\.nfo|(?:folder|poster|banner|fanart|thumb|landscape)"
                r"\.(?:jpg|jpeg|png|webp))", file.name, re.IGNORECASE,
            ) or file.is_symlink():
                raise DeletionBlocked("season metadata escaped selected folder")
            physical = self._captured_path(file, entry['file_identity'])
            if not physical.exists():
                if must_exist:
                    raise DeletionBlocked("season metadata disappeared after capture")
                continue
            status = physical.stat()
            actual = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
            if actual != tuple(entry["file_identity"][k]
                               for k in ("device", "inode", "size", "mtime_ns")):
                raise DeletionBlocked("season metadata changed after capture")
            if not stat.S_ISREG(status.st_mode):
                raise DeletionBlocked("season metadata is not a regular file")
            result.append(file)
        return result

    def _remove_season_folder(self, job: dict[str, Any]) -> Path:
        path = self._season_folder(job, must_exist=False)
        if not path.exists():
            return path
        for file in self._season_metadata(job, must_exist=False):
            self._guard_mount()
            self._season_folder(job, must_exist=True)
            if file not in self._season_metadata(job, must_exist=False):
                continue
            file.unlink()
        try:
            self._guard_mount()
            if not self._season_folder(job, must_exist=False).exists():
                return path
            path.rmdir()  # Never recursively delete unknown videos or directory contents.
        except OSError as error:
            if error.errno in (errno.ENOTEMPTY, errno.EEXIST):
                raise DeletionBlocked("season folder contains uncaptured files") from error
            raise
        return path

    async def _process_season(self, job: dict[str, Any]) -> None:
        while True:
            self._guard_mount()
            stage = job["stage"]
            if stage == "queued":
                self._season_folder(job, must_exist=True)
                self._season_metadata(job, must_exist=True)
                self._season_source_inventory(job)
                await self._season_preflight(job)
                for child in self._season_children(job):
                    self._file(child, must_exist=True)
                    self._association(child)
                    await self._arr_preflight(child, file_exists=True)
                job = self.jobs.set_stage(job["item_id"], "validated")
            elif stage == "validated":
                await self._cancel_season(job)
                job = self.jobs.set_stage(job["item_id"], "tombstoned")
            elif stage == "tombstoned":
                if any(c["stage"] != "complete" for c in self._season_children(job)):
                    raise DeletionRetryable("season episode cleanup is still processing")
                await self._delete_pending_season_sources(job)
                job = self.jobs.set_stage(job["item_id"], "torrents_removed")
            elif stage == "torrents_removed":
                await self._delete_season_seerr(job)
                job = self.jobs.set_stage(job["item_id"], "seerr_removed")
            elif stage == "seerr_removed":
                path = self._remove_season_folder(job)
                await self._forget_jellyfin_item(job["item_id"], path)
                job = self.jobs.set_stage(job["item_id"], "jellyfin_removed")
            elif stage == "jellyfin_removed":
                self._status(await self.client.post(
                    f"{self.seerr_url}/api/v1/settings/jellyfin/sync", headers=self.seerr_headers,
                    json={"start": True, "cancel": False},
                ))
                self.jobs.set_stage(job["item_id"], "complete")
                return
            else:
                raise DeletionBlocked("unknown season deletion stage")

    async def _process(self, job: dict[str, Any]) -> None:
        self._active_job = job
        if job["item_type"] == "Season":
            await self._process_season(job)
            return
        parent_id = job.get("payload", {}).get("parent_item_id")
        if parent_id:
            parent = self.jobs.get(parent_id)
            if parent is None or parent["item_type"] != "Season" or parent["stage"] == "blocked":
                raise DeletionBlocked("season deletion parent is unavailable or blocked")
            if parent["stage"] in {"queued", "validated"}:
                raise DeletionRetryable("season batch has not passed preflight")
        while True:
            stage = job["stage"]
            self._guard_mount()
            if stage == "queued":
                self._file(job, must_exist=True)
                self._association(job)
                await self._arr_preflight(job, file_exists=True)
                job = self.jobs.set_stage(job["item_id"], "validated")
            elif stage == "validated":
                self._file(job, must_exist=True)
                self.jobs.tombstone(job["payload"]["media_key"], job["item_id"])
                job = self.jobs.set_stage(job["item_id"], "tombstoned")
            elif stage == "tombstoned":
                await self._delete_source(job)
                job = self.jobs.set_stage(job["item_id"], "torrents_removed")
            elif stage == "torrents_removed":
                await self._delete_arr(job)
                job = self.jobs.set_stage(job["item_id"], "arr_removed")
            elif stage == "arr_removed":
                await self._delete_seerr(job)
                job = self.jobs.set_stage(job["item_id"], "seerr_removed")
            elif stage == "seerr_removed":
                await self._delete_jellyfin(job)
                job = self.jobs.set_stage(job["item_id"], "jellyfin_removed")
            elif stage == "jellyfin_removed":
                await self._delete_seerr_media_cache(job)
                self.jobs.set_stage(job["item_id"], "complete")
                return
            else:
                raise DeletionBlocked("unknown deletion stage")

    async def run_once(self) -> str:
        """Process at most one item. Unsafe jobs stop; transient failures retry."""
        job = self.jobs.next_queued()
        if job is None:
            return "empty"
        try:
            await self._process(job)
        except DeletionBlocked as error:
            self.jobs.set_stage(job["item_id"], "blocked", str(error))
            LOGGER.warning("Jellyfin deletion %s blocked: %s", job["item_id"], error)
            return "blocked"
        except (DeletionRetryable, httpx.HTTPError, OSError, sqlite3.Error) as error:
            current = self.jobs.get(job["item_id"])
            if current is not None:
                self.jobs.set_stage(job["item_id"], str(current["stage"]), str(error))
                self.jobs.touch(job["item_id"])
            LOGGER.warning("Jellyfin deletion %s will retry: %s", job["item_id"], error)
            return "retry"
        return "complete"
