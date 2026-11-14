"""Resolve a single video identity only against an unambiguous canonical catalog."""

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

_EXPLICIT = re.compile(
    r"(?<![a-z0-9])(?:s(?:e)?(?P<s>\d{1,4})[ ._-]*(?:ep?|episode)[ ._-]*"
    r"(?P<e>\d{1,4})|(?P<xs>\d{1,4})x(?P<xe>\d{1,4}))(?![a-z0-9])",
    re.I,
)
_EP = re.compile(r"(?<![a-z0-9])(?:ep|episode)[ ._-]*(\d{1,4})(?![a-z0-9])", re.I)
_RANGE = re.compile(
    r"[ ._]*(?:(?:[-\u2013+&]|to|through)[ ._-]*(?:(?:episode|ep?)[ ._-]*)?"
    r"|e[ ._-]*)\d+",
    re.I,
)
_ABSOLUTE = re.compile(r"\s-\s(\d{1,4})(?=\s|[\[.(]|$)")


def resolve_episode_identity(
    path: str,
    episodes: Sequence[Mapping[str, object]],
    *,
    season: int | None = None,
    allow_absolute: bool = False,
) -> tuple[int, int] | None:
    """EP-only needs a trusted season; anime numbers need unique absolute metadata.

    Never guess from a bare number, resolution/year, title text, or a multi-
    episode range. Explicit conflicting markers cannot be overridden by context.
    """
    name = PurePosixPath(str(path).replace("\\", "/")).name
    catalog = [
        row
        for row in episodes
        if type(row.get("seasonNumber")) is int
        and row["seasonNumber"] > 0
        and type(row.get("episodeNumber")) is int
        and row["episodeNumber"] > 0
    ]
    matches = list(_EXPLICIT.finditer(name))
    # EP/Episode markers outside explicit SxxEyy tokens must also agree. Mask
    # consumed tokens so SE02.EP01 is not counted twice as an independent claim.
    remaining = _EXPLICIT.sub(lambda match: " " * len(match[0]), name)
    episode_markers = list(_EP.finditer(remaining))
    if any(_RANGE.match(name[match.end() :]) for match in [*matches, *episode_markers]):
        return None
    if matches:
        identities = {
            (int(match["s"] or match["xs"]), int(match["e"] or match["xe"])) for match in matches
        }
        if len(identities) != 1:
            return None
        result = identities.pop()
        if any(int(match[1]) != result[1] for match in episode_markers):
            return None
    else:
        matches = episode_markers
        if matches:
            if type(season) is not int or len({int(match[1]) for match in matches}) != 1:
                return None
            result = (season, int(matches[0][1]))
        else:
            if not allow_absolute:
                return None
            absolute = list(_ABSOLUTE.finditer(name))
            if len(absolute) != 1 or _RANGE.match(name[absolute[0].end() :]):
                return None
            number = int(absolute[0][1])
            mapped = [
                row
                for row in catalog
                if type(row.get("absoluteEpisodeNumber")) is int
                and row["absoluteEpisodeNumber"] == number
            ]
            if len(mapped) != 1:
                return None
            result = (mapped[0]["seasonNumber"], mapped[0]["episodeNumber"])
    if season is not None and (type(season) is not int or result[0] != season):
        return None
    return (
        result
        if sum((row["seasonNumber"], row["episodeNumber"]) == result for row in catalog) == 1
        else None
    )
