import pytest

from homeserver_control.worker.release_quality import ReleasePolicy


def _release(*, languages=(), seeders=1, title="Fixture BluRay", **quality):
    return {
        "languages": languages,
        "seeders": seeders,
        "size": 1_000,
        "title": title,
        "quality": {
            "quality": {"source": "bluray", "modifier": "none", "resolution": 1080, **quality}
        },
    }


def test_declared_audio_preference_beats_seed_count():
    policy = ReleasePolicy(audio_languages=("pt-BR", "en-US"))
    preferred = _release(languages=[{"name": "pt-BR"}], seeders=1)
    fallback = _release(languages=[{"name": "en-US"}], seeders=100)
    assert policy.rank(preferred) > policy.rank(fallback)


@pytest.mark.parametrize("language", ["eng", "en", {"name": "English"}, {"iso6391": "en"}])
def test_explicit_language_aliases_are_preferred(language):
    policy = ReleasePolicy(audio_languages=("en",))
    assert policy.rank(_release(languages=[language])) > policy.rank(
        _release(languages=[{"name": "French"}], seeders=100)
    )


def test_audio_language_list_order_changes_the_selected_release():
    brazilian = _release(languages=["pt-BR"], seeders=1)
    american = _release(languages=["en-US"], seeders=100)
    assert ReleasePolicy(audio_languages=("pt-BR", "en-US")).rank(
        brazilian
    ) > ReleasePolicy(audio_languages=("pt-BR", "en-US")).rank(american)
    assert ReleasePolicy(audio_languages=("en-US", "pt-BR")).rank(
        american
    ) > ReleasePolicy(audio_languages=("en-US", "pt-BR")).rank(brazilian)


def test_original_audio_requires_media_language_context():
    policy = ReleasePolicy()
    english = _release(languages=[{"id": 1, "name": "English"}], seeders=1)
    french = _release(languages=[{"id": 2, "name": "French"}], seeders=100)
    assert policy.rank(english, original_language={"id": 1, "name": "English"}) > policy.rank(
        french, original_language={"id": 1, "name": "English"}
    )
    assert policy.rank(english) < policy.rank(french)


@pytest.mark.parametrize("unknown", [None, "Unknown", {"id": 1}, {}, True])
def test_unknown_original_language_keeps_seed_order_without_blocking(unknown):
    policy = ReleasePolicy()
    english = _release(languages=["en"], seeders=1)
    french = _release(languages=["fr"], seeders=100)
    assert policy.rank(english, original_language=unknown) is not None
    assert policy.rank(english, original_language=unknown) < policy.rank(
        french, original_language=unknown
    )


def test_release_title_does_not_declare_an_audio_language():
    policy = ReleasePolicy(audio_languages=("pt-BR",))
    guessed = _release(title="Fixture Brazilian Portuguese pt-BR Original", seeders=1)
    unknown = _release(title="Fixture", seeders=100)
    assert policy.rank(guessed) < policy.rank(unknown)


def test_generic_portuguese_does_not_establish_brazilian_audio():
    policy = ReleasePolicy(audio_languages=("pt-BR",))
    brazilian = _release(languages=[{"name": "Portuguese (Brazil)"}], seeders=1)
    generic = _release(languages=[{"name": "Portuguese"}], seeders=100)
    assert policy.rank(brazilian) > policy.rank(generic)


def test_bcp47_matching_preserves_explicit_regions():
    policy = ReleasePolicy(audio_languages=("en-US",))
    american = _release(languages=["en_us"], seeders=1)
    british = _release(languages=["en-GB"], seeders=100)
    generic = _release(languages=["eng"], seeders=100)
    assert policy.rank(american) > policy.rank(british)
    assert policy.rank(american) > policy.rank(generic)


@pytest.mark.parametrize(
    "quality,title",
    [
        ({"modifier": "remux"}, "Fixture"),
        ({"resolution": 2160}, "Fixture"),
        ({}, "Fixture DV"),
        ({}, "Fixture Atmos"),
    ],
)
def test_resolution_precedes_audio_while_source_and_hdr_are_tiebreakers(quality, title):
    policy = ReleasePolicy(audio_languages=("pt-BR",))
    higher_quality = _release(languages=["en"], title=title, **quality)
    preferred_audio = _release(languages=["pt-BR"], seeders=100)
    if quality.get("resolution") == 2160:
        assert policy.rank(higher_quality) > policy.rank(preferred_audio)
    else:
        assert policy.rank(preferred_audio) > policy.rank(higher_quality)


@pytest.mark.parametrize("languages", [None, True, [True, {}, {"id": 1}], ["Unknown"]])
def test_missing_or_malformed_audio_metadata_does_not_reject_release(languages):
    assert ReleasePolicy(audio_languages=("en-US",)).rank(_release(languages=languages)) is not None
