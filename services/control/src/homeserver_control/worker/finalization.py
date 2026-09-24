"""Validate completed movie payloads before asking Radarr to import."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath

import httpx

from homeserver_control.domain.subtitle_content import valid_srt
from homeserver_control.gateway.permits import Permit, PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import (
    MOVIE_FINALIZER_SOURCE,
    SubtitleArtifactStore,
)

from .capacity_evidence import CapacityEvidence
from .imports import import_hardlink, probe_hardlink_as
from .subdl import SubDLSource
from .subtitle_language import (
    audio_is_brazilian_portuguese,
    has_embedded_english_subtitle,
    is_brazilian_portuguese_subtitle,
    is_english_subtitle,
)
from .validation import ValidationError, validate_media

_VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov"}
_SUBTITLE = {".srt", ".ass", ".ssa", ".vtt"}


def _subtitle_has_content(path: Path) -> bool:
    if not path.is_file() or not 0 < path.stat().st_size <= 10_000_000:
        return False
    try:
        text = path.read_bytes()[:16384].decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    suffix = path.suffix.lower()
    if suffix == ".srt":
        return bool(re.search(r"\d\d:\d\d:\d\d[,\.]\d+\s*-->\s*\d\d:\d\d:\d\d", text))
    if suffix in {".ass", ".ssa"}:
        return "[Events]" in text and "Dialogue:" in text
    return "WEBVTT" in text and "-->" in text


def _same_content(left: Path, right: Path) -> bool:
    with left.open("rb") as source, right.open("rb") as imported:
        while chunk := source.read(1024 * 1024):
            if imported.read(len(chunk)) != chunk:
                return False
        return imported.read(1) == b""


def _write_external_subtitle(
    video: Path, content: bytes, media_root: Path, *, language: str = "BR_PT"
) -> None:
    if language not in {"BR_PT", "EN"}:
        raise ValidationError("unsupported subtitle language")
    if not valid_srt(content):
        raise ValidationError("subtitle content is not valid")
    suffix = "pt-BR" if language == "BR_PT" else "en"
    target = video.with_name(f"{video.stem}.{suffix}.srt")
    root = media_root.resolve(strict=True)
    if not target.parent.resolve(strict=True).is_relative_to(root):
        raise ValidationError("subtitle target escapes library")
    if target.exists() or target.is_symlink():
        if target.is_symlink() or target.read_bytes() != content:
            raise ValidationError("existing library subtitle differs from verified source")
        return
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{suffix}-", suffix=".tmp", dir=target.parent,
            delete=False,
        ) as output:
            temporary = output.name
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.link(temporary, target)
    except FileExistsError as error:
        if target.is_symlink() or target.read_bytes() != content:
            raise ValidationError(
                "existing library subtitle differs from verified source"
            ) from error
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


class MovieFinalizer:
    def __init__(
        self,
        *,
        repository: ReservationRepository,
        permits: PermitRegistry,
        torrent_root: str | Path,
        gateway_url: str,
        arr_token: str,
        radarr_url: str,
        radarr_api_key: str,
        media_root: str | Path = "/data/media/movies",
        client: httpx.AsyncClient | None = None,
        subtitle_source: SubDLSource | None = None,
        capacity_provider: Callable[[], Awaitable[CapacityEvidence]] | None = None,
        import_uid: int | None = None,
        import_gid: int | None = None,
    ) -> None:
        if not arr_token or not radarr_api_key:
            raise ValueError("gateway and Radarr credentials are required")
        self.repository = repository
        self.permits = permits
        self.subtitle_store = SubtitleArtifactStore(repository.path)
        self.torrent_root = Path(torrent_root)
        self.media_root = Path(media_root)
        self.gateway_url = gateway_url.rstrip("/")
        self.radarr_url = radarr_url.rstrip("/")
        self.gateway_headers = {"X-Arr-Token": arr_token}
        self.radarr_headers = {"X-Api-Key": radarr_api_key}
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self.subtitle_source = subtitle_source
        self.capacity_provider = capacity_provider
        self.import_uid = import_uid
        self.import_gid = import_gid

    async def _hardlink_import_enabled(self) -> bool:
        response = await self.client.get(
            f"{self.radarr_url}/api/v3/config/mediamanagement",
            headers=self.radarr_headers,
        )
        response.raise_for_status()
        payload = response.json()
        return (
            isinstance(payload, dict)
            and payload.get("copyUsingHardlinks") is True
            and payload.get("importExtraFiles") is not True
        )

    async def _copy_fallback_fits(self, selected: list[Path]) -> bool:
        if self.capacity_provider is None:
            raise ValueError("fresh capacity evidence is required for import")
        evidence = await self.capacity_provider()
        pending = self.permits.pending_bytes(evidence)
        copy_bytes = sum(path.stat().st_size for path in selected)
        return copy_bytes <= max(0, evidence.free_bytes - pending)

    def _can_hardlink_video(self, video: Path) -> bool:
        if self.import_uid is None or self.import_gid is None:
            return False
        return probe_hardlink_as(
            video, self.media_root, uid=self.import_uid, gid=self.import_gid
        )

    def _import_matches_source(self, video: Path, permit: Permit, *, copy_allowed: bool) -> bool:
        sources = [
            self._local_path(f"/data/torrents/{relative}")
            for relative in permit.selected_files
            if PurePosixPath(relative).suffix.lower() in _VIDEO
        ]
        if len(sources) != 1:
            raise ValidationError("permit does not identify one video file")
        source_stat = sources[0].stat()
        video_stat = video.stat()
        return (
            source_stat.st_size == video_stat.st_size
            and (
                (source_stat.st_dev == video_stat.st_dev
                 and source_stat.st_ino == video_stat.st_ino)
                or (copy_allowed and _same_content(sources[0], video))
            )
        )

    def _local_path(self, raw: str) -> Path:
        prefix = "/data/torrents"
        if raw != prefix and not raw.startswith(prefix + "/"):
            raise ValidationError("torrent path is outside admitted root")
        relative = raw.removeprefix(prefix).lstrip("/")
        parts = PurePosixPath(relative).parts
        if any(part in {"..", ".", ""} for part in parts):
            raise ValidationError("invalid torrent path")
        candidate = self.torrent_root.joinpath(*parts)
        root = self.torrent_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ValidationError("torrent path escapes admitted root")
        return resolved

    async def _radarr_movie(self, media_key: str) -> dict[str, object] | None:
        tmdb_id = media_key.rsplit(":", 1)[-1]
        response = await self.client.get(
            f"{self.radarr_url}/api/v3/movie", params={"tmdbId": tmdb_id},
            headers=self.radarr_headers,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValidationError("Radarr movie lookup is invalid")
        return next(
            (item for item in payload if isinstance(item, dict)
             and item.get("tmdbId") == int(tmdb_id)),
            None,
        )

    def _movie_file(self, movie: dict[str, object]) -> Path:
        movie_path = movie.get("path")
        movie_file = movie.get("movieFile")
        relative = movie_file.get("relativePath") if isinstance(movie_file, dict) else None
        prefix = "/data/media/movies"
        if (
            not isinstance(movie_path, str)
            or not movie_path.startswith(prefix + "/")
            or not isinstance(relative, str)
            or not relative
            or PurePosixPath(relative).is_absolute()
            or any(part in {"..", "."} for part in PurePosixPath(relative).parts)
        ):
            raise ValidationError("Radarr imported movie path is invalid")
        folder = self.media_root.joinpath(*PurePosixPath(movie_path[len(prefix) + 1:]).parts)
        movie_file_path = folder.joinpath(*PurePosixPath(relative).parts)
        root = self.media_root.resolve(strict=True)
        resolved = movie_file_path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValidationError("Radarr movie file is outside the library")
        return resolved

    def _ensure_subtitle_for_video(
        self, video: Path, permit: Permit, scope_key: str | None
    ) -> None:
        for language, label, matches in (
            ("BR_PT", "pt-BR", is_brazilian_portuguese_subtitle),
            ("EN", "en", is_english_subtitle),
        ):
            subtitles = [
                self._local_path(f"/data/torrents/{name}")
                for name in permit.selected_files
                if PurePosixPath(name).suffix.lower() in _SUBTITLE and matches(name)
            ]
            source = next((path for path in subtitles if _subtitle_has_content(path)), None)
            if subtitles and source is None:
                raise ValidationError("selected subtitle vanished after import")
            if source is not None:
                self._install_subtitle_file(video, source, label)
                return
            content = self.subtitle_store.get(
                permit.reservation_id, scope_key, permit.infohash, language=language,
                source=MOVIE_FINALIZER_SOURCE if scope_key is None else "subdl",
            )
            if content is not None:
                _write_external_subtitle(
                    video, content, self.media_root, language=language
                )
                return
        validated = validate_media(video, maximum_bytes=permit.budget_bytes)
        if not (
            audio_is_brazilian_portuguese(validated.probe)
            or has_embedded_english_subtitle(validated.probe)
        ):
            raise ValidationError("subtitle vanished after import")

    def _install_subtitle_file(self, video: Path, source: Path, label: str) -> None:
        target = video.with_name(f"{video.stem}.{label}{source.suffix.lower()}")
        if target.exists():
            if (target.is_symlink() or not target.is_file()
                    or target.read_bytes() != source.read_bytes()):
                raise ValidationError("existing library subtitle differs from verified source")
            return
        root = self.media_root.resolve(strict=True)
        if not target.parent.resolve(strict=True).is_relative_to(root):
            raise ValidationError("subtitle target escapes library")
        import_hardlink(source, target)

    def _ensure_subtitle(self, movie: dict[str, object], permit: Permit) -> None:
        self._ensure_subtitle_for_video(self._movie_file(movie), permit, None)

    async def finalize(self, media_key: str, reservation_id: str) -> str:
        reservation = self.repository.active_reservation(reservation_id)
        if reservation is None or reservation["media_key"] != media_key:
            return "reservation_inactive"
        permit = self.permits.get_for_reservation(reservation_id)
        if permit is None or permit.state != "confirmed":
            return "not_dispatched"
        import_state = self.repository.import_state(reservation_id)
        if import_state == "complete":
            return "complete"
        if import_state is not None:
            if import_state not in {"accepted", "accepted_copy"}:
                return "import_uncertain"
            movie = await self._radarr_movie(media_key)
            if movie is None or not movie.get("hasFile"):
                return "import_pending"
            if not self._import_matches_source(
                self._movie_file(movie), permit,
                copy_allowed=import_state == "accepted_copy",
            ):
                return "import_uncertain"
            self._ensure_subtitle(movie, permit)
            self.repository.complete_movie_import(reservation_id)
            return "complete"

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
            return "torrent_not_visible"
        if torrent.get("progress") != 1 or torrent.get("amount_left") != 0:
            return "downloading"
        content_path = torrent.get("content_path")
        if not isinstance(content_path, str):
            raise ValidationError("torrent content path is missing")
        content = self._local_path(content_path)
        if not permit.selected_files:
            raise ValidationError("permit has no selected files")
        files_response = await self.client.get(
            f"{self.gateway_url}/api/v2/torrents/files",
            params={"hash": permit.infohash}, headers=self.gateway_headers,
        )
        files_response.raise_for_status()
        files = files_response.json()
        if not isinstance(files, list):
            raise ValidationError("gateway file response is invalid")
        sizes = {
            item["name"]: item["size"] for item in files
            if isinstance(item, dict) and isinstance(item.get("name"), str)
            and isinstance(item.get("size"), int)
        }
        selected: list[Path] = []
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
        if len(videos) != 1:
            raise ValidationError("movie video or Brazilian Portuguese subtitle is missing")
        validated = validate_media(videos[0], maximum_bytes=permit.budget_bytes)
        if not validated.probe.audio_languages:
            raise ValidationError("movie has no audio stream")
        if subtitle_files and any(not _subtitle_has_content(path) for path in subtitle_files):
            raise ValidationError("selected subtitle content is not valid")
        release_titles = list(dict.fromkeys(
            title for title in (
                videos[0].stem,
                content.name if content.is_dir() else None,
                torrent.get("name"),
            ) if isinstance(title, str) and title
        ))

        async def fetch_subtitle(language: str, match_mode: str) -> bytes | None:
            if self.subtitle_source is None:
                return None
            return await self.subtitle_source.fetch_movie(
                tmdb_id=int(media_key.rsplit(":", 1)[-1]),
                release_titles=release_titles,
                language=language,
                match_mode=match_mode,
                movie_duration_seconds=validated.probe.duration_seconds,
            )

        original_ptbr = audio_is_brazilian_portuguese(validated.probe)
        has_brazilian = bool(brazilian_subtitles) or self.subtitle_store.get(
            reservation_id, None, permit.infohash, language="BR_PT",
            source=MOVIE_FINALIZER_SOURCE,
        ) is not None
        if not original_ptbr and not has_brazilian:
            found = await fetch_subtitle("BR_PT", "exact")
            if found is None:
                found = await fetch_subtitle("BR_PT", "same_duration")
            if found is not None:
                self.subtitle_store.put(
                    reservation_id, None, permit.infohash, found,
                    source=MOVIE_FINALIZER_SOURCE,
                )
            has_brazilian = self.subtitle_store.get(
                reservation_id, None, permit.infohash, language="BR_PT",
                source=MOVIE_FINALIZER_SOURCE,
            ) is not None
        if (
            not original_ptbr and not has_brazilian and not english_subtitles
            and not has_embedded_english_subtitle(validated.probe)
        ):
            has_english = self.subtitle_store.get(
                reservation_id, None, permit.infohash, language="EN",
                source=MOVIE_FINALIZER_SOURCE,
            ) is not None
            if not has_english:
                found = await fetch_subtitle("EN", "exact")
                if found is None:
                    found = await fetch_subtitle("EN", "same_duration")
                if found is not None:
                    self.subtitle_store.put(
                        reservation_id, None, permit.infohash, found, language="EN",
                        source=MOVIE_FINALIZER_SOURCE,
                    )
                    has_english = True
            if not has_english:
                return "waiting_subtitles"
        if not await self._hardlink_import_enabled():
            return "import_guard"
        hardlink_ready = self._can_hardlink_video(videos[0])
        if not hardlink_ready and not await self._copy_fallback_fits(selected):
            return "waiting_space"
        if not self.repository.claim_movie_import(
            reservation_id, copy_allowed=not hardlink_ready
        ):
            return "import_pending"
        response = await self.client.post(
            f"{self.radarr_url}/api/v3/command",
            headers=self.radarr_headers,
            json={
                "name": "DownloadedMoviesScan", "path": content_path,
                "downloadClientId": permit.infohash.upper(), "importMode": "Copy",
            },
        )
        response.raise_for_status()
        command = response.json()
        if not isinstance(command, dict) or not isinstance(command.get("id"), int):
            raise ValidationError("Radarr import response is invalid")
        self.repository.record_movie_import(reservation_id, str(command["id"]))
        return "import_requested"
