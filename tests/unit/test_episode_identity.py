import importlib.util

import pytest


def resolve(path, rows, **options):
    assert importlib.util.find_spec("homeserver_control.domain.episode_identity") is not None
    from homeserver_control.domain.episode_identity import resolve_episode_identity

    return resolve_episode_identity(path, rows, **options)


ROWS = [
    {"seasonNumber": 1, "episodeNumber": 1, "absoluteEpisodeNumber": 1},
    {"seasonNumber": 2, "episodeNumber": 1, "absoluteEpisodeNumber": 13},
    {"seasonNumber": 2, "episodeNumber": 2, "absoluteEpisodeNumber": 14},
]


@pytest.mark.parametrize(
    "name", ["Show.S02E01.mkv", "Show.SE02.EP01.mkv", "Show.S02.EP01.mkv", "Show.2x01.mkv"]
)
def test_explicit_season_episode_forms_bind_only_exact_canonical_scope(name):
    assert resolve(name, ROWS, season=2) == (2, 1)


def test_ep_only_requires_explicit_trusted_season_context():
    assert resolve("Show.EP01.mkv", ROWS) is None
    assert resolve("Show.EP01.mkv", ROWS, season=2) == (2, 1)


def test_season_context_cannot_override_an_explicit_other_season_or_episode_range():
    assert resolve("Show.S01E01.mkv", ROWS, season=2) is None
    assert resolve("Show.S02E01-E02.mkv", ROWS, season=2) is None
    assert resolve("Show.S02E01E02.mkv", ROWS, season=2) is None


def test_anime_absolute_requires_a_unique_canonical_absolute_number():
    assert resolve("[Group] Show - 013 [1080p].mkv", ROWS, season=2, allow_absolute=True) == (2, 1)
    assert resolve("[Group] Show - 013 [1080p].mkv", ROWS, season=2) is None
    ambiguous = ROWS + [{"seasonNumber": 3, "episodeNumber": 1, "absoluteEpisodeNumber": 13}]
    assert (
        resolve("[Group] Show - 013 [1080p].mkv", ambiguous, season=2, allow_absolute=True) is None
    )


def test_resolution_year_hash_and_missing_absolute_metadata_are_not_episode_numbers():
    for name in ["Show.1080p.mkv", "Show.2024.mkv", "Show-13-14.mkv"]:
        assert resolve(name, ROWS, season=2, allow_absolute=True) is None
    assert (
        resolve(
            "Show - 013.mkv",
            [{"seasonNumber": 2, "episodeNumber": 1}],
            season=2,
            allow_absolute=True,
        )
        is None
    )


def test_four_digit_canonical_seasons_and_duplicate_episode_rows():
    rows = [{"seasonNumber": 1940, "episodeNumber": 1}]
    assert resolve("Woody.S1940E01.mkv", rows, season=1940) == (1940, 1)
    assert resolve("Woody.S1940E01.mkv", rows + rows, season=1940) is None


@pytest.mark.parametrize("name", ["Show.S02E01abc.mkv", "Show.EP01abc.mkv"])
def test_episode_marker_must_end_at_a_token_boundary(name):
    assert resolve(name, ROWS, season=2) is None


def test_release_title_with_final_episode_marker_is_not_a_file_extension():
    assert resolve("Show.S02E01", ROWS, season=2) == (2, 1)


@pytest.mark.parametrize(
    "name",
    [
        "Show.S02E01 Episode 02.mkv",
        "Show.EP02.S02E01.mkv",
        "Show.S02E01 EP01 to 02.mkv",
        "Show.Episode01-02.mkv",
        "Show.EP01 to 02.mkv",
        "Show.EP01to02.mkv",
        "Show.S02E01 Episode01 through Episode02.mkv",
        "Show.S02E01.S02E02.mkv",
        "Show.S02E01+E02.mkv",
        "Show.S02E01+02.mkv",
        "Show.S02E01&02.mkv",
        "Show.EP01 + Episode02.mkv",
        "Show - 013 & 014.mkv",
    ],
)
def test_all_episode_markers_must_agree_and_ranges_are_never_single_episodes(name):
    assert resolve(name, ROWS, season=2, allow_absolute=True) is None


@pytest.mark.parametrize(
    "name",
    [
        "Show.S02E01 Episode 01.mkv",
        "Show.EP01.S02E01.mkv",
        "Show.S02E01.S02E01.mkv",
        "Show.EP01 Episode01.mkv",
    ],
)
def test_repeated_identical_markers_are_safe_only_with_one_canonical_identity(name):
    assert resolve(name, ROWS, season=2) == (2, 1)
    assert resolve(name, ROWS + [ROWS[1]], season=2) is None
