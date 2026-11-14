"""Rank inspectable movie releases using Radarr's parsed source metadata."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace

from homeserver_common.download_profiles import (
    DEFAULT_PROFILE_NAME,
    PROFILE_AUDIO_PREFERENCES,
    PROFILE_QUALITIES,
    PROFILE_QUALITY_TIERS,
)

from .audio_evidence import declared_language as _declared_language
from .audio_evidence import preferred_audio_tier, release_audio_languages
from .indexers import release_indexer as release_indexer

_DOLBY_VISION = re.compile(r"(?<![a-z0-9])(?:dv|dovi|dolby[ ._-]*vision)(?![a-z0-9])", re.I)
_ATMOS = re.compile(r"(?<![a-z0-9])atmos(?![a-z0-9])", re.I)
_BDRIP = re.compile(r"(?<![a-z0-9])bd[ ._-]*rip(?![a-z0-9])", re.I)
_BRRIP = re.compile(r"(?<![a-z0-9])br[ ._-]*rip(?![a-z0-9])", re.I)


def profile_audio_tier(release, *, original_language=None):
    """Separate original and Brazilian dual claims, without inventing tracks."""
    languages = release_audio_languages(release, original_language=original_language)
    if preferred_audio_tier(languages, original_language, ("dual",)):
        return "dual"
    original = _declared_language(original_language)
    original_present = preferred_audio_tier(languages, original_language, ("original",))
    if original_present and (
        "pt-br" not in languages or original in {"pt", "pt-br"}
    ):
        return "original"
    return None

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


def _animation_audio_preference(release, preferences, original_language) -> int:
    languages = release_audio_languages(release, original_language=original_language)
    tier = preferred_audio_tier(languages, original_language, preferences)
    return len(preferences) - preferences.index(tier) if tier is not None else 0


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
    minimum_mib_per_min: tuple[tuple[int, float], ...] = ((720, 0), (1080, 0), (2160, 0))
    indexer_priority: tuple[str, ...] = ("uindex", "1337x")
    indexer_fallback_min_seeders: int = 20
    animation_audio_preferences: tuple[str, ...] = ("dual", "pt-BR", "original")
    profile_name: str | None = None
    minimum_seeders: int = 5
    dubbed_audio_preferences: tuple[str, ...] = ("dual", "original")
    fallback_indexers: tuple[str, ...] = ()

    def __post_init__(self):
        if (
            len(set(self.fallback_indexers)) != len(self.fallback_indexers)
            or any(name not in {"uindex", "1337x", "amigosshare"}
                   or name not in self.indexer_priority for name in self.fallback_indexers)
            or self.fallback_indexers
            and not set(self.indexer_priority) - set(self.fallback_indexers)
        ):
            raise ValueError(
                "HOMESERVER_RELEASE_FALLBACK_INDEXERS: expected enabled fallback subset"
            )
        for key, preferences in (
            ("ANIMATION_AUDIO_PREFERENCES", self.animation_audio_preferences),
            ("DUBBED_AUDIO_PREFERENCES", self.dubbed_audio_preferences),
        ):
            if (
                not preferences or len(set(preferences)) != len(preferences)
                or any(value not in {"dual", "pt-BR", "original"} for value in preferences)
            ):
                raise ValueError(f"HOMESERVER_{key}: unsupported preference")
        if type(self.minimum_seeders) is not int or self.minimum_seeders < 1:
            raise ValueError("HOMESERVER_RELEASE_MIN_SEEDERS: expected positive integer")
        if self.profile_name is not None:
            if self.profile_name not in PROFILE_QUALITIES:
                raise ValueError("HOMESERVER_DOWNLOAD_DEFAULT_PROFILE: unknown profile")
            qualities = PROFILE_QUALITIES[self.profile_name]
            object.__setattr__(
                self, "resolutions", tuple(dict.fromkeys(row[0] for row in qualities))
            )
            object.__setattr__(self, "sources", tuple(dict.fromkeys(row[1] for row in qualities)))

    @property
    def dubbed(self) -> bool:
        return self.profile_name == "Dublado"

    @property
    def audio_preferences(self) -> tuple[str, ...]:
        return (
            PROFILE_AUDIO_PREFERENCES[self.profile_name]
            if self.profile_name else self.audio_languages
        )

    @property
    def maximum_audio_rank(self) -> int:
        # Keep Dublado's dual=3/original=1 comparable with persisted source ranks.
        return 3 if self.dubbed else len(self.audio_preferences)

    @property
    def preferred_indexers(self) -> tuple[str, ...]:
        """Opted-in ASC is preferred only while selecting the Dublado profile."""
        if self.dubbed and "amigosshare" in self.indexer_priority:
            return (
                "amigosshare", *(name for name in self.indexer_priority if name != "amigosshare"),
            )
        return self.indexer_priority

    def for_profile(self, name: str) -> ReleasePolicy:
        """Select one title's profile without changing other titles' policies."""
        if name not in PROFILE_QUALITIES:
            raise ValueError("unknown download profile")
        return replace(self, profile_name=name)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str], *, media_kind="movie"):
        if media_kind not in {"movie", "series"}:
            raise ValueError("invalid release media kind")
        return cls(
            prefer_dolby_vision=environment.get("HOMESERVER_PREFER_DOLBY_VISION", "true") == "true",
            prefer_atmos=environment.get("HOMESERVER_PREFER_ATMOS", "true") == "true",
            automatic_upgrades=environment.get("HOMESERVER_AUTOMATIC_UPGRADES", "false") == "true",
            audio_languages=tuple(environment.get(
                "HOMESERVER_AUDIO_LANGUAGES", "original"
            ).split(",")),
            minimum_mib_per_min=tuple(
                (
                    resolution,
                    float(
                        environment.get(
                            f"HOMESERVER_QUALITY_MIN_MIB_PER_MIN_{resolution}", str(minimum)
                        )
                    ),
                )
                for resolution, minimum in ((720, 0), (1080, 0), (2160, 0))
            ),
            indexer_priority=tuple(environment.get(
                "HOMESERVER_RELEASE_INDEXER_PRIORITY", "uindex,1337x"
            ).split(",")),
            fallback_indexers=tuple(value.strip() for value in environment.get(
                "HOMESERVER_RELEASE_FALLBACK_INDEXERS", ""
            ).split(",") if value.strip()),
            indexer_fallback_min_seeders=int(
                environment.get(
                    "HOMESERVER_MOVIE_INDEXER_FALLBACK_MIN_SEEDERS",
                    environment.get("HOMESERVER_INDEXER_FALLBACK_MIN_SEEDERS", "20"),
                )
                if media_kind == "movie"
                else environment.get("HOMESERVER_INDEXER_FALLBACK_MIN_SEEDERS", "5")
            ),
            animation_audio_preferences=tuple(value.strip() for value in environment.get(
                "HOMESERVER_ANIMATION_AUDIO_PREFERENCES", "dual,pt-BR,original"
            ).split(",")),
            profile_name=environment.get(
                "HOMESERVER_DOWNLOAD_DEFAULT_PROFILE", DEFAULT_PROFILE_NAME
            ),
            minimum_seeders=int(environment.get("HOMESERVER_RELEASE_MIN_SEEDERS", "5")),
            dubbed_audio_preferences=tuple(value.strip() for value in environment.get(
                "HOMESERVER_DUBBED_AUDIO_PREFERENCES", "dual,original"
            ).split(",")),
        )

    def rank(
        self, release: dict[str, object], *, original_language: object = None, animation=False,
    ):
        return release_rank(
            release, self, original_language=original_language, animation=animation,
        )

    def accepts_size(self, release, *, video_bytes, runtime_minutes) -> bool:
        """Reject undersized encodes using the main video, never torrent padding."""
        if (
            _visual_quality(release, self) is None
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


def _visual_quality(release: dict[str, object], policy: ReleasePolicy) -> tuple[int, str] | None:
    """Validate only visual eligibility; size checks have no language context."""
    quality = release.get("quality")
    detail = quality.get("quality") if isinstance(quality, dict) else None
    if not isinstance(detail, dict):
        return None
    resolution = detail.get("resolution")
    if not isinstance(resolution, int) or resolution not in policy.resolutions:
        return None
    source = detail.get("source")
    modifier = detail.get("modifier", "none")
    if source == "blurayRaw" or source == "bluray" and modifier == "remux":
        family = "remux"
    elif source == "bluray" and modifier == "none":
        family = "bluray"
        if policy.profile_name:
            title = str(release.get("title") or "")
            if _BDRIP.search(title):
                family = "bdrip"
            elif _BRRIP.search(title):
                family = "brrip"
    elif modifier == "none" and (
        source == "webdl"
        or source == "web" and str(detail.get("name", "")).lower().startswith("webdl")
    ):
        family = "webdl"
    else:
        return None
    quality = (resolution, family)
    if policy.profile_name is not None:
        if not any(resolution == res and family in sources
                   for res, sources in PROFILE_QUALITY_TIERS[policy.profile_name]):
            return None
    elif family not in policy.sources:
        return None
    return quality


def release_rank(
    release: dict[str, object],
    policy: ReleasePolicy | None = None,
    *,
    original_language: object = None,
    animation: bool = False,
) -> tuple[int, ...] | None:
    """Return a descending preference key, or None for an ineligible source."""
    policy = policy or ReleasePolicy()
    visual = _visual_quality(release, policy)
    if visual is None:
        return None
    resolution, family = visual
    title = release.get("title")
    title = title if isinstance(title, str) else ""
    size = release.get("size")
    seeders = release_seeders(release)
    extras = (
        int(policy.prefer_dolby_vision and bool(_DOLBY_VISION.search(title))),
        int(policy.prefer_atmos and bool(_ATMOS.search(title))),
        size if isinstance(size, int) and size > 0 else 0,
    )
    if policy.profile_name is not None:
        if seeders is None or seeders < policy.minimum_seeders:
            return None
        tiers = PROFILE_QUALITY_TIERS[policy.profile_name]
        tier = next(len(tiers) - index for index, (res, sources) in enumerate(tiers)
                    if visual[0] == res and visual[1] in sources)
        audio = profile_audio_tier(release, original_language=original_language)
        preference = (
            (3 if audio == "dual" else 1) if policy.dubbed and audio is not None
            else len(policy.audio_preferences) - policy.audio_preferences.index(audio)
            if audio in policy.audio_preferences else 0
        )
        languages = release_audio_languages(release, original_language=original_language)
        if preference == 0 and (policy.dubbed or (
            languages and _declared_language(original_language) is not None
        )):
            return None
        return (
            *((preference, tier) if policy.dubbed else (tier, preference)),
            seeders,
            *extras,
        )
    tier = len(policy.sources) - policy.sources.index(family)
    preference = (
        _animation_audio_preference(release, policy.animation_audio_preferences, original_language)
        if animation else _audio_preference(release, policy.audio_languages, original_language)
    )
    if animation and preference == 0:
        return None
    return (
        *((preference, resolution) if animation else (resolution, preference)),
        seeders if seeders is not None else -1,
        tier,
        *extras,
    )
