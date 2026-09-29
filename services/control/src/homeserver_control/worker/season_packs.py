"""Inspect a complete season once and bind its individual episode files."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent

from .acquisition import _SUBTITLE_SUFFIXES, _VIDEO_SUFFIXES, _is_sample_video
from .release_quality import ReleasePolicy
from .subtitle_language import SubtitlePolicy

_EPISODE = re.compile(
    r"(?<![a-z0-9])s([0-9]{1,3})e([0-9]{1,3})(?![0-9]|e[0-9]|[ ._-]*[-e][0-9])",
    re.I,
)
_RESOLUTION = re.compile(r"(?<![a-z0-9])(?:480p|576p|720p|1080p|2160p|4k)(?![a-z0-9])", re.I)
_REMUX = re.compile(r"(?<![a-z0-9])remux(?![a-z0-9])", re.I)


@dataclass(frozen=True)
class SeasonPackManifest:
    infohash: str
    metadata_sha256: str
    selected_files: tuple[str, ...]
    budget_bytes: int
    episode_files: dict[str, tuple[str, ...]]


def _episode_number(path: str, season: int) -> int | None:
    matches = list(_EPISODE.finditer(PurePosixPath(path).name))
    if len(matches) != 1 or int(matches[0][1]) != season or int(matches[0][2]) <= 0:
        return None
    return int(matches[0][2])


def inspect_season_pack(
    torrent: bytes,
    *,
    season: int,
    episode_runtimes: Mapping[int, float | None],
    release: dict[str, object],
    release_policy: ReleasePolicy | None = None,
    allow_external_subtitle: bool = False,
    subtitle_policy: SubtitlePolicy | None = None,
) -> SeasonPackManifest | None:
    """Require one good 1080p video for every known episode of this season.

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
    if not isinstance(detail, dict) or detail.get("resolution") != 1080:
        return None
    quality = dict(detail)
    if quality.get("source") == "web":
        quality.update(source="webdl", modifier="none")
    elif quality.get("source") in ("bluray", "blurayRaw"):
        quality.update(
            source="bluray",
            modifier="remux" if quality.get("source") == "blurayRaw"
            or _REMUX.search(str(release.get("title", ""))) else "none",
        )
    normalized_release = release | {"quality": {"quality": quality}}
    policy = release_policy or ReleasePolicy(resolutions=(1080,))
    if policy.rank(normalized_release) is None:
        return None
    try:
        inspected = inspect_torrent(torrent)
    except TorrentBytesError:
        return None
    videos = {}
    for entry in inspected.files:
        if PurePosixPath(entry.path).suffix.lower() not in _VIDEO_SUFFIXES:
            continue
        if _is_sample_video(entry.path):
            continue
        number = _episode_number(entry.path, season)
        if number not in episode_runtimes or number in videos:
            return None
        if any(match[0].lower() != "1080p" for match in _RESOLUTION.finditer(entry.path)):
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
            and _episode_number(entry.path, season) == number
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
