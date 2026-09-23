from homeserver_control.domain.policy import within_limits


def test_movie_upper_boundary() -> None:
    assert within_limits(80_000_000_000, [])
    assert not within_limits(80_000_000_001, [])


def test_episode_limit_is_independent_from_season_total() -> None:
    assert not within_limits(None, [5_000_000_001])


def test_season_upper_boundary() -> None:
    assert within_limits(None, [5_000_000_000] * 20)
    assert not within_limits(None, [5_000_000_000] * 21)


def test_movie_and_episode_shapes_are_mutually_exclusive() -> None:
    assert not within_limits(1, [1])
    assert not within_limits(None, [])


def test_boolean_and_float_sizes_are_rejected() -> None:
    assert not within_limits(True, [])
    assert not within_limits(1.0, [])
    assert not within_limits(None, [1.0])
