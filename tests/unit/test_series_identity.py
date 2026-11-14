"""Canonical identities require unique native metadata or current Seerr proof."""

from copy import deepcopy

import pytest

from homeserver_control.worker.series_identity import SeriesIdentityResolver


def request(**changes):
    return {
        "source_id": "3:1", "kind": "season", "media_key": "season:tmdb:97546:1",
        "tvdb_id": 383203, "external_service_id": 6,
    } | changes


def test_missing_native_tmdb_is_normalized_only_in_an_independent_copy():
    resolver = SeriesIdentityResolver()
    native = {"id": 6, "tmdbId": 0, "tvdbId": 383203, "seasons": [{"seasonNumber": 1}]}
    before = deepcopy(native)
    assert resolver.resolve([native], 97546) is None
    resolver.update_requests([request()])
    result = resolver.resolve([native], 97546)
    assert result == {
        "id": 6, "tmdbId": 97546, "tvdbId": 383203, "seasons": [{"seasonNumber": 1}],
    }
    result["seasons"][0]["seasonNumber"] = 2
    assert native == before


@pytest.mark.parametrize("tmdb", [None, 0])
def test_global_tvdb_binding_is_sufficient_when_native_tmdb_is_unavailable(tmdb):
    resolver = SeriesIdentityResolver()
    binding = request()
    binding.pop("external_service_id")
    resolver.update_requests([binding])
    assert resolver.resolve([{"id": 6, "tmdbId": tmdb, "tvdbId": 383203}], 97546) == {
        "id": 6, "tmdbId": 97546, "tvdbId": 383203,
    }


def test_local_native_id_without_global_tvdb_cannot_normalize_missing_tmdb():
    resolver = SeriesIdentityResolver()
    binding = request()
    binding.pop("tvdb_id")
    resolver.update_requests([binding])
    assert resolver.resolve([{"id": 6, "tmdbId": 0, "tvdbId": 383203}], 97546) is None


@pytest.mark.parametrize("field", ["tvdb_id", "external_service_id"])
@pytest.mark.parametrize("invalid", [True, False, 0, -1, "6", None, 6.0])
def test_malformed_request_binding_cannot_normalize_a_native_series(field, invalid):
    resolver = SeriesIdentityResolver()
    resolver.update_requests([request(**{field: invalid})])
    assert resolver.resolve([{"id": 6, "tmdbId": 0, "tvdbId": 383203}], 97546) is None


@pytest.mark.parametrize("field,invalid", [
    ("tmdbId", True), ("tmdbId", False), ("tmdbId", "0"), ("tmdbId", -1),
    ("tmdbId", 999), ("id", True), ("id", 0), ("id", 7),
    ("tvdbId", True), ("tvdbId", 0), ("tvdbId", 999),
])
def test_binding_rejects_malformed_or_conflicting_native_identity(field, invalid):
    resolver = SeriesIdentityResolver()
    resolver.update_requests([request()])
    native = {"id": 6, "tmdbId": 0, "tvdbId": 383203} | {field: invalid}
    assert resolver.resolve([native], 97546) is None


@pytest.mark.parametrize("other", [
    request(tvdb_id=999), request(external_service_id=7),
    request(media_key="season:tmdb:999:1"),
])
def test_conflicting_current_requests_cannot_supply_identity_proof(other):
    resolver = SeriesIdentityResolver()
    resolver.update_requests([request(), other])
    assert resolver.resolve([{"id": 6, "tmdbId": 0, "tvdbId": 383203}], 97546) is None


def test_withdrawn_binding_is_forgotten_on_the_next_snapshot():
    resolver = SeriesIdentityResolver()
    native = {"id": 6, "tmdbId": 0, "tvdbId": 383203}
    resolver.update_requests([request()])
    assert resolver.resolve([native], 97546) is not None
    resolver.update_requests([])
    assert resolver.resolve([native], 97546) is None


def test_positive_tmdb_remains_sufficient_without_request_bindings():
    native = {"id": 6, "tmdbId": 97546}
    assert SeriesIdentityResolver().resolve([native], 97546) == native


@pytest.mark.parametrize("duplicate", [
    {"id": 7, "tmdbId": 97546, "tvdbId": 999},
    {"id": 6, "tmdbId": 999, "tvdbId": 999},
    {"id": 7, "tmdbId": 999, "tvdbId": 383203},
])
def test_duplicate_native_identity_is_rejected_even_without_request_binding(duplicate):
    native = {"id": 6, "tmdbId": 97546, "tvdbId": 383203}
    assert SeriesIdentityResolver().resolve([native, duplicate], 97546) is None
