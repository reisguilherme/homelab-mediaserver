import importlib.util
from datetime import UTC, datetime, timedelta

import pytest

NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def classify(rows, **options):
    assert importlib.util.find_spec("homeserver_control.domain.season_completion") is not None
    from homeserver_control.domain.season_completion import season_completion

    return season_completion(rows, season=1, now=NOW, **options)


def episode(number, *, season=1, offset=-1, **extra):
    return {
        "id": season * 100 + number,
        "seasonNumber": season,
        "episodeNumber": number,
        "airDateUtc": (NOW + timedelta(days=offset)).isoformat(),
        **extra,
    }


def test_all_known_aired_does_not_prove_current_season_complete():
    assert classify([episode(1), episode(2)], series_status="continuing").state == "unknown"


@pytest.mark.parametrize("evidence", ["ended", "finale", "later_season"])
def test_completed_season_needs_canonical_completion_evidence(evidence):
    rows = [episode(1), episode(2, monitored=False)]
    status = "ended" if evidence == "ended" else "continuing"
    if evidence == "finale":
        rows[-1]["finaleType"] = "season"
    if evidence == "later_season":
        rows.append(episode(1, season=2))
    result = classify(rows, series_status=status, expected_episode_count=2)
    assert result.state == "complete"
    assert result.episode_numbers == (1, 2)


def test_known_future_means_ongoing_even_if_the_series_is_marked_ended():
    result = classify([episode(1), episode(2, offset=1)], series_status="ended")
    assert result.state == "ongoing"
    assert result.aired_episode_numbers == (1,)


@pytest.mark.parametrize("change", ["date", "count", "duplicate", "naive", "partial"])
def test_incomplete_or_ambiguous_catalog_waits_for_metadata(change):
    rows = [episode(1), episode(2)]
    options = {"series_status": "ended", "expected_episode_count": 2}
    if change == "date":
        rows[1]["airDateUtc"] = None
    elif change == "count":
        options["expected_episode_count"] = 3
    elif change == "duplicate":
        rows.append(episode(2))
    elif change == "naive":
        rows[1]["airDateUtc"] = "2026-10-01T12:00:00"
    else:
        options["metadata_complete"] = False
    assert classify(rows, **options).state == "unknown"


def test_midseason_finale_does_not_close_season_and_future_specials_do_not_block_it():
    rows = [episode(1, finaleType="midseason"), episode(1, season=0, offset=1)]
    assert classify(rows, series_status="continuing").state == "unknown"
    rows[0]["finaleType"] = "series"
    assert classify(rows, series_status="ended").state == "complete"


def test_future_later_season_alone_does_not_prove_completion():
    assert (
        classify([episode(1), episode(1, season=2, offset=2)], series_status="continuing").state
        == "unknown"
    )


def test_gapped_or_nonstarting_episode_catalog_does_not_prove_complete():
    assert classify([episode(1), episode(3)], series_status="ended").state == "unknown"
    assert classify([episode(2)], series_status="ended").state == "unknown"


def test_audited_release_receipt_requires_explicit_limited_native_id_allowlist():
    rows = [episode(1), episode(2)]
    rows[1].update(id=10427540, airDateUtc=None, released_before_audit_verified=True)
    assert classify(rows, series_status="ended").state == "unknown"
    result = classify(
        rows, series_status="ended", verified_released_episode_ids=frozenset({10427540})
    )
    assert result.state == "complete"
    assert "audited_release_before_cutoff" in result.evidence
    assert result.aired_episode_numbers == (1, 2)
    assert (
        classify(
            rows, series_status="ended", verified_released_episode_ids=frozenset({10427543})
        ).state
        == "unknown"
    )
    rows[1]["airDateUtc"] = (NOW + timedelta(days=1)).isoformat()
    assert (
        classify(
            rows, series_status="ended", verified_released_episode_ids=frozenset({10427540})
        ).state
        == "ongoing"
    )
