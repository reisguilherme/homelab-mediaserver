import pytest

from homeserver_common.env import load_settings
from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.worker.subtitle_language import SubtitlePolicy


def _brazilian_original():
    return MediaProbe(
        width=1920,
        height=1080,
        audio_languages=("pt-br",),
        subtitle_languages=(),
        raw={
            "streams": [
                {
                    "codec_type": "audio",
                    "disposition": {"original": 1},
                    "tags": {"language": "pt-BR"},
                }
            ]
        },
    )


def test_empty_skip_configuration_requires_subtitles_even_for_brazilian_original(tmp_path):
    path = tmp_path / ".env"
    path.write_text('HOMESERVER_SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES=""\n')
    settings = load_settings(path, mode="dev")
    assert settings.subtitle_skip_original_audio_languages == ()
    policy = SubtitlePolicy.from_environment(settings.as_environment())
    assert not policy.waives_subtitles(_brazilian_original())


def test_default_skip_configuration_waives_verified_brazilian_original(tmp_path):
    path = tmp_path / ".env"
    path.write_text("")
    policy = SubtitlePolicy.from_environment(load_settings(path, mode="dev").as_environment())
    assert policy.waives_subtitles(_brazilian_original())


def test_loader_rejects_unsupported_original_english_detector(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES=en-US\n")
    with pytest.raises(ValueError, match="SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES"):
        load_settings(path, mode="dev")


def test_direct_policy_rejects_unsupported_original_english_detector():
    with pytest.raises(ValueError, match="SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES"):
        SubtitlePolicy.from_environment(
            {"HOMESERVER_SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES": "en-US"}
        )
