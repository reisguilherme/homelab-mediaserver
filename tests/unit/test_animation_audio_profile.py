import importlib.util

import pytest

from homeserver_common.env import load_settings
from homeserver_control.worker.release_quality import ReleasePolicy, release_rank


def _release(*, languages=(), title="Fixture 1080p BluRay", resolution=1080, seeders=10):
    return {
        "languages": languages,
        "title": title,
        "seeders": seeders,
        "size": 3_000_000_000,
        "quality": {"quality": {
            "source": "bluray", "modifier": "none", "resolution": resolution,
        }},
    }


@pytest.mark.parametrize("genres", [
    ["Animation"], ["Animação"], ["Anime"], [{"name": "Animation"}], [{"id": 16}],
])
def test_animation_requires_explicit_native_genre(genres):
    name = "homeserver_control.worker.media_profiles"
    assert importlib.util.find_spec(name) is not None, "animation classifier is missing"
    from homeserver_control.worker.media_profiles import is_animation

    assert is_animation({"genres": genres})


@pytest.mark.parametrize("media", [
    {}, None, True, {"genres": ["Kids", "Family"]}, {"seriesType": "anime"},
    {"genres": "Animation"}, {"genres": [{"id": True}]},
    {"genres": ["Animated Biography"]}, {"genres": [{"id": "16"}]},
])
def test_unproven_animation_does_not_change_live_action_policy(media):
    name = "homeserver_control.worker.media_profiles"
    assert importlib.util.find_spec(name) is not None, "animation classifier is missing"
    from homeserver_control.worker.media_profiles import is_animation

    assert not is_animation(media)


def test_animation_audio_tiers_precede_resolution_and_seed_count():
    policy = ReleasePolicy()
    dual = _release(languages=["pt-BR", "eng"], seeders=2)
    dubbed = _release(languages=["pt-BR"], resolution=2160, seeders=300)
    original = _release(languages=["eng"], resolution=2160, seeders=500)
    unknown = _release(seeders=1000)

    def rank(release):
        return policy.rank(release, original_language="en", animation=True)

    assert rank(dual) > rank(dubbed) > rank(original)
    assert rank(unknown) is None
    assert rank(dual)[:2] == (3, 1080)
    assert rank(dubbed)[:2] == (2, 2160)
    assert rank(original)[:2] == (1, 2160)


def test_non_animation_keeps_existing_original_language_resolution_order():
    policy = ReleasePolicy()
    dual = _release(languages=["pt-BR", "en"], seeders=2)
    original = _release(languages=["en"], resolution=2160, seeders=50)
    assert policy.rank(original, original_language="en", animation=False) > policy.rank(
        dual, original_language="en", animation=False,
    )
    assert policy.rank(original, original_language="en")[:2] == (2160, 1)


def test_anime_uses_declared_japanese_original_in_dual_tier():
    policy = ReleasePolicy()
    japanese = _release(languages=["pob", "jpn"], seeders=1)
    english_dub = _release(languages=["pob", "eng"], seeders=100)
    assert policy.rank(japanese, original_language={"name": "Japanese"}, animation=True)[0] == 3
    assert policy.rank(english_dub, original_language="ja", animation=True)[0] == 2


@pytest.mark.parametrize("languages,title", [
    (["en"], "Fixture 1080p Dual Audio"),
    (["Portuguese", "en"], "Fixture 1080p Dual Audio"),
    (["pt-PT", "en"], "Fixture 1080p Dual Audio"),
    (["pt-PT", "en"], "Fixture 1080p Dual Audio PT-BR ENG"),
    (["Portuguese (Portugal)", "en"], "Fixture 1080p Dual Audio PT-BR ENG"),
    (["European Portuguese", "en"], "Fixture 1080p Dual Audio PT-BR ENG"),
])
def test_dual_label_and_generic_portuguese_do_not_establish_brazilian_audio(languages, title):
    assert ReleasePolicy().rank(
        _release(languages=languages, title=title), original_language="en", animation=True,
    )[0] == 1


@pytest.mark.parametrize("marker", [
    "PT-BR", "PTBR", "POR-BR", "Portuguese (Brazil)", "Dublado BR",
])
def test_unambiguous_title_brazilian_audio_marker_can_supplement_native_metadata(marker):
    assert ReleasePolicy().rank(
        _release(title=f"Fixture 1080p BluRay {marker}"),
        original_language="en", animation=True,
    )[0] == 2


def test_explicit_audio_language_list_in_title_establishes_both_dual_languages():
    assert release_rank(
        _release(title="Fixture 1080p BluRay DUAL AUDIO PT-BR ENG"),
        original_language="en", animation=True,
    )[0] == 3


@pytest.mark.parametrize("title,original", [
    ("[DUAL AUDIO PT-BR] The English Teacher 1080p BluRay", "en"),
    ("[DUAL AUDIO PT-BR] The Japanese Dog 1080p BluRay", "ja"),
    ("(DUAL AUDIO PT-BR) The German Teacher 1080p BluRay", "de"),
    ("DUAL AUDIO PT-BR The English Teacher 1080p BluRay", "en"),
    ("DUAL AUDIO PT-BR English Teacher 1080p BluRay", "en"),
    ("DUAL AUDIO PT-BR Japanese Dog 1080p BluRay", "ja"),
    ("DUAL AUDIO PT-BR 1080p BluRay English-Group", "en"),
    ("[DUAL AUDIO Portuguese (Brazil)] The English Teacher 1080p BluRay", "en"),
])
def test_audio_title_list_stops_before_movie_words_and_release_metadata(title, original):
    assert ReleasePolicy().rank(
        _release(title=title), original_language=original, animation=True,
    )[0] == 2


@pytest.mark.parametrize("title", [
    "[DUAL AUDIO PT-BR ENG] The Teacher 1080p BluRay",
    "(DUAL AUDIO PTBR+ENG) The Teacher 1080p BluRay",
    "DUAL AUDIO Portuguese (Brazil) / English [1080p BluRay]",
])
def test_bounded_audio_blocks_preserve_explicit_brazilian_and_original_languages(title):
    assert ReleasePolicy().rank(
        _release(title=title), original_language="en", animation=True,
    )[0] == 3


@pytest.mark.parametrize("portuguese", [
    {"iso6391": "pt", "name": "Portuguese (Brazil)"},
    {"code": "por", "name": "Brazilian Portuguese"},
])
def test_animation_audio_preserves_explicit_region_when_native_code_is_generic(portuguese):
    assert ReleasePolicy().rank(
        _release(languages=[portuguese, "en"]), original_language="en", animation=True,
    )[0] == 3


@pytest.mark.parametrize("portuguese", [
    {"iso6391": "pt", "name": "Portuguese (Portugal)"},
    {"code": "pt-PT", "name": "Portuguese (Brazil)"},
    {"code": "pt-BR", "name": "Portuguese (Portugal)"},
])
def test_animation_audio_region_conflict_never_affirms_brazilian_track(portuguese):
    assert ReleasePolicy().rank(
        _release(languages=[portuguese, "en"], title="Fixture DUAL AUDIO PT-BR ENG"),
        original_language="en", animation=True,
    )[0] == 1


@pytest.mark.parametrize("title", [
    "Brazil 1080p BluRay", "Fixture 1080p Portuguese", "Fixture 1080p English",
    "Fixture 1080p PT-BR Subtitles", "Fixture 1080p Legendas PTBR",
    "Fixture 1080p Dublado",
])
def test_title_country_or_subtitle_tags_do_not_establish_audio(title):
    assert ReleasePolicy().rank(
        _release(title=title), original_language="en", animation=True,
    ) is None


@pytest.mark.parametrize("original", [None, "Unknown", {"id": 1}, {}, True])
def test_unknown_original_language_never_assumes_english(original):
    policy = ReleasePolicy()
    assert policy.rank(
        _release(languages=["en"]), original_language=original, animation=True,
    ) is None
    assert policy.rank(
        _release(languages=["pt-BR", "en"]), original_language=original, animation=True,
    )[0] == 2


def test_two_brazilian_labels_do_not_claim_a_distinct_original_track():
    assert ReleasePolicy().rank(
        _release(languages=["pt-BR", "pob"], title="Fixture 1080p Dual Audio"),
        original_language="pt-BR", animation=True,
    )[0] == 2


def test_animation_keeps_quality_rejections_and_seed_tiebreaker():
    policy = ReleasePolicy()
    poor = _release(languages=["pt-BR", "en"])
    poor["quality"]["quality"]["source"] = "webrip"
    assert policy.rank(poor, original_language="en", animation=True) is None
    low_seeds = _release(languages=["pt-BR", "en"], seeders=1)
    high_seeds = _release(languages=["pt-BR", "en"], seeders=50)
    assert policy.rank(high_seeds, original_language="en", animation=True) > policy.rank(
        low_seeds, original_language="en", animation=True,
    )


@pytest.mark.parametrize("languages", [["fr"], ["pt-PT"], ["Portuguese"], [], None])
def test_animation_rejects_sources_outside_brazilian_or_proven_original_audio(languages):
    assert ReleasePolicy().rank(
        _release(languages=languages), original_language="en", animation=True,
    ) is None


def test_live_action_missing_audio_metadata_keeps_its_existing_fallback():
    assert ReleasePolicy().rank(_release(), original_language="en", animation=False) is not None


def test_legacy_animation_audio_order_remains_loadable_for_direct_policy_callers(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_ANIMATION_AUDIO_PREFERENCES=original,dual,pt-BR\n")
    settings = load_settings(path, mode="dev")
    assert settings.animation_audio_preferences == ("original", "dual", "pt-BR")
    policy = ReleasePolicy(animation_audio_preferences=settings.animation_audio_preferences)
    assert policy.animation_audio_preferences == ("original", "dual", "pt-BR")
    original = _release(languages=["en"], seeders=1)
    dubbed = _release(languages=["pt-BR"], seeders=100)
    assert policy.rank(original, original_language="en", animation=True) > policy.rank(
        dubbed, original_language="en", animation=True,
    )


def test_animation_default_is_enabled_without_private_configuration(tmp_path):
    path = tmp_path / ".env"
    path.write_text("")
    settings = load_settings(path, mode="dev")
    assert settings.animation_audio_preferences == ("dual", "pt-BR", "original")
    assert ReleasePolicy.from_environment({}).animation_audio_preferences == (
        "dual", "pt-BR", "original",
    )


@pytest.mark.parametrize("value", ["pt-PT", "dual,dual", "dubbed", ""])
def test_invalid_animation_audio_preference_is_rejected(tmp_path, value):
    path = tmp_path / ".env"
    path.write_text(f"HOMESERVER_ANIMATION_AUDIO_PREFERENCES={value}\n")
    with pytest.raises(ValueError, match="HOMESERVER_ANIMATION_AUDIO_PREFERENCES"):
        load_settings(path, mode="dev")
