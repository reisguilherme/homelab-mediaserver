"""Validate completed episodes before asking Sonarr to import them."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import httpx

from homeserver_control.domain.policy import EPISODE_LIMIT_BYTES
from homeserver_control.gateway.permits import Permit, PermitRegistry
from homeserver_control.persistence.db import ReservationRepository

from .finalization import _SUBTITLE, _VIDEO, MovieFinalizer, _subtitle_has_content
from .imports import import_hardlink
from .series_acquisition import _episode_tag, _single_episode_name
from .subtitle_language import is_brazilian_portuguese_subtitle
from .validation import ValidationError, validate_media

_SEASON_KEY = re.compile(r"season:tmdb:([1-9][0-9]*):([0-9]+)")


class SeriesFinalizer(MovieFinalizer):
    def __init__(
        self, *, repository: ReservationRepository, permits: PermitRegistry,
        torrent_root: str | Path, gateway_url: str, arr_token: str,
        sonarr_url: str, sonarr_api_key: str,
        media_root: str | Path = "/data/media/tv",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            repository=repository, permits=permits, torrent_root=torrent_root,
            gateway_url=gateway_url, arr_token=arr_token,
            radarr_url=sonarr_url, radarr_api_key=sonarr_api_key,
            media_root=media_root, client=client,
        )
        self.sonarr_url = sonarr_url.rstrip("/")

    async def _episodes(self, tmdb_id: int) -> list[dict[str, object]]:
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/series", headers=self.radarr_headers
        )
        response.raise_for_status()
        series = response.json()
        if not isinstance(series, list):
            raise ValidationError("Sonarr series lookup is invalid")
        match = next(
            (item for item in series if isinstance(item, dict)
             and item.get("tmdbId") == tmdb_id and isinstance(item.get("id"), int)),
            None,
        )
        if match is None:
            return []
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/episode",
            params={"seriesId": match["id"]}, headers=self.radarr_headers,
        )
        response.raise_for_status()
        episodes = response.json()
        if not isinstance(episodes, list):
            raise ValidationError("Sonarr episode lookup is invalid")
        return [item for item in episodes if isinstance(item, dict)]

    async def _imported_video(self, episode: dict[str, object]) -> Path:
        file_id = episode.get("episodeFileId")
        if not isinstance(file_id, int) or file_id <= 0:
            raise ValidationError("Sonarr episode file id is missing")
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/episodefile/{file_id}",
            headers=self.radarr_headers,
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload.get("path") if isinstance(payload, dict) else None
        prefix = "/data/media/tv/"
        if not isinstance(raw, str) or not raw.startswith(prefix):
            raise ValidationError("Sonarr episode file is outside the TV library")
        relative = PurePosixPath(raw[len(prefix):])
        if any(part in {"..", ".", ""} for part in relative.parts):
            raise ValidationError("Sonarr episode file path is invalid")
        root = self.media_root.resolve(strict=True)
        path = self.media_root.joinpath(*relative.parts).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValidationError("Sonarr episode file is outside the TV library")
        return path

    def _ensure_subtitle(self, video: Path, permit: Permit) -> None:
        subtitles = [
            self._local_path(f"/data/torrents/{name}") for name in permit.selected_files
            if PurePosixPath(name).suffix.lower() in _SUBTITLE
            and is_brazilian_portuguese_subtitle(name)
        ]
        source = next((path for path in subtitles if _subtitle_has_content(path)), None)
        if source is None:
            raise ValidationError("Brazilian Portuguese subtitle vanished after import")
        target = video.with_name(f"{video.stem}.pt-BR{source.suffix.lower()}")
        if target.exists():
            if target.is_symlink() or not _subtitle_has_content(target):
                raise ValidationError("existing TV subtitle is invalid")
            return
        import_hardlink(source, target)

    async def _validate_download(
        self, permit: Permit, *, season: int, number: int
    ) -> str | None:
        response = await self.client.get(
            f"{self.gateway_url}/api/v2/torrents/info",
            params={"hashes": permit.infohash}, headers=self.gateway_headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValidationError("gateway torrent response is invalid")
        torrent = next(
            (item for item in payload if isinstance(item, dict)
             and item.get("hash", "").lower() == permit.infohash),
            None,
        )
        if torrent is None:
            return None
        if torrent.get("progress") != 1 or torrent.get("amount_left") != 0:
            return None
        content_path = torrent.get("content_path")
        if not isinstance(content_path, str):
            raise ValidationError("torrent content path is missing")
        content = self._local_path(content_path)
        response = await self.client.get(
            f"{self.gateway_url}/api/v2/torrents/files",
            params={"hash": permit.infohash}, headers=self.gateway_headers,
        )
        response.raise_for_status()
        files = response.json()
        if not isinstance(files, list):
            raise ValidationError("gateway file response is invalid")
        sizes = {
            item["name"]: item["size"] for item in files
            if isinstance(item, dict) and isinstance(item.get("name"), str)
            and isinstance(item.get("size"), int)
        }
        selected = []
        for relative in permit.selected_files:
            path = self._local_path(f"/data/torrents/{relative}")
            if relative not in sizes or path.stat().st_size != sizes[relative]:
                raise ValidationError("torrent file does not match qBittorrent metadata")
            if path != content and content not in path.parents:
                raise ValidationError("selected file is outside torrent content")
            selected.append(path)
        videos = [path for path in selected if path.suffix.lower() in _VIDEO]
        subtitles = [path for path in selected if path.suffix.lower() in _SUBTITLE
                     and is_brazilian_portuguese_subtitle(str(path))]
        if (
            len(videos) != 1 or not _single_episode_name(str(videos[0]), season, number)
            or not subtitles
        ):
            raise ValidationError("episode video or Brazilian Portuguese subtitle is missing")
        validated = validate_media(videos[0], maximum_bytes=EPISODE_LIMIT_BYTES)
        if not validated.probe.audio_languages:
            raise ValidationError("episode has no audio stream")
        if not any(_subtitle_has_content(path) for path in subtitles):
            raise ValidationError("Brazilian Portuguese subtitle content is not valid")
        return content_path

    async def finalize(self, media_key: str, reservation_id: str) -> str:
        reservation = self.repository.active_reservation(reservation_id)
        if reservation is None or reservation["media_key"] != media_key:
            return "reservation_inactive"
        match = _SEASON_KEY.fullmatch(media_key)
        if match is None:
            return "unsupported_media"
        season = int(match.group(2))
        episodes = await self._episodes(int(match.group(1)))
        seen_complete = False
        for episode in sorted(
            (item for item in episodes if item.get("seasonNumber") == season
             and isinstance(item.get("episodeNumber"), int)),
            key=lambda item: item["episodeNumber"],
        ):
            number = episode["episodeNumber"]
            permit = self.permits.get_for_reservation(
                reservation_id, scope_key=_episode_tag(season, number)
            )
            if permit is None or permit.state != "confirmed":
                continue
            state = self.repository.episode_import_state(permit.permit_id)
            if state == "complete":
                seen_complete = True
                continue
            if state is not None:
                if state != "accepted":
                    return "import_uncertain"
                if episode.get("hasFile") is not True:
                    return "import_pending"
                video = await self._imported_video(episode)
                self._ensure_subtitle(video, permit)
                self.repository.complete_episode_import(permit.permit_id)
                return "complete"
            if episode.get("hasFile") is True:
                return "already_imported_without_validation"
            content_path = await self._validate_download(
                permit, season=season, number=number
            )
            if content_path is None:
                return "downloading"
            if not self.repository.claim_episode_import(permit.permit_id):
                return "import_pending"
            response = await self.client.post(
                f"{self.sonarr_url}/api/v3/command",
                headers=self.radarr_headers,
                json={
                    "name": "DownloadedEpisodesScan", "path": content_path,
                    "downloadClientId": permit.infohash.upper(), "importMode": "Copy",
                },
            )
            response.raise_for_status()
            command = response.json()
            if not isinstance(command, dict) or not isinstance(command.get("id"), int):
                raise ValidationError("Sonarr import response is invalid")
            self.repository.record_episode_import(permit.permit_id, str(command["id"]))
            return "import_requested"
        return "complete" if seen_complete else "not_dispatched"
