"""Validate completed episodes before asking Sonarr to import them."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

import httpx

from homeserver_control.gateway.permits import Permit, PermitRegistry
from homeserver_control.persistence.db import ReservationRepository

from .audio_audit import audit_download_audio
from .capacity_evidence import CapacityEvidence
from .finalization import (
    _SUBTITLE,
    _VIDEO,
    MovieFinalizer,
    _subtitle_has_content,
)
from .series_acquisition import _episode_aired, _episode_tag, _season_active
from .series_identity import SeriesIdentityResolver
from .source_probe import canonical_quality
from .subdl import (
    SubDLSource,
    _episode_parts,
    _movie_editions,
    _release_key,
    _source_family,
)
from .subtitle_language import (
    SubtitlePolicy,
    has_embedded_brazilian_subtitle,
    has_embedded_english_subtitle,
    is_brazilian_portuguese_subtitle,
    is_english_subtitle,
)
from .subtitle_recovery import recover_subtitle
from .validation import ValidationError, validate_media

_SEASON_KEY = re.compile(r"season:tmdb:([1-9][0-9]*):([0-9]+)")


class SeriesFinalizer(MovieFinalizer):
    library = 'tv'

    def __init__(
        self,
        *,
        repository: ReservationRepository,
        permits: PermitRegistry,
        torrent_root: str | Path,
        gateway_url: str,
        arr_token: str,
        sonarr_url: str,
        sonarr_api_key: str,
        media_root: str | Path = "/data/media/tv",
        client: httpx.AsyncClient | None = None,
        subtitle_source: SubDLSource | None = None,
        subtitle_policy: SubtitlePolicy | None = None,
        capacity_provider: Callable[[], Awaitable[CapacityEvidence]] | None = None,
        import_uid: int | None = None,
        import_gid: int | None = None,
        is_tombstoned: Callable[[str], bool] | None = None,
        jellyfin_url: str | None = None,
        jellyfin_api_key: str | None = None,
        series_identity: SeriesIdentityResolver | None = None,
    ) -> None:
        super().__init__(
            repository=repository,
            permits=permits,
            torrent_root=torrent_root,
            gateway_url=gateway_url,
            arr_token=arr_token,
            radarr_url=sonarr_url,
            radarr_api_key=sonarr_api_key,
            media_root=media_root,
            client=client,
            subtitle_source=subtitle_source,
            subtitle_policy=subtitle_policy,
            capacity_provider=capacity_provider,
            import_uid=import_uid,
            import_gid=import_gid,
            jellyfin_url=jellyfin_url,
            jellyfin_api_key=jellyfin_api_key,
        )
        self.sonarr_url = sonarr_url.rstrip("/")
        self.series_identity = series_identity or SeriesIdentityResolver()
        self.is_tombstoned = is_tombstoned or (lambda _key: False)

    async def _episodes(
        self, tmdb_id: int, *, media_context: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/series", headers=self.radarr_headers
        )
        response.raise_for_status()
        series = response.json()
        if not isinstance(series, list):
            raise ValidationError("Sonarr series lookup is invalid")
        match = self.series_identity.resolve(series, tmdb_id)
        if match is None:
            return []
        if media_context is not None:
            media_context.update(match)
            media_context.pop("_homeserver_subtitle_titles", None)
            titles = self.series_identity.subtitle_titles(series, tmdb_id)
            if titles:
                media_context["_homeserver_subtitle_titles"] = titles
        self._series_directory = match.get('path')
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/episode",
            params={"seriesId": match["id"]},
            headers=self.radarr_headers,
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
        episode: dict[str, object],
        reservations_by_season: dict[int, str],
        series_tmdb_id: int,
    ) -> bool:
        season = episode["seasonNumber"]
        number = episode["episodeNumber"]
        if (
            self.is_tombstoned(f"season:tmdb:{series_tmdb_id}:{season}")
            or self.is_tombstoned(f"episode:tmdb:{series_tmdb_id}:{_episode_tag(season, number)}")
        ):
            return True
        if episode.get("hasFile") is not True:
            return False
        reservation_id = reservations_by_season.get(season)
        if reservation_id is None:
            return False
        permit = self.permits.get_for_reservation(
            reservation_id, scope_key=_episode_tag(season, number)
        )
        return (
            permit is None
            or permit.state != "confirmed"
            or self.repository.episode_import_state(permit.permit_id) in {
                "complete", "subtitles_pending", "subtitles_pending_copy",
            }
        )

    def _episode_active(self, media_key: str, permit: Permit) -> bool:
        return (
            _season_active(
                self.repository, permit.reservation_id, media_key, self.is_tombstoned,
            )
            and self.permits.probe_reservation_active(permit)
            and self.permits.active_source(permit)
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
        relative = PurePosixPath(raw[len(prefix) :])
        if any(part in {"..", ".", ""} for part in relative.parts):
            raise ValidationError("Sonarr episode file path is invalid")
        root = self.media_root.resolve(strict=True)
        path = self.media_root.joinpath(*relative.parts).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValidationError("Sonarr episode file is outside the TV library")
        return path

    def _ensure_subtitle(
        self, video: Path, permit: Permit,
        *, subtitle_policy: SubtitlePolicy | None = None,
    ) -> None:
        match = re.fullmatch(r"S([0-9]{2,})E([0-9]{2,})", permit.scope_key or "")
        if match is None:
            raise ValidationError("episode permit scope is invalid")
        self._validate_selected_episode_subtitles(
            permit, season=int(match.group(1)), number=int(match.group(2))
        )
        self._ensure_subtitle_for_video(
            video, permit, permit.scope_key, subtitle_policy=subtitle_policy,
        )

    def _validate_selected_episode_subtitles(
        self, permit: Permit, *, season: int, number: int,
    ) -> None:
        if permit.scope_key != _episode_tag(season, number):
            raise ValidationError("episode permit scope is invalid")
        if any(
            PurePosixPath(name).suffix.lower() in _SUBTITLE
            and not self.permits.episode_identity_matches(permit, name)
            for name in permit.selected_files
        ):
            raise ValidationError("selected subtitle belongs to another episode")

    async def _validate_download(
        self, permit: Permit, *, season: int, number: int,
        subtitle_policy: SubtitlePolicy | None = None,
        media_context: dict[str, object] | None = None,
    ) -> tuple[str, bool, bool, bool, str | None, float | None] | None:
        policy = subtitle_policy or self.subtitle_policy
        response = await self.client.get(
            f"{self.gateway_url}/api/v2/torrents/info",
            params={"hashes": permit.infohash},
            headers=self.gateway_headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValidationError("gateway torrent response is invalid")
        torrent = next(
            (
                item
                for item in payload
                if isinstance(item, dict) and item.get("hash", "").lower() == permit.infohash
            ),
            None,
        )
        if torrent is None:
            return None
        if permit.season_pack_parent_id is None and (
            torrent.get("progress") != 1 or torrent.get("amount_left") != 0
        ):
            return None
        content_path = torrent.get("content_path")
        if not isinstance(content_path, str):
            raise ValidationError("torrent content path is missing")
        response = await self.client.get(
            f"{self.gateway_url}/api/v2/torrents/files",
            params={"hash": permit.infohash},
            headers=self.gateway_headers,
        )
        response.raise_for_status()
        files = response.json()
        if not isinstance(files, list):
            raise ValidationError("gateway file response is invalid")
        if permit.season_pack_parent_id is not None:
            completed = {
                item["name"] for item in files if isinstance(item, dict)
                and isinstance(item.get("name"), str) and item.get("progress") == 1
            }
            if not set(permit.selected_files).issubset(completed):
                return None
        content = self._content_path(permit, content_path)
        sizes = {
            item["name"]: item["size"]
            for item in files
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("size"), int)
        }
        selected = []
        self._validate_selected_episode_subtitles(permit, season=season, number=number)
        for relative in permit.selected_files:
            path = self._source_path(permit, relative)
            if relative not in sizes or path.stat().st_size != sizes[relative]:
                raise ValidationError("torrent file does not match qBittorrent metadata")
            if path != content and content not in path.parents:
                raise ValidationError("selected file is outside torrent content")
            selected.append(path)
        videos = [path for path in selected if path.suffix.lower() in _VIDEO]
        subtitle_files = [path for path in selected if path.suffix.lower() in _SUBTITLE]
        if any(not policy.matches(str(path)) for path in subtitle_files):
            raise ValidationError("selected subtitle is not Brazilian Portuguese or English")
        brazilian_subtitles = [
            path for path in subtitle_files if is_brazilian_portuguese_subtitle(str(path))
        ]
        english_subtitles = [path for path in subtitle_files if is_english_subtitle(str(path))]
        if len(videos) != 1 or not self.permits.episode_identity_matches(
            permit, next(
                name for name in permit.selected_files
                if PurePosixPath(name).suffix.lower() in _VIDEO
            ),
        ):
            raise ValidationError("episode video or Brazilian Portuguese subtitle is missing")
        validated = validate_media(videos[0], maximum_bytes=permit.budget_bytes)
        if not validated.probe.audio_languages:
            raise ValidationError("episode has no audio stream")
        audit_download_audio(self.repository, permit, validated.probe)
        if subtitle_files and any(not _subtitle_has_content(path) for path in subtitle_files):
            raise ValidationError("selected subtitle content is not valid")
        brazilian_ready = (
            bool(brazilian_subtitles)
            or has_embedded_brazilian_subtitle(validated.probe)
            or self._stored_subtitle(
                permit, permit.scope_key, language="BR_PT"
            )
            is not None
        )
        english_ready = (
            bool(english_subtitles)
            or self._stored_subtitle(
                permit, permit.scope_key, language="EN"
            )
            is not None
            or (
                policy.allow_generic_english
                and has_embedded_english_subtitle(validated.probe)
            )
        )
        original_ptbr = policy.waives_subtitles(validated.probe)
        name = torrent.get("name")
        title = name if isinstance(name, str) else None
        if permit.season_pack_parent_id is not None:
            title = self._pack_subtitle_title(
                permit, videos[0], media_context or {}, season=season, number=number,
                pack_title=title,
            )
        return (
            # Scanning the complete pack folder lets Sonarr import later files
            # before this controller validates their subtitles and episode order.
            permit.destination.rstrip('/') + '/' + next(
                relative for relative in permit.selected_files
                if PurePosixPath(relative).suffix.lower() in _VIDEO
            )
            if permit.season_pack_parent_id is not None else content_path,
            brazilian_ready,
            english_ready,
            original_ptbr,
            title,
            validated.probe.duration_seconds,
        )

    @staticmethod
    def _pack_subtitle_title(
        permit: Permit, video: Path, media_context: dict[str, object], *,
        season: int, number: int, pack_title: str | None = None,
    ) -> str | None:
        """Use the verified child and canonical series, never an episode-less pack name."""
        title = media_context.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        titles = [title]
        aliases = media_context.get("_homeserver_subtitle_titles")
        if isinstance(aliases, tuple):
            titles.extend(alias for alias in aliases if isinstance(alias, str) and alias.strip())
        title_keys = {
            _release_key(form) for name in titles for form in (
                name, unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode(),
            )
        }
        year = media_context.get("year")
        if isinstance(year, (int, str)) and re.fullmatch(r"[1-9][0-9]{3}", str(year)):
            title_keys.update(f"{key}{year}" for key in tuple(title_keys))
        parsed = _episode_parts(video.stem)
        if parsed is not None and (
            parsed[1:3] != (season, number)
            or parsed[0] and parsed[0] not in title_keys
        ):
            return None
        rank = getattr(permit, "quality_rank", None)
        family = ({1: "web", 2: "bluray", 3: "bluray"}.get(rank[0])
                  if isinstance(rank, tuple) and len(rank) >= 2 else None)
        file_family = _source_family(video.stem.lower())
        if family is None:
            family = file_family
        elif file_family is not None and file_family != family:
            return None
        if family is None:
            return None
        file_editions = _movie_editions(video.stem)
        pack_editions = _movie_editions(pack_title or "")
        if file_editions and pack_editions and file_editions != pack_editions:
            return None
        editions = file_editions | pack_editions
        if (parsed is not None and parsed[0] and file_family == family
                and editions == file_editions):
            return video.stem
        edition_names = {
            "extended": "EXTENDED", "directors_cut": "DIRECTORS.CUT", "unrated": "UNRATED",
            "uncut": "UNCUT", "special_edition": "SPECIAL.EDITION", "final_cut": "FINAL.CUT",
            "ultimate_cut": "ULTIMATE.CUT",
        }
        edition_label = ".".join(edition_names[item] for item in sorted(editions))
        label = {"web": "WEB-DL", "bluray": "BluRay", "webrip": "WEBRip",
                 "hdtv": "HDTV", "dvd": "DVD"}[family]
        return ".".join(part for part in (
            title.strip(), _episode_tag(season, number), edition_label, label,
        ) if part)

    @staticmethod
    def _native_import_quality_matches(quality: object, rank: object) -> bool:
        if (not isinstance(quality, dict) or not isinstance(rank, tuple) or len(rank) < 2
                or any(type(value) is not int for value in rank[:2])):
            return False
        native, revision = quality.get("quality"), quality.get("revision")
        valid = (
            isinstance(native, dict) and isinstance(revision, dict)
            and type(native.get("id")) is int and native["id"] > 0
            and isinstance(native.get("name"), str) and bool(native["name"].strip())
            and isinstance(native.get("source"), str)
            and type(native.get("resolution")) is int and native["resolution"] == rank[1]
            and type(revision.get("version")) is int and revision["version"] > 0
            and type(revision.get("real")) is int and revision["real"] >= 0
            and type(revision.get("isRepack")) is bool
        )
        if not valid:
            return False
        modifier = native.get("modifier", "none")
        if not isinstance(modifier, str) or modifier not in {"none", "remux"}:
            return False
        if native["source"] in {"web", "webdl"} and modifier != "none":
            return False
        # Native descriptor facts alone must establish remux; a filename cannot supply it.
        observed = canonical_quality({"quality": quality})
        return observed is not None and observed[:2] == rank[:2]

    async def _episode_import_command(
        self, permit: Permit, video: Path, content_path: str, *,
        media_context: dict, episode: dict, validated_title: str | None,
        publish_pending: bool = False,
    ) -> dict | None:
        command = {"name": "DownloadedEpisodesScan", "path": content_path, "importMode": "Copy"}
        if permit.season_pack_parent_id is None:
            return command | {"downloadClientId": permit.infohash.upper()}
        season, number = episode["seasonNumber"], episode["episodeNumber"]
        canonical = dict(media_context)
        aliases = canonical.pop("_homeserver_subtitle_titles", ())
        if self._pack_subtitle_title(
            permit, video, canonical, season=season, number=number,
        ) is not None:
            return command
        parsed = _episode_parts(video.stem)
        if (not isinstance(aliases, tuple) or not aliases or parsed is None or not parsed[0]):
            return command
        # Only an exact current authenticated alias may obtain a new native decision.
        # A synthesized canonical title or an unbound filename is insufficient.
        proved = self._pack_subtitle_title(
            permit, video, media_context, season=season, number=number,
        )
        if proved != video.stem or validated_title != proved:
            return None if publish_pending else command
        videos = [name for name in permit.selected_files
                  if PurePosixPath(name).suffix.lower() in _VIDEO]
        if (len(videos) != 1
                or content_path != permit.destination.rstrip('/') + '/' + videos[0]
                or not self.permits.episode_identity_matches(permit, videos[0])
                or type(media_context.get("id")) is not int or media_context["id"] <= 0
                or type(episode.get("id")) is not int or episode["id"] <= 0
                or ("seriesId" in episode and (
                    type(episode["seriesId"]) is not int
                    or episode["seriesId"] != media_context["id"]
                ))):
            return None
        response = await self.client.get(
            f"{self.sonarr_url}/api/v3/manualimport", headers=self.radarr_headers,
            params={"folder": content_path, "filterExistingFiles": "false"},
        )
        response.raise_for_status()
        try:
            candidates = response.json()
        except ValueError:
            return None
        matches = [item for item in candidates if isinstance(item, dict)
                   and item.get("path") == content_path] if isinstance(candidates, list) else []
        if len(matches) != 1:
            return None
        candidate = matches[0]
        episodes = candidate.get("episodes")
        if (candidate.get("series") is not None or candidate.get("seasonNumber") is not None
                or (episodes is not None and not (
                    isinstance(episodes, list) and not episodes
                ))
                or candidate.get("rejections") != [
                    {"reason": "Unknown Series", "type": "permanent"},
                ]
                or not self._native_import_quality_matches(
                    candidate.get("quality"), permit.quality_rank,
                )):
            return None
        languages = candidate.get("languages")
        if (not isinstance(languages, list) or not languages or any(
            not isinstance(item, dict) or type(item.get("id")) is not int or item["id"] < 0
            or not isinstance(item.get("name"), str) or not item["name"].strip()
            for item in languages
        ) or (candidate.get("releaseGroup") is not None
              and not isinstance(candidate["releaseGroup"], str))
                or ("indexerFlags" in candidate and (
                    type(candidate["indexerFlags"]) is not int or candidate["indexerFlags"] < 0
                )) or ("releaseType" in candidate
                       and not isinstance(candidate["releaseType"], str))):
            return None
        native_file = {key: candidate[key] for key in (
            "quality", "languages", "releaseGroup", "indexerFlags", "releaseType",
        ) if key in candidate}
        native_file.update(
            path=content_path, folderName=PurePosixPath(content_path).parent.name,
            downloadId=permit.infohash, seriesId=media_context["id"], episodeIds=[episode["id"]],
        )
        return {"name": "ManualImport", "importMode": "Copy", "files": [native_file]}

    async def finalize(self, media_key: str, reservation_id: str) -> str:
        if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
            return "reservation_inactive"
        match = _SEASON_KEY.fullmatch(media_key)
        if match is None:
            return "unsupported_media"
        tmdb_id, season = int(match.group(1)), int(match.group(2))
        publish_pending = self.repository.series_subtitle_publication_allowed(tmdb_id)
        media_context: dict[str, object] = {}
        episodes = await self._episodes(tmdb_id, media_context=media_context)
        subtitle_policy = await self._resolve_subtitle_policy(media_context)
        if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
            return "reservation_inactive"
        reservations_by_season = self._requested_seasons(tmdb_id)
        reservations_by_season.setdefault(season, reservation_id)
        chronological = sorted(
            (
                item
                for item in episodes
                if isinstance(item.get("seasonNumber"), int)
                and not isinstance(item["seasonNumber"], bool)
                and item["seasonNumber"] in reservations_by_season
                and isinstance(item.get("episodeNumber"), int)
                and not isinstance(item["episodeNumber"], bool)
                and item["episodeNumber"] > 0
                and item.get("monitored") is not False
                and _episode_aired(item) is not False
            ),
            key=lambda item: (item["seasonNumber"], item["episodeNumber"]),
        )
        known_seasons = {
            item["seasonNumber"] for item in episodes
            if isinstance(item.get("seasonNumber"), int)
            and not isinstance(item["seasonNumber"], bool)
            and item["seasonNumber"] in reservations_by_season
        }
        prior_season_missing = any(
            number < season and number not in known_seasons for number in reservations_by_season
        )
        first_missing = next(
            (
                item
                for item in chronological
                if not self._episode_imported(item, reservations_by_season, tmdb_id)
            ),
            None,
        )
        seen_complete = False
        seen_pending = False
        for episode in sorted(
            (
                item
                for item in chronological
                if item["seasonNumber"] == season
            ),
            key=lambda item: item["episodeNumber"],
        ):
            if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
                return "reservation_inactive"
            number = episode["episodeNumber"]
            if self.is_tombstoned(f"episode:tmdb:{tmdb_id}:{_episode_tag(season, number)}"):
                continue
            permit = self.permits.get_for_reservation(
                reservation_id, scope_key=_episode_tag(season, number)
            )
            if permit is None or permit.state != "confirmed":
                continue
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            if prior_season_missing:
                return "waiting_previous_season"
            if first_missing is not None and (
                first_missing["seasonNumber"],
                first_missing["episodeNumber"],
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
            if state in {"subtitles_pending", "subtitles_pending_copy"}:
                seen_pending = True
                continue
            if state is not None:
                if state not in {"accepted", "accepted_copy"}:
                    return "import_uncertain"
                if episode.get("hasFile") is not True:
                    return "import_pending"
                video = await self._imported_video(episode)
                if not self._episode_active(media_key, permit):
                    return "reservation_inactive"
                if not self._import_matches_source(
                    video, permit, copy_allowed=state == "accepted_copy"
                ):
                    return ('import_not_hardlinked' if self.storage_registry is not None
                            else 'import_uncertain')
                if not self._episode_active(media_key, permit):
                    return "reservation_inactive"
                if publish_pending:
                    validated = await self._validate_download(
                        permit, season=season, number=number, subtitle_policy=subtitle_policy,
                        media_context=media_context,
                    )
                    if not self._episode_active(media_key, permit):
                        return "reservation_inactive"
                    if validated is None:
                        return "downloading"
                    ready = await self._recover_episode_subtitle(
                        media_key, permit, validated, subtitle_policy=subtitle_policy,
                        fetch_external=False,
                    )
                    if not self._episode_active(media_key, permit):
                        return "reservation_inactive"
                    if not ready:
                        await self._notify_jellyfin(video)
                        if not self._episode_active(media_key, permit):
                            return "reservation_inactive"
                        self.repository.verify_episode_import(permit.permit_id)
                        return "subtitles_pending"
                self._ensure_subtitle(video, permit, subtitle_policy=subtitle_policy)
                await self._notify_jellyfin(video)
                if not self._episode_active(media_key, permit):
                    return "reservation_inactive"
                self.repository.complete_episode_import(permit.permit_id)
                return "complete"
            if episode.get("hasFile") is True:
                return "already_imported_without_validation"
            validated_download = await self._validate_download(
                permit, season=season, number=number, subtitle_policy=subtitle_policy,
                media_context=media_context,
            )
            if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
                return "reservation_inactive"
            if validated_download is None:
                return "downloading"
            content_path = validated_download[0]
            if (publish_pending and permit.season_pack_parent_id is not None
                    and validated_download[4] is None):
                return "import_guard"
            if not publish_pending and not await self._recover_episode_subtitle(
                media_key, permit, validated_download, subtitle_policy=subtitle_policy,
            ):
                return ("waiting_subtitles" if self._episode_active(media_key, permit)
                        else "reservation_inactive")
            if not await self._hardlink_import_enabled():
                return "import_guard"
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            selected = [
                self._source_path(permit, relative) for relative in permit.selected_files
            ]
            video = next((path for path in selected if path.suffix.lower() in _VIDEO), None)
            if video is None:
                raise ValidationError("permit does not identify one episode video")
            directory = None
            if self.storage_registry is not None:
                directory = self._import_directory(permit, self._series_directory)
                # Sonarr may create the season folder; probe existing season folders
                # too, where branch affinity and per-directory permissions differ.
                for candidate in directory.iterdir():
                    if candidate.is_dir() and re.fullmatch(
                        rf'Season\s+0*{season}', candidate.name, re.IGNORECASE,
                    ):
                        directory = candidate
                        break
            hardlink_ready = self._can_hardlink_video(video, permit=permit, directory=directory)
            if not hardlink_ready and not await self._copy_fallback_fits(selected):
                return 'import_guard' if self.storage_registry is not None else 'waiting_space'
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            command_body = await self._episode_import_command(
                permit, video, content_path, media_context=media_context, episode=episode,
                validated_title=validated_download[4], publish_pending=publish_pending,
            )
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            if command_body is None:
                return "import_guard"
            if not self.repository.claim_episode_import(
                permit.permit_id, copy_allowed=not hardlink_ready
            ):
                return "import_pending"
            response = await self.client.post(
                f"{self.sonarr_url}/api/v3/command",
                headers=self.radarr_headers,
                json=command_body,
            )
            response.raise_for_status()
            command = response.json()
            if not isinstance(command, dict) or not isinstance(command.get("id"), int):
                raise ValidationError("Sonarr import response is invalid")
            self.repository.record_episode_import(permit.permit_id, str(command["id"]))
            return "import_requested"
        return ("subtitles_pending" if seen_pending else
                "complete" if seen_complete else "not_dispatched")

    async def _recover_episode_subtitle(
        self, media_key: str, permit: Permit,
        validated: tuple[str, bool, bool, bool, str | None, float | None],
        *, subtitle_policy: SubtitlePolicy, fetch_external: bool = True,
    ) -> bool:
        _, brazilian_ready, english_ready, original_ptbr, title, duration = validated
        if original_ptbr:
            return self._episode_active(media_key, permit)
        scope = re.fullmatch(r"S([0-9]{2,})E([0-9]{2,})", permit.scope_key or "")
        if scope is None:
            raise ValidationError("episode permit scope is invalid")
        season, number = int(scope[1]), int(scope[2])

        async def fetch(language, match_mode):
            if not fetch_external or self.subtitle_source is None or not title:
                return None
            if permit.season_pack_parent_id is not None and duration is None:
                return None
            runtime = ({"episode_duration_seconds": duration}
                       if permit.season_pack_parent_id is not None else {})
            return await self.subtitle_source.fetch(
                tmdb_id=int(media_key.split(":")[2]), release_title=title,
                season=season, episode=number, language=language, match_mode=match_mode,
                **runtime,
            )

        present = {"BR_PT": brazilian_ready, "EN": english_ready}
        result = await recover_subtitle(subtitle_policy, fetch, already_present=present.get)
        if result is None or not self._episode_active(media_key, permit):
            return False
        if result.content:
            self.subtitle_store.put(
                permit.reservation_id, permit.scope_key, permit.infohash, result.content,
                language=result.language, replace_language=True,
            )
        return True

    async def retry_pending_subtitles(self, media_key: str, reservation_id: str) -> str:
        """Retry one oldest verified publication after the cycle's new import work."""
        if not _season_active(self.repository, reservation_id, media_key, self.is_tombstoned):
            return "reservation_inactive"
        match = _SEASON_KEY.fullmatch(media_key)
        if match is None:
            return "unsupported_media"
        tmdb_id, season = int(match[1]), int(match[2])
        media_context = {}
        episodes = await self._episodes(tmdb_id, media_context=media_context)
        subtitle_policy = await self._resolve_subtitle_policy(media_context)
        pending = self.repository.pending_episode_subtitles(reservation_id)
        candidates = {}
        for episode in episodes:
            number = episode.get("episodeNumber")
            if (episode.get("seasonNumber") != season or type(number) is not int or number <= 0):
                continue
            permit = self.permits.get_for_reservation(
                reservation_id, scope_key=_episode_tag(season, number),
            )
            if permit is not None and permit.state == "confirmed":
                candidates[permit.permit_id] = (episode, permit)
        for permit_id in pending:
            if permit_id not in candidates:
                continue
            episode, permit = candidates[permit_id]
            if not self._episode_active(media_key, permit):
                continue
            self.repository.mark_episode_subtitle_retry(permit_id)
            if episode.get("hasFile") is not True:
                return "import_pending"
            video = await self._imported_video(episode)
            state = self.repository.episode_import_state(permit_id)
            if not self._import_matches_source(
                video, permit, copy_allowed=state == "subtitles_pending_copy",
            ):
                return ('import_not_hardlinked' if self.storage_registry is not None
                        else 'import_uncertain')
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            await self._notify_jellyfin(video)
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            validated = await self._validate_download(
                permit, season=season, number=episode["episodeNumber"],
                subtitle_policy=subtitle_policy, media_context=media_context,
            )
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            if validated is None or not await self._recover_episode_subtitle(
                media_key, permit, validated, subtitle_policy=subtitle_policy,
            ):
                return ("waiting_subtitles" if self._episode_active(media_key, permit)
                        else "reservation_inactive")
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            self._ensure_subtitle(video, permit, subtitle_policy=subtitle_policy)
            await self._notify_jellyfin(video)
            if not self._episode_active(media_key, permit):
                return "reservation_inactive"
            self.repository.complete_episode_import(permit_id)
            return "complete"
        return "not_dispatched"
