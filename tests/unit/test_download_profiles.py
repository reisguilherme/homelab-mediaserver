import pytest

from homeserver_common.env import load_settings
from homeserver_control.worker.release_quality import ReleasePolicy


def test_shared_quality_tiers_tie_webdl_bdrip_brrip_without_enabling_native_disk():
    from homeserver_common.download_profiles import PROFILE_QUALITIES, PROFILE_QUALITY_TIERS

    assert PROFILE_QUALITY_TIERS["Max Quality"] == (
        (2160, ("remux",)), (2160, ("bluray",)),
        (2160, ("webdl", "bdrip", "brrip")),
        (1080, ("remux",)), (1080, ("bluray",)),
        (1080, ("webdl", "bdrip", "brrip")),
    )
    for name in ("Standard", "Dublado"):
        assert PROFILE_QUALITY_TIERS[name] == (
            (1080, ("bluray",)), (1080, ("webdl", "bdrip", "brrip")),
        )
        assert PROFILE_QUALITIES[name] == ((1080, "bluray"), (1080, "webdl"))


def _release(resolution=1080, source="bluray", *, seeders=5, languages=(), title="Fixture"):
    return {
        "title": title,
        "size": 3_000_000_000,
        "seeders": seeders,
        "languages": languages,
        "quality": {"quality": {
            "resolution": resolution,
            "source": "bluray" if source == "remux" else source,
            "modifier": "remux" if source == "remux" else "none",
        }},
    }


@pytest.mark.parametrize("media_kind", ["movie", "series"])
def test_default_profile_rejects_uhd_remux_and_accepts_only_fhd_bluray_webdl(media_kind):
    policy = ReleasePolicy.from_environment({}, media_kind=media_kind)
    assert policy.profile_name == "Standard"
    assert policy.rank(_release(1080, "bluray")) is not None
    assert policy.rank(_release(1080, "webdl")) is not None
    for resolution, source in [(2160, "webdl"), (1080, "remux"), (720, "bluray"),
                               (1080, "webrip"), (1080, "hdtv")]:
        assert policy.rank(_release(resolution, source)) is None


def test_max_quality_orders_every_visual_tier_before_audio_and_seed_count():
    policy = ReleasePolicy.from_environment({}).for_profile("Max Quality")
    tiers = [(2160, "remux"), (2160, "bluray"), (2160, "webdl"),
             (1080, "remux"), (1080, "bluray"), (1080, "webdl")]
    for better, worse in zip(tiers, tiers[1:], strict=False):
        assert policy.rank(_release(*better), original_language="en") > policy.rank(
            _release(*worse, seeders=1000, languages=["en"], title="Fixture DV Atmos"),
            original_language="en",
        )


@pytest.mark.parametrize("seeders", [None, 0, 1, 4, True, -1, "100", 5.5])
@pytest.mark.parametrize("profile", ["Max Quality", "Standard", "Dublado"])
def test_named_profiles_require_proven_minimum_seeders(profile, seeders):
    policy = ReleasePolicy.from_environment({}).for_profile(profile)
    assert policy.rank(
        _release(seeders=seeders, languages=["pt-BR", "en"]), original_language="en",
    ) is None


def test_minimum_seeders_is_configurable_and_preserved_by_profile_selection():
    policy = ReleasePolicy.from_environment({"HOMESERVER_RELEASE_MIN_SEEDERS": "10"})
    for profile in ("Max Quality", "Standard", "Dublado"):
        selected = policy.for_profile(profile)
        assert selected.rank(
            _release(seeders=9, languages=["pt-BR", "en"]), original_language="en",
        ) is None
        assert selected.rank(
            _release(seeders=10, languages=["pt-BR", "en"]), original_language="en",
        ) is not None


def test_dublado_prioritizes_dual_then_original_and_rejects_brazilian_only():
    policy = ReleasePolicy.from_environment({}).for_profile("Dublado")
    dual = _release(source="webdl", languages=["pt-BR", "en"])
    dubbed = _release(languages=["pt-BR"], seeders=500)
    original = _release(languages=["en"], seeders=1000)
    assert policy.rank(dual, original_language="en") > policy.rank(
        original, original_language="en"
    )
    assert policy.rank(dubbed, original_language="en") is None
    assert policy.rank(_release(languages=["fr"]), original_language="en") is None
    for resolution, source in [(2160, "bluray"), (1080, "remux")]:
        assert policy.rank(_release(resolution, source, languages=["pt-BR", "en"])) is None


@pytest.mark.parametrize("profile", ["Max Quality", "Standard", "Dublado"])
def test_genre_flag_does_not_override_explicit_profile(profile):
    policy = ReleasePolicy.from_environment({}).for_profile(profile)
    release = _release(languages=["fr"], seeders=10)
    assert policy.rank(release, original_language="en", animation=True) == policy.rank(
        release, original_language="en", animation=False,
    )


def test_named_profiles_rank_audio_then_seeds_then_hdr_and_size_within_visual_tier():
    policy = ReleasePolicy.from_environment({})
    original = _release(languages=["en"], seeders=5)
    other = _release(languages=["fr"], seeders=100)
    assert policy.rank(original, original_language="en") is not None
    assert policy.rank(other, original_language="en") is None
    assert policy.rank(_release(seeders=6)) > policy.rank(_release(title="Fixture DV Atmos"))
    assert policy.rank(_release(title="Fixture DV")) > policy.rank(_release(title="Fixture Atmos"))
    assert policy.rank(_release(title="Fixture Atmos")) > policy.rank(_release())


def test_selected_profile_is_immutable_and_does_not_leak_between_titles():
    standard = ReleasePolicy.from_environment({})
    dubbed = standard.for_profile("Dublado")
    maximum = dubbed.for_profile("Max Quality")
    assert dubbed.dubbed
    assert not maximum.dubbed
    assert not standard.dubbed
    assert standard.rank(_release(2160, "remux")) is None
    assert maximum.rank(_release(2160, "remux")) is not None
    with pytest.raises(ValueError, match="profile"):
        standard.for_profile("Animation")


def test_dublado_size_check_does_not_require_original_language_or_seed_metadata():
    policy = ReleasePolicy.from_environment(
        {"HOMESERVER_QUALITY_MIN_MIB_PER_MIN_1080": "20"},
    ).for_profile("Dublado")
    release = _release(languages=["en"], seeders=None)
    assert policy.rank(release) is None
    assert policy.accepts_size(release, video_bytes=3_000_000_000, runtime_minutes=45)
    assert not policy.accepts_size(release, video_bytes=100_000_000, runtime_minutes=45)
    assert not policy.accepts_size(
        _release(2160, "webdl"), video_bytes=30_000_000_000, runtime_minutes=45,
    )


def test_legacy_resolution_and_animation_settings_load_without_overriding_profiles(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "HOMESERVER_MOVIE_RESOLUTIONS=2160\nHOMESERVER_SERIES_RESOLUTIONS=1080\n"
        "HOMESERVER_MEDIA_SOURCES=remux\n"
        "HOMESERVER_ANIMATION_AUDIO_PREFERENCES=original,dual,pt-BR\n"
    )
    policy = ReleasePolicy.from_environment(load_settings(path).as_environment())
    assert policy.rank(_release(1080, "webdl"), animation=True) is not None
    assert policy.rank(_release(2160, "remux")) is None
    dubbed = policy.for_profile("Dublado")
    assert dubbed.rank(_release(languages=["pt-BR", "en"]), original_language="en") > dubbed.rank(
        _release(languages=["en"]), original_language="en",
    )
    assert dubbed.rank(_release(languages=["pt-BR"]), original_language="en") is None


def test_default_profile_is_configurable_but_named_audio_order_remains_fixed(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "HOMESERVER_DOWNLOAD_DEFAULT_PROFILE=Dublado\n"
        "HOMESERVER_DUBBED_AUDIO_PREFERENCES=original,dual,pt-BR\n"
        "HOMESERVER_RELEASE_MIN_SEEDERS=8\n"
    )
    settings = load_settings(path)
    policy = ReleasePolicy.from_environment(settings.as_environment())
    assert policy.dubbed
    assert policy.rank(
        _release(languages=["pt-BR", "en"], seeders=8), original_language="en",
    ) > policy.rank(
        _release(languages=["en"], seeders=100), original_language="en",
    )
    assert policy.rank(_release(languages=["pt-BR"], seeders=100), original_language="en") is None


@pytest.mark.parametrize("key,value", [
    ("DOWNLOAD_DEFAULT_PROFILE", "Animation"), ("RELEASE_MIN_SEEDERS", "0"),
    ("RELEASE_MIN_SEEDERS", "-1"), ("DUBBED_AUDIO_PREFERENCES", "pt-PT"),
    ("DUBBED_AUDIO_PREFERENCES", "dual,dual"), ("DUBBED_AUDIO_PREFERENCES", ""),
])
def test_invalid_named_profile_preferences_are_rejected_by_settings(tmp_path, key, value):
    path = tmp_path / ".env"
    path.write_text(f"HOMESERVER_{key}={value}\n")
    with pytest.raises(ValueError, match=f"HOMESERVER_{key}"):
        load_settings(path)


def test_catalog_exposes_named_profile_controls_and_hides_obsolete_quality_controls():
    from homeserver_common.catalog import catalog

    keys = {row["key"] for row in catalog()}
    assert {"HOMESERVER_DOWNLOAD_DEFAULT_PROFILE", "HOMESERVER_RELEASE_MIN_SEEDERS",
            "HOMESERVER_DUBBED_AUDIO_PREFERENCES"} <= keys
    assert not {"HOMESERVER_MOVIE_RESOLUTIONS", "HOMESERVER_SERIES_RESOLUTIONS",
                "HOMESERVER_MEDIA_SOURCES", "HOMESERVER_ANIMATION_AUDIO_PREFERENCES"} & keys
