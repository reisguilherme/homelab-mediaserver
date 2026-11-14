"""Series-year aliases must preserve verified pack subtitle identity."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from homeserver_control.worker.series_finalization import SeriesFinalizer

_AHS_EPISODE = (
    "American Horror Story (2011) - S02E01 - Welcome to Briarcliff "
    "(1080p BluRay x265 RZeroX)"
)


def _subtitle_title(stem, *, context=None, pack_title=None):
    return SeriesFinalizer._pack_subtitle_title(
        SimpleNamespace(quality_rank=(2, 1080, 0, 0, 0, 0)),
        Path(stem + ".mkv"),
        context if context is not None else {
            "title": "American Horror Story", "year": 2011,
            "firstAired": "2011-10-05T00:00:00Z",
        },
        season=2, number=1, pack_title=pack_title,
    )


@pytest.mark.parametrize("year", [2011, "2011"])
def test_verified_native_series_year_allows_pack_subtitle_search_title(year):
    assert _subtitle_title(_AHS_EPISODE, context={
        "title": "American Horror Story", "year": year,
    }) == _AHS_EPISODE


def test_canonical_title_without_series_year_still_allows_pack_subtitle_search():
    assert _subtitle_title("American.Horror.Story.S02E01.1080p.BluRay") == (
        "American.Horror.Story.S02E01.1080p.BluRay"
    )


def test_canonical_title_already_including_native_year_keeps_exact_match():
    assert _subtitle_title(_AHS_EPISODE, context={
        "title": "American Horror Story (2011)", "year": 2011,
    }) == _AHS_EPISODE


def test_filename_year_must_equal_the_native_series_year():
    assert _subtitle_title(_AHS_EPISODE, context={
        "title": "American Horror Story", "year": 2012,
    }) is None


@pytest.mark.parametrize("stem", [
    _AHS_EPISODE.replace("(2011)", "(2012)"),
    _AHS_EPISODE.replace("American Horror Story", "Other Story"),
    _AHS_EPISODE.replace("S02E01", "S01E01"),
    _AHS_EPISODE.replace("S02E01", "S02E02"),
    _AHS_EPISODE.replace("BluRay", "WEB-DL"),
])
def test_series_year_alias_preserves_title_episode_and_source_guards(stem):
    assert _subtitle_title(stem) is None


@pytest.mark.parametrize("year", [None, True, 11, 20110, 2011.0, "2011x", "201"])
def test_series_year_alias_requires_valid_native_four_digit_year(year):
    assert _subtitle_title(_AHS_EPISODE, context={
        "title": "American Horror Story", "year": year,
    }) is None


def test_series_year_alias_preserves_conflicting_editions_guard():
    assert _subtitle_title(
        _AHS_EPISODE.replace("1080p", "EXTENDED 1080p"),
        pack_title="American Horror Story S02 UNRATED BluRay",
    ) is None


def test_series_year_is_not_removed_from_different_canonical_names():
    assert _subtitle_title("Story.S02E01.1080p.BluRay", context={
        "title": "Story 2011", "year": 2011,
    }) is None
