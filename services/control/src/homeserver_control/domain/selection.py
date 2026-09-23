from itertools import product

from .models import EpisodeFile, ReleaseRef, SelectionPlan


def select_season(
    releases: list[ReleaseRef],
    *,
    episode_keys: set[str],
    max_work: int | None = None,
    existing_valid: set[str] | None = None,
    existing_sizes: dict[str, int] | None = None,
) -> SelectionPlan:
    existing = existing_valid or set()
    pending_keys = sorted(episode_keys - existing)
    if not pending_keys:
        return SelectionPlan(files=(), total_bytes=0, reason="already_available")
    candidates: list[list[EpisodeFile]] = []
    for key in pending_keys:
        options = [
            file
            for release in releases
            for file in release.files
            if file.episode_key == key and file.size_bytes > 0
        ]
        if not options:
            return SelectionPlan(files=(), total_bytes=0, reason=f"waiting_source:{key}")
        candidates.append(options)

    combinations = product(*candidates)
    best: tuple[EpisodeFile, ...] | None = None
    best_score: tuple[int, tuple[int, ...]] | None = None
    for combination in combinations:
        if len({file.episode_key for file in combination}) != len(combination):
            continue
        total = sum(file.size_bytes for file in combination)
        score = (total, tuple(-file.size_bytes for file in combination))
        if best_score is None or score < best_score:
            best = tuple(combination)
            best_score = score
    if best is None:
        return SelectionPlan(files=(), total_bytes=0, reason="ambiguous_mapping")
    total = sum(file.size_bytes for file in best)
    already_present = sum((existing_sizes or {}).get(key, 0) for key in existing)
    if max_work is not None and total + already_present > max_work:
        return SelectionPlan(files=(), total_bytes=total + already_present, reason="over_budget")
    return SelectionPlan(files=best, total_bytes=total)
