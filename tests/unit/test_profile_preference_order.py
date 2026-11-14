"""Decision examples for the owner's explicit quality and audio preferences."""

import pytest

from homeserver_control.worker.release_quality import ReleasePolicy


def release(
    source="bluray",
    title="Fixture 1080p BluRay",
    languages=("en",),
    resolution=1080,
    modifier="none",
    seeds=20,
):
    return {
        "title": title,
        "languages": list(languages),
        "seeders": seeds,
        "size": 10 * 1024**3,
        "quality": {"quality": {"source": source, "modifier": modifier, "resolution": resolution}},
    }


@pytest.mark.parametrize("profile", ["Max Quality", "Standard", "Dublado"])
def test_bdrip_brrip_and_webdl_share_visual_tier(profile):
    policy = ReleasePolicy(profile_name=profile)
    ranks = [
        policy.rank(row, original_language="en")
        for row in (
            release("webdl", "Fixture 1080p WEB-DL"),
            release("bluray", "Fixture 1080p BDRip"),
            release("bluray", "Fixture 1080p BRRip"),
        )
    ]
    assert all(rank is not None for rank in ranks)
    assert ranks[0] == ranks[1] == ranks[2]
    assert policy.rank(release(), original_language="en") > ranks[0]


@pytest.mark.parametrize("profile", ["Max Quality", "Standard"])
def test_original_category_beats_dual_at_equal_visual_quality(profile):
    policy = ReleasePolicy(profile_name=profile)
    original = policy.rank(release(languages=("en",)), original_language="en")
    dual = policy.rank(release(languages=("en", "pt-BR")), original_language="en")
    assert original > dual


def test_dublado_prefers_dual_webdl_over_original_bluray():
    policy = ReleasePolicy(profile_name="Dublado")
    dual = policy.rank(
        release("webdl", "Fixture 1080p WEB-DL", ("en", "pt-BR")), original_language="en"
    )
    original = policy.rank(release(languages=("en",)), original_language="en")
    assert dual > original


@pytest.mark.parametrize("profile", ["Max Quality", "Standard", "Dublado"])
def test_brazilian_only_dub_without_original_is_rejected(profile):
    assert (
        ReleasePolicy(profile_name=profile).rank(
            release(languages=("pt-BR",)), original_language="en"
        )
        is None
    )


@pytest.mark.parametrize("profile", ["Max Quality", "Standard", "Dublado"])
def test_original_brazilian_movie_remains_eligible(profile):
    assert (
        ReleasePolicy(profile_name=profile).rank(
            release(languages=("pt-BR",)), original_language="pt-BR"
        )
        is not None
    )


def test_max_quality_respects_all_six_visual_tiers_even_for_rips():
    policy = ReleasePolicy(profile_name="Max Quality")
    offers = [
        release(resolution=2160, modifier="remux"),
        release(resolution=2160),
        release("bluray", "Fixture 2160p BRRip", resolution=2160),
        release(modifier="remux"),
        release(),
        release("bluray", "Fixture 1080p BDRip"),
    ]
    ranks = [policy.rank(row, original_language="en") for row in offers]
    assert all(rank is not None for rank in ranks)
    assert all(a > b for a, b in zip(ranks, ranks[1:], strict=False))


def test_inferred_movie_audio_cannot_promote_a_dual_claim():
    policy = ReleasePolicy(profile_name="Dublado")
    offered = release(title="Fixture 1080p BluRay MULTi", languages=("pt-BR", "en"))
    offered["audioLanguagesInferred"] = True
    assert policy.rank(offered, original_language="en") is None


def test_explicit_foreign_audio_without_original_is_rejected():
    assert (
        ReleasePolicy(profile_name="Standard").rank(
            release(languages=("fr",)), original_language="en"
        )
        is None
    )


def test_untagged_standard_candidate_retains_inconclusive_lowest_audio_rank():
    policy = ReleasePolicy(profile_name="Standard")
    unknown = release(languages=())
    assert policy.rank(unknown, original_language="en") is not None
    assert policy.rank(unknown, original_language="en") < policy.rank(
        release(), original_language="en"
    )
