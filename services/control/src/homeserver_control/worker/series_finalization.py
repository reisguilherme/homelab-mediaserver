"""Validate completed episodes before asking Sonarr to import them."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

import httpx

from homeserver_control.gateway.permits import Permit, PermitRegistry
from homeserver_control.persistence.db import ReservationRepository

from .capacity_evidence import CapacityEvidence
from .finalization import (
    _SUBTITLE,
    _VIDEO,
    MovieFinalizer,
    _subtitle_has_content,
)
from .series_acquisition import _episode_tag, _single_episode_name
from .subdl import SubDLSource
from .subtitle_language import (
    audio_is_brazilian_portuguese,
    has_embedded_english_subtitle,
    is_brazilian_portuguese_subtitle,
    is_english_subtitle,
)
from .validation import ValidationError, validate_media

_SEASON_KEY = re.compile(r"season:tmdb:([1-9][0-9]*):([0-9]+)")


class SeriesFinalizer(MovieFinalizer):
    def __init__(
        self, *, repository: ReservationRepository, permits: PermitRegistry,
        torrent_root: str | Path, gateway_url: str, arr_token: str,
        sonarr_url: str, sonarr_api_key: str,
        media_root: str | Path = "/data/media/tv",
        client: httpx.AsyncClient | None = None,
        subtitle_source: SubDLSource | None = None,
        capacity_provider: Callable[[], Awaitable[CapacityEvidence]] | None = None,
        import_uid: int | None = None,
        import_gid: int | None = None,
    ) -> None:
        super().__init__(
            repository=repository, permits=permits, torrent_root=torrent_root,
            gateway_url=gateway_url, arr_token=arr_token,
            radarr_url=sonarr_url, radarr_api_key=sonarr_api_key,
            media_root=media_root, client=client, subtitle_source=subtitle_source,
            capacity_provider=capacity_provider,
            import_uid=import_uid, import_gid=import_gid,
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
        self, episode: dict[str, object], reservations_by_season: dict[int, str]
    ) -> bool:
        if episode.get("hasFile") is not True:
            return False
        season = episode["seasonNumber"]
        number = episode["episodeNumber"]
        reservation_id = reservations_by_season.get(season)
        if reservation_id is None:
            return False
        permit = self.permits.get_for_reservation(
            reservation_id, scope_key=_episode_tag(season, number)
        )
        return (
            permit is None or permit.state != "confirmed"
            or self.repository.episode_import_state(permit.permit_id) == "complete"
        )

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
        match = re.fullmatch(r"S([0-9]{2,})E([0-9]{2,})", permit.scope_key or "")
        if match is None:
            raise ValidationError("episode permit scope is invalid")
        self._validate_selected_episode_subtitles(
            permit, season=int(match.group(1)), number=int(match.group(2))
        )
        self._ensure_subtitle_for_video(video, permit, permit.scope_key)

    @staticmethod
    def _validate_selected_episode_subtitles(
        permit: Permit, *, season: int, number: int
    ) -> None:
        if any(
            PurePosixPath(name).suffix.lower() in _SUBTITLE
            and not _single_episode_name(PurePosixPath(name).name, season, number)
            for name in permit.selected_files
        ):
            raise ValidationError("selected subtitle belongs to another episode")

    async def _validate_download(
        self, permit: Permit, *, season: int, number: int
    ) -> tuple[str, bool, bool, bool, str | None] | None:
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
        self._validate_selected_episode_subtitles(
            permit, season=season, number=number
        )
        for relative in permit.selected_files:
            path = self._local_path(f"/data/torrents/{relative}")
            if relative not in sizes or path.stat().st_size != sizes[relative]:
                raise ValidationError("torrent file does not match qBittorrent metadata")
            if path != content and content not in path.parents:
                raise ValidationError("selected file is outside torrent content")
            selected.append(path)
        videos = [path for path in selected if path.suffix.lower() in _VIDEO]
        subtitle_files = [path for path in selected if path.suffix.lower() in _SUBTITLE]
        if any(
            not (is_brazilian_portuguese_subtitle(str(path))
                 or is_english_subtitle(str(path)))
            for path in subtitle_files
        ):
            raise ValidationError("selected subtitle is not Brazilian Portuguese or English")
        brazilian_subtitles = [
            path for path in subtitle_files
            if is_brazilian_portuguese_subtitle(str(path))
        ]
        english_subtitles = [
            path for path in subtitle_files if is_english_subtitle(str(path))
        ]
        if (
            len(videos) != 1 or not _single_episode_name(videos[0].name, season, number)
        ):
            raise ValidationError("episode video or Brazilian Portuguese subtitle is missing")
        validated = validate_media(videos[0], maximum_bytes=permit.budget_bytes)
        if not validated.probe.audio_languages:
            raise ValidationError("episode has no audio stream")
        if subtitle_files and any(not _subtitle_has_content(path) for path in subtitle_files):
            raise ValidationError("selected subtitle content is not valid")
        brazilian_ready = bool(brazilian_subtitles) or self.subtitle_store.get(
            permit.reservation_id, permit.scope_key, permit.infohash, language="BR_PT"
        ) is not None
        english_ready = bool(english_subtitles) or self.subtitle_store.get(
            permit.reservation_id, permit.scope_key, permit.infohash, language="EN"
        ) is not None or has_embedded_english_subtitle(validated.probe)
        original_ptbr = audio_is_brazilian_portuguese(validated.probe)
        name = torrent.get("name")
        return (
            content_path, brazilian_ready, english_ready, original_ptbr,
            name if isinstance(name, str) else None,
        )

    async def finalize(self, media_key: str, reservation_id: str) -> str:
        reservation = self.repository.active_reservation(reservation_id)
        if reservation is None or reservation["media_key"] != media_key:
            return "reservation_inactive"
        match = _SEASON_KEY.fullmatch(media_key)
        if match is None:
            return "unsupported_media"
        tmdb_id, season = int(match.group(1)), int(match.group(2))
        episodes = await self._episodes(tmdb_id)
        reservations_by_season = self._requested_seasons(tmdb_id)
        reservations_by_season.setdefault(season, reservation_id)
        chronological = sorted(
            (item for item in episodes
             if isinstance(item.get("seasonNumber"), int)
             and not isinstance(item["seasonNumber"], bool)
             and item["seasonNumber"] in reservations_by_season
             and isinstance(item.get("episodeNumber"), int)
             and not isinstance(item["episodeNumber"], bool)
             and item["episodeNumber"] > 0),
            key=lambda item: (item["seasonNumber"], item["episodeNumber"]),
        )
        known_seasons = {item["seasonNumber"] for item in chronological}
        prior_season_missing = any(
            number < season and number not in known_seasons
            for number in reservations_by_season
        )
        first_missing = next(
            (item for item in chronological
             if not self._episode_imported(item, reservations_by_season)), None
        )
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
            if prior_season_missing:
                return "waiting_previous_season"
            if first_missing is not None and (
                first_missing["seasonNumber"], first_missing["episodeNumber"]
            ) < (season, number):
                return (
                    "waiting_previous_season"
                    if first_missing["seasonNumber"] < season
                    else "waiting_previous_episode"
                )
            state = self.repository.episode_import_state(permit.permit_id)
            if state == "complete":
                seen_complete = True
                continue
            if state is not None:
                if state not in {"accepted", "accepted_copy"}:
                    return "import_uncertain"
                if episode.get("hasFile") is not True:
                    return "import_pending"
                video = await self._imported_video(episode)
                if not self._import_matches_source(
                    video, permit, copy_allowed=state == "accepted_copy"
                ):
                    return "import_uncertain"
                self._ensure_subtitle(video, permit)
                self.repository.complete_episode_import(permit.permit_id)
                return "complete"
            if episode.get("hasFile") is True:
                return "already_imported_without_validation"
            validated_download = await self._validate_download(
                permit, season=season, number=number
            )
            if validated_download is None:
                return "downloading"
            content_path, brazilian_ready, english_ready, original_ptbr, title = validated_download
            if not original_ptbr and not brazilian_ready:
                if self.subtitle_source is not None and title:
                    found = await self.subtitle_source.fetch(
                        tmdb_id=int(match.group(1)), release_title=title,
                        season=season, episode=number,
                    )
                    if found is not None:
                        self.subtitle_store.put(
                            reservation_id, permit.scope_key, permit.infohash, found
                        )
                        brazilian_ready = True
            if not original_ptbr and not brazilian_ready and not english_ready:
                if self.subtitle_source is not None and title:
                    found = await self.subtitle_source.fetch(
                        tmdb_id=int(match.group(1)), release_title=title,
                        season=season, episode=number, language="EN",
                    )
                    if found is not None:
                        self.subtitle_store.put(
                            reservation_id, permit.scope_key, permit.infohash, found,
                            language="EN",
                        )
                        english_ready = True
                if not english_ready:
                    return "waiting_subtitles"
            if not await self._hardlink_import_enabled():
                return "import_guard"
            selected = [
                self._local_path(f"/data/torrents/{relative}")
                for relative in permit.selected_files
            ]
            video = next(
                (path for path in selected if path.suffix.lower() in _VIDEO), None
            )
            if video is None:
                raise ValidationError("permit does not identify one episode video")
            hardlink_ready = self._can_hardlink_video(video)
            if not hardlink_ready and not await self._copy_fallback_fits(selected):
                return "waiting_space"
            if not self.repository.claim_episode_import(
                permit.permit_id, copy_allowed=not hardlink_ready
            ):
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
