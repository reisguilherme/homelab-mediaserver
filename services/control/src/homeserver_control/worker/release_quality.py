"""Rank inspectable movie releases using Radarr's parsed source metadata."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

_DOLBY_VISION = re.compile(r"(?<![a-z0-9])(?:dv|dovi|dolby[ ._-]*vision)(?![a-z0-9])", re.I)
_ATMOS = re.compile(r"(?<![a-z0-9])atmos(?![a-z0-9])", re.I)
_LANGUAGE_ALIASES = {
    "english": "en", "eng": "en",
    "portuguese": "pt", "por": "pt",
    "brazilian portuguese": "pt-br", "portuguese (brazil)": "pt-br", "pob": "pt-br",
    "french": "fr", "fra": "fr", "fre": "fr",
    "spanish": "es", "spa": "es",
    "german": "de", "deu": "de", "ger": "de",
    "italian": "it", "ita": "it",
    "japanese": "ja", "jpn": "ja",
    "chinese": "zh", "zho": "zh", "chi": "zh",
    "korean": "ko", "kor": "ko",
    "russian": "ru", "rus": "ru",
}


def _declared_language(value: object) -> str | None:
    if isinstance(value, Mapping):
        # Native numeric IDs alone do not establish a language or region.
        for key in ("code", "isoCode", "iso6391", "iso6392", "languageCode", "name"):
            if language := _declared_language(value.get(key)):
                return language
        return None
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("_", "-")
    normalized = _LANGUAGE_ALIASES.get(normalized, normalized)
    if normalized == "und" or not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", normalized):
        return None
    return normalized


def _audio_preference(release, preferences, original_language) -> int:
    declared = release.get("languages")
    if not isinstance(declared, (list, tuple)):
        return 0
    languages = {_declared_language(value) for value in declared} - {None}
    for index, preference in enumerate(preferences):
        desired = _declared_language(
            original_language if preference.strip().lower() == "original" else preference
        )
        if desired is not None and any(
            language == desired or "-" not in desired and language.startswith(desired + "-")
            for language in languages
        ):
            return len(preferences) - index
    return 0


def release_seeders(release: dict[str, object]) -> int | None:
    value = release.get("seeders")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


@dataclass(frozen=True)
class ReleasePolicy:
    resolutions: tuple[int, ...] = (2160, 1080)
    sources: tuple[str, ...] = ("remux", "bluray", "webdl")
    prefer_dolby_vision: bool = True
    prefer_atmos: bool = True
    automatic_upgrades: bool = False
    audio_languages: tuple[str, ...] = ("original",)
    minimum_mib_per_min: tuple[tuple[int, float], ...] = ((720, 10), (1080, 20), (2160, 50))

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]):
        resolutions = tuple(
            int(value)
            for value in environment.get("HOMESERVER_MEDIA_RESOLUTIONS", "2160,1080").split(",")
        )
        sources = tuple(
            environment.get("HOMESERVER_MEDIA_SOURCES", "remux,bluray,webdl").split(",")
        )
        return cls(
            resolutions,
            sources,
            environment.get("HOMESERVER_PREFER_DOLBY_VISION", "true") == "true",
            environment.get("HOMESERVER_PREFER_ATMOS", "true") == "true",
            environment.get("HOMESERVER_AUTOMATIC_UPGRADES", "false") == "true",
            tuple(environment.get("HOMESERVER_AUDIO_LANGUAGES", "original").split(",")),
            tuple(
                (
                    resolution,
                    float(
                        environment.get(
                            f"HOMESERVER_QUALITY_MIN_MIB_PER_MIN_{resolution}", str(minimum)
                        )
                    ),
                )
                for resolution, minimum in ((720, 10), (1080, 20), (2160, 50))
            ),
        )

    def rank(self, release: dict[str, object], *, original_language: object = None):
        return release_rank(release, self, original_language=original_language)

    def accepts_size(self, release, *, video_bytes, runtime_minutes) -> bool:
        """Reject undersized encodes using the main video, never torrent padding."""
        if (
            self.rank(release) is None
            or isinstance(video_bytes, bool)
            or not isinstance(video_bytes, int)
            or video_bytes <= 0
        ):
            return False
        resolution = release["quality"]["quality"]["resolution"]
        floor = dict(self.minimum_mib_per_min).get(resolution)
        if floor is None or not math.isfinite(floor) or floor < 0:
            return False
        if floor == 0:
            return True
        runtime = media_runtime_minutes(runtime_minutes)
        return runtime is not None and video_bytes >= floor * runtime * 1024**2


def media_runtime_minutes(*values) -> float | None:
    for value in values:
        if (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value > 0
        ):
            return float(value)
    return None


def release_rank(
    release: dict[str, object],
    policy: ReleasePolicy | None = None,
    *,
    original_language: object = None,
) -> tuple[int, int, int, int, int, int, int] | None:
    """Return a descending preference key, or None for a forbidden source."""
    policy = policy or ReleasePolicy()
    quality = release.get("quality")
    detail = quality.get("quality") if isinstance(quality, dict) else None
    if not isinstance(detail, dict):
        return None
    resolution = detail.get("resolution")
    if not isinstance(resolution, int) or resolution not in policy.resolutions:
        return None
    source = detail.get("source")
    modifier = detail.get("modifier", "none")
    if source == "bluray" and modifier == "remux":
        family = "remux"
    elif source == "bluray" and modifier == "none":
        family = "bluray"
    elif source == "webdl" and modifier == "none":
        family = "webdl"
    else:
        return None
    if family not in policy.sources:
        return None
    tier = len(policy.sources) - policy.sources.index(family)
    title = release.get("title")
    title = title if isinstance(title, str) else ""
    size = release.get("size")
    seeders = release_seeders(release)
    return (
        tier,
        len(policy.resolutions) - policy.resolutions.index(resolution),
        int(policy.prefer_dolby_vision and bool(_DOLBY_VISION.search(title))),
        int(policy.prefer_atmos and bool(_ATMOS.search(title))),
        _audio_preference(release, policy.audio_languages, original_language),
        seeders if seeders is not None else -1,
        size if isinstance(size, int) and size > 0 else 0,
    )
