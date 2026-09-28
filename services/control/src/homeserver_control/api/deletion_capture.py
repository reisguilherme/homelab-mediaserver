"""Capture an explicit Jellyfin delete before its catalog item disappears."""

from __future__ import annotations

import json
import re
import stat
import time
from pathlib import Path
from typing import Any

import httpx

from homeserver_control.persistence.deletion_jobs import DeletionJobStore

_ITEM_ID = re.compile(r"^[0-9a-fA-F]{32}$")


class DeletionCaptureError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        super().__init__(detail)


class DeletionAdmission:
    def __init__(
        self,
        *,
        jobs: DeletionJobStore,
        media_root: str | Path,
        snapshot_path: str | Path,
        filesystem_id: str,
        jellyfin_url: str,
        radarr_url: str,
        radarr_api_key: str,
        sonarr_url: str,
        sonarr_api_key: str,
        client: httpx.AsyncClient | None = None,
        http_timeout_seconds: float = 15,
    ) -> None:
        self.jobs = jobs
        self.media_root = Path(media_root)
        self.snapshot_path = Path(snapshot_path)
        self.filesystem_id = filesystem_id
        self.jellyfin_url = jellyfin_url.rstrip("/")
        self.radarr_url = radarr_url.rstrip("/")
        self.radarr_api_key = radarr_api_key
        self.sonarr_url = sonarr_url.rstrip("/")
        self.sonarr_api_key = sonarr_api_key
        self.client = client
        self.http_timeout_seconds = http_timeout_seconds

    async def _get_json(
        self, url: str, *, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> Any:
        async def request(client: httpx.AsyncClient) -> Any:
            try:
                response = await client.get(url, headers=headers, params=params)
            except httpx.HTTPError as error:
                raise DeletionCaptureError(503, "media service unavailable") from error
            if response.status_code in (401, 403):
                raise DeletionCaptureError(403, "Jellyfin session is not authorized")
            if response.status_code == 404:
                raise DeletionCaptureError(404, "media item was not found")
            if response.status_code != 200:
                raise DeletionCaptureError(503, "media service failed preflight")
            try:
                return response.json()
            except ValueError as error:
                raise DeletionCaptureError(503, "media service returned invalid data") from error

        if self.client is not None:
            return await request(self.client)
        async with httpx.AsyncClient(timeout=self.http_timeout_seconds) as client:
            return await request(client)

    def _check_mount(self) -> None:
        try:
            snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DeletionCaptureError(503, "media mount evidence unavailable") from error
        measured = snapshot.get("measured_at") if isinstance(snapshot, dict) else None
        if (
            not isinstance(snapshot, dict)
            or snapshot.get("filesystem_id") != self.filesystem_id
            or type(measured) not in (int, float)
            or not 0 <= time.time() - measured <= 30
        ):
            raise DeletionCaptureError(503, "media mount evidence is stale or mismatched")

    def _file_identity(self, raw_path: object, library: str) -> tuple[Path, dict[str, int]]:
        if not isinstance(raw_path, str) or not raw_path:
            raise DeletionCaptureError(409, "item has no local media file")
        path = Path(raw_path)
        library_root = self.media_root / library
        try:
            resolved_inside_library = path.resolve(strict=False).is_relative_to(
                library_root.resolve(strict=True)
            )
        except OSError as error:
            raise DeletionCaptureError(503, "media library is unavailable") from error
        if (
            not path.is_absolute()
            or not path.is_relative_to(library_root)
            or ".." in path.parts
            or not resolved_inside_library
        ):
            raise DeletionCaptureError(409, "item path is outside the expected library")
        for part in (path, *path.parents):
            if part == self.media_root.parent:
                break
            if part.is_symlink():
                raise DeletionCaptureError(409, "media path contains a symlink")
        try:
            file_stat = path.stat()
        except OSError as error:
            raise DeletionCaptureError(409, "media file is unavailable") from error
        if not stat.S_ISREG(file_stat.st_mode):
            raise DeletionCaptureError(409, "media file is not regular")
        return path, {
            "device": file_stat.st_dev,
            "inode": file_stat.st_ino,
            "size": file_stat.st_size,
            "mtime_ns": file_stat.st_mtime_ns,
            "nlink": file_stat.st_nlink,
        }

    async def capture(self, item_id: str, user_token: str) -> dict[str, object]:
        if not _ITEM_ID.fullmatch(item_id) or not user_token:
            raise DeletionCaptureError(401, "valid Jellyfin session required")
        jellyfin_headers = {"X-Emby-Token": user_token}
        user = await self._get_json(
            f"{self.jellyfin_url}/Users/Me", headers=jellyfin_headers
        )
        if not isinstance(user, dict) or not isinstance(user.get("Id"), str):
            raise DeletionCaptureError(403, "Jellyfin session is not authorized")
        policy = user.get("Policy") or {}
        if not (policy.get("IsAdministrator") and policy.get("EnableContentDeletion")):
            raise DeletionCaptureError(403, "Jellyfin administrator deletion permission required")
        self._check_mount()
        existing = self.jobs.get(item_id)
        if existing is not None:
            return existing
        item = await self._get_json(
            f"{self.jellyfin_url}/Users/{user['Id']}/Items/{item_id}",
            headers=jellyfin_headers,
        )
        if not isinstance(item, dict) or str(item.get("Id", "")).lower() != item_id.lower():
            raise DeletionCaptureError(409, "Jellyfin item identity changed")
        item_type = item.get("Type")
        if item_type == "Movie":
            payload = await self._capture_movie(item)
        elif item_type == "Episode":
            payload = await self._capture_episode(item, jellyfin_headers, str(user["Id"]))
        else:
            raise DeletionCaptureError(422, "delete individual movies or episodes")
        return self.jobs.enqueue(item_id, item_type, payload)

    async def _capture_movie(self, item: dict[str, Any]) -> dict[str, Any]:
        path, identity = self._file_identity(item.get("Path"), "movies")
        tmdb_id = str((item.get("ProviderIds") or {}).get("Tmdb", ""))
        if not tmdb_id.isdecimal() or int(tmdb_id) <= 0:
            raise DeletionCaptureError(409, "movie has no TMDB identity")
        if not self.radarr_url or not self.radarr_api_key:
            raise DeletionCaptureError(503, "Radarr is not configured")
        movies = await self._get_json(
            f"{self.radarr_url}/api/v3/movie",
            headers={"X-Api-Key": self.radarr_api_key},
            params={"tmdbId": tmdb_id},
        )
        matches = [
            entry for entry in movies if str(entry.get("tmdbId")) == tmdb_id
        ] if isinstance(movies, list) else []
        if len(matches) != 1:
            raise DeletionCaptureError(409, "movie does not match one Radarr entry")
        movie = matches[0]
        movie_file = movie.get("movieFile") or {}
        if (
            movie_file.get("path") != str(path)
            or movie_file.get("size") != identity["size"]
            or not isinstance(movie.get("id"), int)
            or not isinstance(movie_file.get("id"), int)
            or movie_file["id"] <= 0
        ):
            raise DeletionCaptureError(409, "Radarr file does not match Jellyfin")
        return {
            "media_key": f"movie:tmdb:{tmdb_id}",
            "file_path": str(path),
            "file_identity": identity,
            "radarr_id": movie["id"],
            "radarr_file_id": movie_file.get("id"),
            "tmdb_id": int(tmdb_id),
        }

    async def _capture_episode(
        self, item: dict[str, Any], jellyfin_headers: dict[str, str], user_id: str
    ) -> dict[str, Any]:
        path, identity = self._file_identity(item.get("Path"), "tv")
        series_item_id = item.get("SeriesId")
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        if (
            not isinstance(series_item_id, str)
            or not isinstance(season, int)
            or not isinstance(episode, int)
        ):
            raise DeletionCaptureError(409, "episode coordinates unavailable")
        series_item = await self._get_json(
            f"{self.jellyfin_url}/Users/{user_id}/Items/{series_item_id}",
            headers=jellyfin_headers,
        )
        tvdb_id = str((series_item.get("ProviderIds") or {}).get("Tvdb", ""))
        tmdb_id = str((series_item.get("ProviderIds") or {}).get("Tmdb", ""))
        if (
            not tvdb_id.isdecimal() or int(tvdb_id) <= 0
            or not tmdb_id.isdecimal() or int(tmdb_id) <= 0
        ):
            raise DeletionCaptureError(409, "series identity unavailable")
        if not self.sonarr_url or not self.sonarr_api_key:
            raise DeletionCaptureError(503, "Sonarr is not configured")
        arr_headers = {"X-Api-Key": self.sonarr_api_key}
        series_list = await self._get_json(
            f"{self.sonarr_url}/api/v3/series", headers=arr_headers
        )
        series_matches = [
            entry for entry in series_list if str(entry.get("tvdbId")) == tvdb_id
        ] if isinstance(series_list, list) else []
        if len(series_matches) != 1:
            raise DeletionCaptureError(409, "series does not match one Sonarr entry")
        sonarr_series_id = series_matches[0].get("id")
        if not isinstance(sonarr_series_id, int) or sonarr_series_id <= 0:
            raise DeletionCaptureError(409, "Sonarr series ID is invalid")
        episodes = await self._get_json(
            f"{self.sonarr_url}/api/v3/episode", headers=arr_headers,
            params={"seriesId": str(sonarr_series_id)},
        )
        matched = [
            entry for entry in episodes
            if entry.get("seasonNumber") == season and entry.get("episodeNumber") == episode
        ] if isinstance(episodes, list) else []
        if (
            len(matched) != 1
            or not isinstance(matched[0].get("id"), int)
            or not isinstance(matched[0].get("episodeFileId"), int)
        ):
            raise DeletionCaptureError(409, "episode does not match one Sonarr file")
        sonarr_episode = matched[0]
        episode_file_id = sonarr_episode["episodeFileId"]
        if sum(entry.get("episodeFileId") == episode_file_id for entry in episodes) != 1:
            raise DeletionCaptureError(409, "episode file is shared with another episode")
        episode_file = await self._get_json(
            f"{self.sonarr_url}/api/v3/episodefile/{episode_file_id}",
            headers=arr_headers,
        )
        if (
            episode_file.get("id") != episode_file_id
            or episode_file.get("path") != str(path)
            or episode_file.get("size") != identity["size"]
        ):
            raise DeletionCaptureError(409, "Sonarr file does not match Jellyfin")
        return {
            "media_key": f"episode:tmdb:{tmdb_id}:S{season:02d}E{episode:02d}",
            "file_path": str(path),
            "file_identity": identity,
            "sonarr_series_id": sonarr_series_id,
            "sonarr_episode_id": sonarr_episode["id"],
            "sonarr_episode_file_id": episode_file_id,
            "series_tmdb_id": int(tmdb_id),
            "season": season,
            "episode": episode,
        }
