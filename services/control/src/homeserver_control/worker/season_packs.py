"""Inspect a complete season once and bind its individual episode files."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from homeserver_control.domain.episode_identity import resolve_episode_identity
from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent

from .acquisition import _SUBTITLE_SUFFIXES, _VIDEO_SUFFIXES, _is_sample_video
from .release_quality import ReleasePolicy
from .subtitle_language import SubtitlePolicy

_SEASON = re.compile(r"(?<![a-z0-9])(?:se?|season|temporada)[ ._-]*(\d{1,4})(?!\d)", re.I)
_RESOLUTION = re.compile(r"(?<![a-z0-9])(?:480p|576p|720p|1080p|2160p|4k)(?![a-z0-9])", re.I)
_REMUX = re.compile(r"(?<![a-z0-9])remux(?![a-z0-9])", re.I)
_BONUS_DIRECTORIES = {"extras", "featurettes"}


def _is_bonus_path(path: str) -> bool:
    # The torrent root is the release name, not an explicit bonus directory.
    return any(
        part.casefold() in _BONUS_DIRECTORIES for part in PurePosixPath(path).parts[1:-1]
    )


@dataclass(frozen=True)
class SeasonPackManifest:
    infohash: str
    metadata_sha256: str
    selected_files: tuple[str, ...]
    budget_bytes: int
    episode_files: dict[str, tuple[str, ...]]


def inspect_season_pack(
    torrent: bytes,
    *,
    season: int,
    episode_runtimes: Mapping[int, float | None],
    release: dict[str, object],
    release_policy: ReleasePolicy | None = None,
    allow_external_subtitle: bool = False,
    subtitle_policy: SubtitlePolicy | None = None,
    original_language: object = None,
    animation: bool = False,
    episode_catalog: Sequence[Mapping[str, object]] | None = None,
) -> SeasonPackManifest | None:
    """Require one profile-compatible video for every known episode of this season.

    All torrent bytes are admitted: bindings select what may be imported, not
    qBittorrent file priorities. Extras and already imported episodes still use
    disk space when the complete pack downloads.
    """
    if (
        isinstance(season, bool)
        or not isinstance(season, int)
        or season <= 0
        or not episode_runtimes
        or any(isinstance(number, bool) or not isinstance(number, int) or number <= 0
               for number in episode_runtimes)
    ):
        return None
    detail = release.get("quality")
    detail = detail.get("quality") if isinstance(detail, dict) else None
    policy = release_policy or ReleasePolicy(resolutions=(1080,))
    allowed_resolutions = policy.resolutions if policy.profile_name else (1080,)
    if not isinstance(detail, dict) or detail.get("resolution") not in allowed_resolutions:
        return None
    quality = dict(detail)
    if quality.get("source") == "web":
        quality.update(source="webdl", modifier="none")
    elif quality.get("source") in ("bluray", "blurayRaw"):
        quality.update(
            source="bluray",
            modifier="remux" if quality.get("source") == "blurayRaw"
            or quality.get("modifier") == "remux"
            or _REMUX.search(str(release.get("title", ""))) else "none",
        )
    normalized_release = release | {"quality": {"quality": quality}}
    if policy.rank(
        normalized_release, original_language=original_language,
        animation=animation if policy.profile_name else False,
    ) is None:
        return None
    try:
        inspected = inspect_torrent(torrent)
    except TorrentBytesError:
        return None
    catalog = episode_catalog if episode_catalog is not None else [
        {"seasonNumber": season, "episodeNumber": number} for number in episode_runtimes
    ]
    title_seasons = {int(match[1]) for match in _SEASON.finditer(str(release.get("title", "")))}
    context_season = season if (
        type(release.get("seasonNumber")) is int and release["seasonNumber"] == season
        or title_seasons == {season}
    ) else None

    def episode_number(path):
        identity = resolve_episode_identity(
            path, catalog, season=context_season,
            allow_absolute=animation and episode_catalog is not None,
        )
        return identity[1] if identity is not None and identity[0] == season else None

    videos = {}
    for entry in inspected.files:
        if PurePosixPath(entry.path).suffix.lower() not in _VIDEO_SUFFIXES:
            continue
        if _is_sample_video(entry.path) or _is_bonus_path(entry.path):
            continue
        number = episode_number(entry.path)
        if number not in episode_runtimes or number in videos:
            return None
        tokens = {f"{detail['resolution']}p"}
        if detail["resolution"] == 2160:
            tokens.add("4k")
        if any(match[0].lower() not in tokens for match in _RESOLUTION.finditer(entry.path)):
            return None
        if not policy.accepts_size(
            normalized_release, video_bytes=entry.length,
            runtime_minutes=episode_runtimes[number],
        ):
            return None
        videos[number] = entry
    if videos.keys() != episode_runtimes.keys():
        return None
    subtitles = subtitle_policy or SubtitlePolicy()
    bindings = {}
    for number, video in sorted(videos.items()):
        matching = [
            entry for entry in inspected.files
            if PurePosixPath(entry.path).suffix.lower() in _SUBTITLE_SUFFIXES
            and not _is_bonus_path(entry.path)
            and episode_number(entry.path) == number
            and subtitles.matches(entry.path)
        ]
        if not matching and not allow_external_subtitle:
            return None
        bindings[f"S{season:02d}E{number:02d}"] = (
            video.path, *(entry.path for entry in matching),
        )
    return SeasonPackManifest(
        inspected.infohash, inspected.metadata_sha256,
        tuple(entry.path for entry in inspected.files), inspected.total_bytes, bindings,
    )
