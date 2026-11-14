from homeserver_control.worker.release_quality import ReleasePolicy, release_rank


def test_default_indexer_fallback_requires_meaningful_swarm() -> None:
    assert ReleasePolicy.from_environment({}).indexer_fallback_min_seeders == 20
    assert ReleasePolicy.from_environment({}, media_kind="series").indexer_fallback_min_seeders == 5


def test_movie_indexer_fallback_can_differ_from_series() -> None:
    environment = {
        "HOMESERVER_INDEXER_FALLBACK_MIN_SEEDERS": "5",
        "HOMESERVER_MOVIE_INDEXER_FALLBACK_MIN_SEEDERS": "20",
    }
    assert ReleasePolicy.from_environment(environment).indexer_fallback_min_seeders == 20
    assert ReleasePolicy.from_environment(
        environment, media_kind="series"
    ).indexer_fallback_min_seeders == 5


def test_legacy_constructor_keeps_configured_resolution_and_source_order():
    policy = ReleasePolicy(resolutions=(1080,), sources=("webdl", "remux"))
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


def test_legacy_constructor_keeps_resolution_before_seeds_and_source_family() -> None:
    uhd = _release(source="webdl", modifier="none", resolution=2160, seeders=1)
    full_hd = _release(source="bluray", modifier="remux", resolution=1080, seeders=100)
    assert ReleasePolicy().rank(uhd) > ReleasePolicy().rank(full_hd)


def test_standard_defaults_to_full_hd_for_movies_and_series() -> None:
    from homeserver_control.worker.series_acquisition import _series_rank

    series = ReleasePolicy.from_environment({}, media_kind="series")
    assert series.resolutions == (1080,)
    assert ReleasePolicy.from_environment({}).resolutions == (1080,)
    full_hd = _release(resolution=1080, modifier="none", seeders=5, title="Series 1080p BluRay")
    assert _series_rank(full_hd, series) is not None
    assert _series_rank(_release(resolution=2160), ReleasePolicy()) is None
    assert _series_rank(_release(resolution=720), ReleasePolicy(resolutions=(720,))) is None


def test_invalid_seed_count_does_not_outweigh_known_zero() -> None:
    known_zero = release_rank(_release(seeders=0))
    for invalid in (True, -1, 1.5, "100", None):
        assert known_zero > release_rank(_release(seeders=invalid))


def test_native_radarr_remux_is_ranked_as_remux() -> None:
    native_remux = _release(source="blurayRaw", modifier="none", seeders=10)
    native_remux["quality"]["quality"].update(name="Bluray-2160p Remux")
    assert release_rank(native_remux) == release_rank(
        _release(source="bluray", modifier="remux", seeders=10)
    )
    assert ReleasePolicy(sources=("bluray", "webdl")).rank(native_remux) is None


def test_native_radarr_web_source_requires_explicit_webdl_classification() -> None:
    native_webdl = _release(source="web", modifier="none", seeders=10)
    native_webdl["quality"]["quality"].update(name="WEBDL-2160p")
    native_webrip = _release(source="web", modifier="none", seeders=10)
    native_webrip["quality"]["quality"].update(name="WEBRip-2160p")
    assert release_rank(native_webdl) == release_rank(
        _release(source="webdl", modifier="none", seeders=10)
    )
    assert release_rank(native_webrip) is None


def test_dubbed_original_fallback_accepts_native_latvian_movie_and_release() -> None:
    policy = ReleasePolicy.from_environment({}).for_profile("Dublado")
    offered = _release(
        resolution=1080, modifier="none", seeders=93,
        title="Flow.2024.1080p.BluRay.KNiVES", size=13 * 1024**3,
    )
    offered["languages"] = [{"id": 36, "name": "Latvian"}]

    rank = policy.rank(offered, original_language={"id": 36, "name": "Latvian"})

    assert rank is not None
    assert rank[:3] == (1, 2, 93)


def test_dubbed_original_fallback_accepts_latvian_iso_codes() -> None:
    policy = ReleasePolicy.from_environment({}).for_profile("Dublado")
    offered = _release(resolution=1080, modifier="none", seeders=10)
    for code in ("lv", "lav"):
        offered["languages"] = [{"code": code}]
        rank = policy.rank(offered, original_language={"id": 36, "name": "Latvian"})
        assert rank is not None
        assert rank[0] == 1


def test_dubbed_original_fallback_does_not_infer_unknown_language_or_brazilian_audio() -> None:
    policy = ReleasePolicy.from_environment({}).for_profile("Dublado")
    offered = _release(resolution=1080, modifier="none", seeders=10)
    for original, declared in (
        ({"id": 36}, {"id": 36}),
        ({"name": "ImaginaryLanguage"}, {"name": "ImaginaryLanguage"}),
        ({"name": "Latvian"}, {"name": "Unknown"}),
        ({"name": "Latvian"}, {"name": "Portuguese (Portugal)"}),
    ):
        offered["languages"] = [declared]
        assert policy.rank(offered, original_language=original) is None


def test_asc_native_dual_declaration_qualifies_known_original_language() -> None:
    offered = _release(
        resolution=1080, modifier="none", seeders=5,
        title="Fixture 1080p BluRay Dual-Audio Brazilian Original",
    )
    offered.update(indexer="Amigos Share Club (Prowlarr)", audioEvidence=[], languages=["English"])
    rank = ReleasePolicy(profile_name="Dublado").rank(offered, original_language="ja")
    assert rank is not None
    assert rank[:3] == (3, 2, 5)


def test_asc_source_label_aliases_are_exact_and_preserve_public_aliases() -> None:
    from homeserver_control.worker.release_quality import release_indexer

    for label, expected in (
        ("Amigos Share Club", "amigosshare"), ("amigosshare", "amigosshare"),
        ("amigos-share.club", "amigosshare"), ("ASC", "amigosshare"),
        ("Amigos Share Club (Prowlarr)", "amigosshare"),
        ("UIndex.org (Prowlarr)", "uindex"), ("www.1337x.to", "1337x"),
        ("Fake Amigos Share Club", None), ("ASC Clone", None),
        ("amigos-share.club.evil.invalid", None),
    ):
        assert release_indexer({"indexer": label}) == expected
    assert release_indexer({"indexer": "Other", "title": "Amigos Share Club ASC"}) is None


def test_asc_brazilian_declaration_cannot_bypass_minimum_seeds() -> None:
    offered = _release(
        resolution=1080, modifier="none", seeders=4,
        title="Fixture 1080p BluRay Dual-Audio Brazilian Original",
    )
    offered.update(indexer="Amigos Share Club", audioEvidence=[])
    policy = ReleasePolicy(profile_name="Dublado")
    assert policy.rank(offered, original_language="en") is None
    offered["seeders"] = 5
    assert policy.rank(offered, original_language="en")[0] == 3
