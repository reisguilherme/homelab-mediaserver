"""Rank inspectable movie releases using Radarr's parsed source metadata."""

from __future__ import annotations

import re

_DOLBY_VISION = re.compile(r"(?<![a-z0-9])(?:dv|dovi|dolby[ ._-]*vision)(?![a-z0-9])", re.I)
_ATMOS = re.compile(r"(?<![a-z0-9])atmos(?![a-z0-9])", re.I)


def release_seeders(release: dict[str, object]) -> int | None:
    value = release.get("seeders")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def release_rank(release: dict[str, object]) -> tuple[int, int, int, int, int, int] | None:
    """Return a descending preference key, or None for a forbidden source."""
    quality = release.get("quality")
    detail = quality.get("quality") if isinstance(quality, dict) else None
    if not isinstance(detail, dict):
        return None
    resolution = detail.get("resolution")
    if not isinstance(resolution, int) or resolution not in {1080, 2160}:
        return None
    source = detail.get("source")
    modifier = detail.get("modifier", "none")
    if source == "bluray" and modifier == "remux":
        tier = 3
    elif source == "bluray" and modifier == "none":
        tier = 2
    elif source == "webdl" and modifier == "none":
        tier = 1
    else:
        return None
    title = release.get("title")
    title = title if isinstance(title, str) else ""
    size = release.get("size")
    seeders = release_seeders(release)
    return (
        tier,
        resolution,
        int(bool(_DOLBY_VISION.search(title))),
        int(bool(_ATMOS.search(title))),
        seeders if seeders is not None else -1,
        size if isinstance(size, int) and size > 0 else 0,
    )
