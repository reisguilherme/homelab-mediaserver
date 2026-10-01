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
_SEASON_METADATA = re.compile(
    r"^(?:season\.nfo|(?:folder|poster|banner|fanart|thumb|landscape)\.(?:jpg|jpeg|png|webp))$",
    re.IGNORECASE,
)


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
        elif item_type == "Season":
            payload = await self._capture_season(item, jellyfin_headers, str(user["Id"]))
            try:
                return self.jobs.enqueue_season(item_id, payload)
            except ValueError as error:
                raise DeletionCaptureError(409, str(error)) from error
        else:
            raise DeletionCaptureError(422, "delete movies, episodes or a season")
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

    def _season_directory(self, raw_path: object) -> tuple[Path, dict[str, int]]:
        if not isinstance(raw_path, str) or not raw_path:
            raise DeletionCaptureError(409, "season has no local directory")
        path = Path(raw_path)
        root = self.media_root / "tv"
        try:
            inside = path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
        except OSError as error:
            raise DeletionCaptureError(409, "season directory is unavailable") from error
        if (
            not path.is_absolute() or path == root or ".." in path.parts
            or not path.is_relative_to(root) or not inside
        ):
            raise DeletionCaptureError(409, "season path is outside the expected library")
        for component in (path, *path.parents):
            if component == self.media_root.parent:
                break
            if component.is_symlink():
                raise DeletionCaptureError(409, "season path contains a symlink")
        status = path.stat()
        if not stat.S_ISDIR(status.st_mode):
            raise DeletionCaptureError(409, "season path is not a directory")
        return path, {"device": status.st_dev, "inode": status.st_ino}

    async def _capture_season(
        self, item: dict[str, Any], jellyfin_headers: dict[str, str], user_id: str
    ) -> dict[str, Any]:
        directory, identity = self._season_directory(item.get("Path"))
        season = item.get("IndexNumber")
        series_id = item.get("SeriesId")
        item_id = item["Id"]
        if (
            type(season) is not int or season < 0
            or not isinstance(series_id, str) or not _ITEM_ID.fullmatch(series_id)
        ):
            raise DeletionCaptureError(409, "season coordinates unavailable")
        series = await self._get_json(
            f"{self.jellyfin_url}/Users/{user_id}/Items/{series_id}",
            headers=jellyfin_headers,
        )
        if not isinstance(series, dict) or (
            series.get("Id") != series_id or series.get("Type") != "Series"
        ):
            raise DeletionCaptureError(409, "Jellyfin series identity changed")
        series_directory, _series_identity = self._season_directory(series.get("Path"))
        if directory == series_directory or not directory.is_relative_to(series_directory):
            raise DeletionCaptureError(409, "season path is not a separate series subdirectory")
        provider_ids = series.get("ProviderIds") or {}
        if not isinstance(provider_ids, dict):
            raise DeletionCaptureError(409, "series identity unavailable")
        tvdb_id, tmdb_id = (str(provider_ids.get(name, "")) for name in ("Tvdb", "Tmdb"))
        if any(not value.isdecimal() or int(value) <= 0 for value in (tvdb_id, tmdb_id)):
            raise DeletionCaptureError(409, "series identity unavailable")
        if not self.sonarr_url or not self.sonarr_api_key:
            raise DeletionCaptureError(503, "Sonarr is not configured")
        arr_headers = {"X-Api-Key": self.sonarr_api_key}
        series_list = await self._get_json(
            f"{self.sonarr_url}/api/v3/series", headers=arr_headers,
        )
        matches = [
            entry for entry in series_list
            if isinstance(entry, dict) and str(entry.get("tvdbId")) == tvdb_id
        ] if isinstance(series_list, list) else []
        if len(matches) != 1 or type(matches[0].get("id")) is not int or matches[0]["id"] <= 0:
            raise DeletionCaptureError(409, "series does not match one Sonarr entry")
        if matches[0].get("path") != str(series_directory):
            raise DeletionCaptureError(409, "Sonarr series directory does not match Jellyfin")
        sonarr_series_id = matches[0]["id"]
        episodes = await self._get_json(
            f"{self.sonarr_url}/api/v3/episode", headers=arr_headers,
            params={"seriesId": str(sonarr_series_id)},
        )
        if not isinstance(episodes, list) or any(
            not isinstance(entry, dict)
            or type(entry.get("id")) is not int or entry["id"] <= 0
            or entry.get("seriesId") != sonarr_series_id
            or type(entry.get("seasonNumber")) is not int or entry["seasonNumber"] < 0
            or type(entry.get("episodeNumber")) is not int or entry["episodeNumber"] < 0
            or type(entry.get("episodeFileId")) is not int or entry["episodeFileId"] < 0
            for entry in episodes
        ):
            raise DeletionCaptureError(409, "Sonarr episode snapshot is incomplete")
        selected = [entry for entry in episodes if entry["seasonNumber"] == season]
        if (
            not selected or len({entry["id"] for entry in episodes}) != len(episodes)
            or len({entry["episodeNumber"] for entry in selected}) != len(selected)
        ):
            raise DeletionCaptureError(409, "Sonarr season identity is ambiguous")
        result = await self._get_json(
            f"{self.jellyfin_url}/Shows/{series_id}/Episodes", headers=jellyfin_headers,
            params={"userId": user_id, "seasonId": item_id, "fields": "Path",
                    "isMissing": "false"},
        )
        children = result.get("Items") if isinstance(result, dict) else None
        if (
            not isinstance(children, list) or not children
            or type(result.get("TotalRecordCount")) is not int
            or result["TotalRecordCount"] != len(children)
            or ("StartIndex" in result and result["StartIndex"] != 0)
        ):
            raise DeletionCaptureError(409, "Jellyfin season snapshot is incomplete")
        captured = []
        seen_ids: set[str] = set()
        seen_episode_ids: set[int] = set()
        seen_paths: set[Path] = set()
        for child in children:
            if not isinstance(child, dict) or (
                child.get("Type") != "Episode" or child.get("SeriesId") != series_id
                or child.get("SeasonId") != item_id or child.get("ParentIndexNumber") != season
                or type(child.get("IndexNumber")) is not int
                or not isinstance(child.get("Id"), str) or not _ITEM_ID.fullmatch(child["Id"])
                or child["Id"] in seen_ids or child["Id"] == item_id
            ):
                raise DeletionCaptureError(409, "Jellyfin child is outside the captured season")
            seen_ids.add(child["Id"])
            path, file_identity = self._file_identity(child.get("Path"), "tv")
            if not path.is_relative_to(directory) or path in seen_paths:
                raise DeletionCaptureError(409, "episode path is outside the captured season")
            seen_paths.add(path)
            matched = [
                entry for entry in selected if entry["episodeNumber"] == child["IndexNumber"]
            ]
            if len(matched) != 1 or matched[0]["episodeFileId"] <= 0:
                raise DeletionCaptureError(409, "episode does not match one Sonarr file")
            entry = matched[0]
            if entry["id"] in seen_episode_ids or sum(
                record["episodeFileId"] == entry["episodeFileId"] for record in episodes
            ) != 1:
                raise DeletionCaptureError(409, "episode file is shared with another episode")
            seen_episode_ids.add(entry["id"])
            episode_file = await self._get_json(
                f"{self.sonarr_url}/api/v3/episodefile/{entry['episodeFileId']}",
                headers=arr_headers,
            )
            if not isinstance(episode_file, dict) or (
                episode_file.get("id") != entry["episodeFileId"]
                or episode_file.get("path") != str(path)
                or episode_file.get("size") != file_identity["size"]
            ):
                raise DeletionCaptureError(409, "Sonarr file does not match Jellyfin")
            captured.append({"item_id": child["Id"], "payload": {
                "media_key": f"episode:tmdb:{tmdb_id}:S{season:02d}E{entry['episodeNumber']:02d}",
                "file_path": str(path), "file_identity": file_identity,
                "sonarr_series_id": sonarr_series_id, "sonarr_episode_id": entry["id"],
                "sonarr_episode_file_id": entry["episodeFileId"],
                "series_tmdb_id": int(tmdb_id), "season": season,
                "episode": entry["episodeNumber"], "parent_item_id": item_id,
            }})
        if seen_episode_ids != {entry["id"] for entry in selected if entry["episodeFileId"] > 0}:
            raise DeletionCaptureError(409, "Jellyfin season does not cover every Sonarr file")
        metadata_files = []
        for candidate in sorted(directory.iterdir()):
            if not _SEASON_METADATA.fullmatch(candidate.name):
                continue
            path, metadata_identity = self._file_identity(str(candidate), "tv")
            if metadata_identity["size"] > 50_000_000:
                raise DeletionCaptureError(409, "season metadata is not a small regular file")
            metadata_files.append({"file_path": str(path), "file_identity": metadata_identity})
        captured.sort(key=lambda child: child["payload"]["episode"])
        return {
            "media_key": f"season:tmdb:{tmdb_id}:{season}",
            "series_tmdb_id": int(tmdb_id), "series_tvdb_id": int(tvdb_id),
            "sonarr_series_id": sonarr_series_id, "season": season,
            "file_path": str(directory), "directory_identity": identity,
            "episodes": captured, "sonarr_episode_ids": sorted(entry["id"] for entry in selected),
            "metadata_files": metadata_files,
        }
