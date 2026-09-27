"""Resume explicit Jellyfin deletions while preserving unrelated media."""

from __future__ import annotations

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

from homeserver_control.persistence.deletion_jobs import DeletionJobStore

LOGGER = logging.getLogger(__name__)
_MOVIE_KEY = re.compile(r"movie:tmdb:([1-9][0-9]*)$")
_EPISODE_KEY = re.compile(r"episode:tmdb:([1-9][0-9]*):S([0-9]{2})E([0-9]{2})$")
_SIDECAR = re.compile(
    r"^(?:\.(?:pt[-_]BR|en|eng)(?:\.(?:forced|hi|sdh|cc))?)?"
    r"\.(?:srt|ass|ssa|vtt|nfo|jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)


class DeletionBlocked(ValueError):
    """Identity or source scope is ambiguous and needs operator review."""


class DeletionRetryable(RuntimeError):
    """A dependency or fresh mount proof may become available later."""


class DeletionCoordinator:
    """Process one durable job per call, removing its source before Arr media."""

    def __init__(
        self, *, jobs: DeletionJobStore, media_root: str | Path,
        data_root: str | Path, snapshot_path: str | Path, filesystem_id: str,
        radarr_url: str, radarr_api_key: str, sonarr_url: str,
        sonarr_api_key: str, seerr_url: str, seerr_api_key: str,
        gateway_url: str, arr_token: str, jellyfin_url: str,
        jellyfin_api_key: str, client: httpx.AsyncClient | None = None,
        mount_check: Callable[[Path], bool] | None = None,
    ) -> None:
        if not all((
            filesystem_id, radarr_url, radarr_api_key, sonarr_url, sonarr_api_key,
            seerr_url, seerr_api_key, gateway_url, arr_token, jellyfin_url,
            jellyfin_api_key,
        )):
            raise ValueError("deletion coordinator requires service credentials and media UUID")
        self.jobs = jobs
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

    def _guard_mount(self) -> None:
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
                "sonarr_series_id", "sonarr_episode_id", "sonarr_episode_file_id",
                "series_tmdb_id", "season", "episode",
            )
            if match is None or (
                payload.get("series_tmdb_id"), payload.get("season"), payload.get("episode")
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
                path.is_absolute() and path != library
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
        try:
            current = path.stat()
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
            if rows[0]["filesystem_id"] != self.filesystem_id:
                raise DeletionBlocked("reservation belongs to another media filesystem")
            permit_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='gateway_permits' AND type='table'"
            ).fetchone()
            permits = [] if permit_table is None else connection.execute(
                """SELECT token, state FROM gateway_permits
                WHERE reservation_id = ? AND scope_key IS ?
                AND state IN ('authorized','dispatching','unknown','confirmed')""",
                (rows[0]["id"], scope),
            ).fetchall()
        if len(permits) > 1:
            raise DeletionBlocked("multiple active torrent permits match captured item")
        if permits and permits[0]["state"] != "confirmed":
            raise DeletionBlocked("torrent permit is not confirmed")
        return reservation_key, rows[0]["source_id"], permits[0]["token"] if permits else None

    @staticmethod
    def _status(response: httpx.Response, *, allow_missing: bool = False) -> bool:
        if response.status_code == 404 and allow_missing:
            return False
        if response.status_code in {400, 401, 403, 409, 422}:
            raise DeletionBlocked(f"{response.request.url.host} rejected deletion preflight")
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
            movie.get("id") != payload["radarr_id"]
            or movie.get("tmdbId") != payload["tmdb_id"]
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
        matches = [item for item in episodes if isinstance(item, dict)
                   and item.get("id") == payload["sonarr_episode_id"]]
        if len(matches) != 1 or (
            matches[0].get("seasonNumber") != payload["season"]
            or matches[0].get("episodeNumber") != payload["episode"]
            or matches[0].get("seriesId") != payload["sonarr_series_id"]
        ):
            raise DeletionBlocked("Sonarr episode identity changed")
        episode_file_id = payload["sonarr_episode_file_id"]
        if any(item.get("episodeFileId") == episode_file_id
               for item in episodes if item is not matches[0] and isinstance(item, dict)):
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
        response = await self.client.post(
            f"{self.gateway_url}/internal/delete-source",
            headers=self.gateway_headers,
            json={
                "permit_token": permit_token,
                "media_key": reservation_key,
                "scope_key": (
                    None if job["item_type"] == "Movie"
                    else f"S{job['payload']['season']:02d}E{job['payload']['episode']:02d}"
                ),
            },
        )
        result = self._json(response)
        if not isinstance(result, dict) or result.get("state") not in {"deleted", "missing"}:
            raise DeletionRetryable("gateway source removal was not confirmed")
        _path, remaining = self._file(job, must_exist=True)
        if remaining is not None and remaining.st_nlink > 1:
            raise DeletionBlocked("media has another hardlink after torrent cleanup")

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
        self._cleanup_sidecars(path)

    def _cleanup_sidecars(self, video: Path) -> None:
        """Remove only files sharing the selected video's complete stem."""
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
            suffix = sidecar.name[len(prefix):]
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
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
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
            f"{self.seerr_url}/api/v1/request/{source_id}", headers=self.seerr_headers,
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
            f"{self.seerr_url}/api/v1/request/{source_id}", headers=self.seerr_headers,
        )
        self._status(response, allow_missing=True)

    async def _jellyfin_item_present(self, item_id: str) -> bool:
        response = await self.client.get(
            f"{self.jellyfin_url}/Items",
            params={"Ids": item_id, "Recursive": "true"},
            headers=self.jellyfin_headers,
        )
        result = self._json(response)
        items = result.get("Items") if isinstance(result, dict) else None
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("Id"), str)
            or item["Id"].lower() != item_id.lower()
            for item in items
        ) or len(items) > 1:
            raise DeletionRetryable("Jellyfin returned an invalid item lookup")
        return bool(items)

    async def _delete_jellyfin(self, job: dict[str, Any]) -> None:
        self._guard_mount()
        path, remaining = self._file(job, must_exist=False)
        if remaining is not None:
            raise DeletionBlocked("media file still exists before Jellyfin cleanup")
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

    async def _process(self, job: dict[str, Any]) -> None:
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
