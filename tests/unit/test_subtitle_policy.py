import pytest

from homeserver_control.worker.subtitle_language import SubtitlePolicy
from homeserver_control.worker.subtitle_recovery import recover_subtitle


def test_policy_order_and_unsupported_region():
    policy = SubtitlePolicy.from_environment(
        {
            "HOMESERVER_SUBTITLE_LANGUAGES": "en-US,pt-BR",
            "HOMESERVER_SUBTITLE_MATCH_MODES": "release",
        }
    )
    assert policy.attempts == (("EN", "exact"), ("BR_PT", "exact"))
    with pytest.raises(ValueError, match="SUBTITLE_LANGUAGES"):
        SubtitlePolicy.from_environment({"HOMESERVER_SUBTITLE_LANGUAGES": "pt-PT"})


@pytest.mark.asyncio
async def test_recovery_keeps_each_language_modes_together():
    calls = []

    async def fetch(language, match_mode):
        calls.append((language, match_mode))
        return b"subtitle" if language == "EN" else None

    result = await recover_subtitle(SubtitlePolicy(), fetch)
    assert calls == [("BR_PT", "exact"), ("BR_PT", "same_duration"), ("EN", "exact")]
    assert result.language == "EN"


@pytest.mark.asyncio
async def test_recovery_does_not_query_disabled_provider():
    async def fetch(language, match_mode):
        raise AssertionError("disabled provider queried")

    policy = SubtitlePolicy.from_environment({"HOMESERVER_SUBTITLE_PROVIDERS": "bazarr"})
    assert await recover_subtitle(policy, fetch) is None


def test_region_policy_does_not_conflate_portuguese_or_english_regions():
    policy = SubtitlePolicy(allow_generic_english=False)
    assert policy.matches("movie.pt-BR.srt")
    assert policy.matches("movie.en-US.srt")
    assert not policy.matches("movie.pt-PT.srt")
    assert not policy.matches("movie.en.srt")
    assert not policy.matches("movie.en-GB.srt")


def test_credits_margin_changes_timing_eligibility():
    from homeserver_control.worker.subdl import _movie_timing_plausible

    content = "".join(
        f"{number}\n{minute // 60:02}:{minute % 60:02}:00,000 --> "
        f"{minute // 60:02}:{minute % 60:02}:03,000\nText\n\n"
        for number, minute in enumerate(range(0, 111, 10), 1)
    ).encode()
    assert not _movie_timing_plausible(content, 7200, credits_margin_seconds=480)
    assert _movie_timing_plausible(content, 7200, credits_margin_seconds=600)
