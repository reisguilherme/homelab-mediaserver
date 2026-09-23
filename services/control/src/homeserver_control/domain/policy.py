MOVIE_LIMIT_BYTES = 80_000_000_000
MOVIE_RESERVATION_BYTES = MOVIE_LIMIT_BYTES + 1_000_000_000
EPISODE_LIMIT_BYTES = 5_000_000_000
SEASON_LIMIT_BYTES = 100_000_000_000


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def within_limits(movie_bytes: int | None, episode_bytes: list[int]) -> bool:
    """Validate an already mapped movie or season using decimal-byte limits."""
    if movie_bytes is not None:
        return (
            _positive_integer(movie_bytes)
            and movie_bytes <= MOVIE_LIMIT_BYTES
            and not episode_bytes
        )
    return (
        bool(episode_bytes)
        and all(_positive_integer(size) and size <= EPISODE_LIMIT_BYTES for size in episode_bytes)
        and sum(episode_bytes) <= SEASON_LIMIT_BYTES
    )
