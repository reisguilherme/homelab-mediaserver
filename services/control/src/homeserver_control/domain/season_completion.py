"""Classify one full canonical season without confusing catalog presence with closure."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class SeasonCompletion:
    state: str
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]
    episode_numbers: tuple[int, ...]
    aired_episode_numbers: tuple[int, ...]


def episode_aired(episode: Mapping[str, object], *, now: datetime | None = None) -> bool | None:
    """Accept only timezone-aware broadcast dates compared in UTC."""
    current = now or datetime.now(UTC)
    value = episode.get("airDateUtc")
    if current.tzinfo is None:
        return None
    if isinstance(value, str):
        try:
            aired = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if aired.tzinfo is not None:
                return aired.astimezone(UTC) <= current.astimezone(UTC)
        except ValueError:
            pass
    return None


def season_completion(
    episodes: Sequence[Mapping[str, object]],
    *,
    season: int,
    series_status: object = None,
    now: datetime | None = None,
    expected_episode_count: int | None = None,
    metadata_complete: bool = True,
    verified_released_episode_ids: frozenset[int] = frozenset(),
) -> SeasonCompletion:
    """Require full catalog, aired episodes and an independent closing signal.

    Pass every canonical episode, including unmonitored and later seasons. A
    successful full Sonarr episode response is a complete catalog contract;
    when its same-order season count is available it must agree. Date-only and
    inferred broadcast dates must be adapted/audited before this boundary.
    The optional native-ID allowlist is an explicit caller-owned release receipt;
    production leaves it empty. Row flags cannot enable this exception.
    """
    reasons, evidence = [], []
    if type(season) is not int or season <= 0:
        return SeasonCompletion("unknown", ("invalid_season",), (), (), ())
    rows = [row for row in episodes if row.get("seasonNumber") == season]
    numbers = [row.get("episodeNumber") for row in rows]
    valid = all(type(number) is int and number > 0 for number in numbers)
    if not metadata_complete:
        reasons.append("canonical_catalog_incomplete")
    if not rows or not valid:
        reasons.append("canonical_season_missing_or_invalid")
    if valid and len(numbers) != len(set(numbers)):
        reasons.append("duplicate_canonical_episode")
    if valid and numbers and sorted(set(numbers)) != list(range(1, max(numbers) + 1)):
        reasons.append("canonical_episode_number_gap")
    if expected_episode_count is not None and (
        type(expected_episode_count) is not int
        or expected_episode_count <= 0
        or len(rows) != expected_episode_count
    ):
        reasons.append("canonical_episode_count_mismatch")
    known_numbers = tuple(sorted(set(numbers))) if valid else ()

    def released(row):
        value = episode_aired(row, now=now)
        if (
            value is None
            and type(row.get("id")) is int
            and row["id"] in verified_released_episode_ids
        ):
            return True
        return value

    audited = any(episode_aired(row, now=now) is None and released(row) is True for row in rows)
    aired = tuple(sorted(row["episodeNumber"] for row in rows if valid and released(row) is True))
    dates = [released(row) for row in rows]
    if any(value is None for value in dates):
        reasons.append("episode_air_date_unknown")
    if reasons:
        return SeasonCompletion("unknown", tuple(reasons), (), known_numbers, aired)
    if any(value is False for value in dates):
        return SeasonCompletion("ongoing", (), ("known_future_episode",), known_numbers, aired)
    if audited:
        evidence.append("audited_release_before_cutoff")
    status = str(series_status or "").casefold()
    if status in {"ended", "canceled", "cancelled"}:
        evidence.append("series_ended")
    # A mid-season finale is not a completed season.
    if any(str(row.get("finaleType") or "").casefold() in {"season", "series"} for row in rows):
        evidence.append("season_or_series_finale_aired")
    if any(
        type(row.get("seasonNumber")) is int
        and row["seasonNumber"] > season
        and type(row.get("episodeNumber")) is int
        and row["episodeNumber"] > 0
        and episode_aired(row, now=now) is True
        for row in episodes
    ):
        evidence.append("later_season_aired")
    return SeasonCompletion(
        "complete"
        if any(item != "audited_release_before_cutoff" for item in evidence)
        else "unknown",
        ()
        if any(item != "audited_release_before_cutoff" for item in evidence)
        else ("season_closure_unconfirmed",),
        tuple(evidence),
        known_numbers,
        aired,
    )
