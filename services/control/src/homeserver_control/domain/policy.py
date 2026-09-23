def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def within_limits(movie_bytes: int | None, episode_bytes: list[int]) -> bool:
    """Validate the shape and positive sizes of an already mapped release."""
    if movie_bytes is not None:
        return (
            _positive_integer(movie_bytes)
            and not episode_bytes
        )
    return (
        bool(episode_bytes)
        and all(_positive_integer(size) for size in episode_bytes)
    )
