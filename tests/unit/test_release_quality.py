from homeserver_control.worker.release_quality import ReleasePolicy, release_rank


def test_configured_resolution_and_source_order():
    policy = ReleasePolicy.from_environment(
        {"HOMESERVER_MOVIE_RESOLUTIONS": "1080", "HOMESERVER_MEDIA_SOURCES": "webdl,remux"}
    )
    offered = _release()
    offered["quality"]["quality"]["resolution"] = 1080
    assert policy.rank(offered) is not None
    offered["quality"]["quality"]["resolution"] = 2160
    assert policy.rank(offered) is None


def _release(
    *,
    source="bluray",
    modifier="remux",
    resolution=2160,
    title="Film 2160p BluRay REMUX DV Atmos",
    size=10_000,
    seeders=None,
):
    release = {
        "title": title,
        "size": size,
        "quality": {
            "quality": {
                "source": source,
                "modifier": modifier,
                "resolution": resolution,
            }
        },
    }
    if seeders is not None:
        release["seeders"] = seeders
    return release


def test_more_seeders_beat_larger_file_at_equal_quality() -> None:
    assert release_rank(_release(size=1_000, seeders=50)) > release_rank(
        _release(size=10_000, seeders=2)
    )


def test_seeds_win_at_same_resolution_before_source_and_hdr_preferences() -> None:
    remux = _release(source="bluray", modifier="remux", seeders=1)
    webdl = _release(source="webdl", modifier="none", seeders=100)
    assert release_rank(webdl) > release_rank(remux)
    assert release_rank(_release(seeders=1)) < release_rank(
        _release(title="Film 2160p BluRay REMUX", seeders=100)
    )


def test_movie_highest_allowed_resolution_precedes_seed_count_and_source_family() -> None:
    uhd = _release(source="webdl", modifier="none", resolution=2160, seeders=1)
    full_hd = _release(source="bluray", modifier="remux", resolution=1080, seeders=100)
    assert ReleasePolicy.from_environment({}).rank(uhd) > ReleasePolicy().rank(full_hd)


def test_series_only_accepts_full_hd_while_movies_allow_uhd_fallback_full_hd() -> None:
    from homeserver_control.worker.series_acquisition import _series_rank

    series = ReleasePolicy.from_environment({}, media_kind="series")
    assert series.resolutions == (1080,)
    assert ReleasePolicy.from_environment({}).resolutions == (2160, 1080)
    assert _series_rank(_release(resolution=1080), series) is not None
    assert _series_rank(_release(resolution=2160), ReleasePolicy()) is None
    assert _series_rank(_release(resolution=720), ReleasePolicy(resolutions=(720,))) is None


def test_invalid_seed_count_does_not_outweigh_known_zero() -> None:
    known_zero = release_rank(_release(seeders=0))
    for invalid in (True, -1, 1.5, "100", None):
        assert known_zero > release_rank(_release(seeders=invalid))
