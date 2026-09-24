from __future__ import annotations

import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.worker.subtitle_language import (
    audio_is_brazilian_portuguese,
    has_embedded_english_subtitle,
    is_english_subtitle,
)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Movie.en.srt", True),
        ("Movie.ENG.srt", True),
        ("Movie.English.srt", True),
        ("Movie.en-US.srt", True),
        ("Movie.en.SDH.srt", True),
        ("Movie.pt-BR.srt", False),
        ("Movie.en.pt-BR.srt", False),
        ("Movie.enforced.srt", False),
        ("The.English.S01E01.srt", False),
    ],
)
def test_english_subtitle_requires_explicit_filename_language(filename, expected):
    assert is_english_subtitle(filename) is expected


@pytest.mark.parametrize(
    ("audio", "expected"),
    [
        ([{"codec_type": "audio", "tags": {"language": "pt-BR"}}], False),
        ([{"codec_type": "audio", "tags": {"language": "pob"}}], False),
        ([{"codec_type": "audio", "disposition": {"original": 1},
          "tags": {"language": "pt-BR"}}], True),
        ([{"codec_type": "audio", "disposition": {"original": 1},
          "tags": {"language": "pob"}}], True),
        ([{"codec_type": "audio", "tags": {
            "language": "por", "title": "Português Brasileiro (Original)"
        }}], True),
        ([{"codec_type": "audio", "tags": {"language": "por"}}], False),
        ([{"codec_type": "audio", "tags": {
            "language": "pt-BR", "title": "Dublado"
        }}], False),
        ([{"codec_type": "audio", "tags": {"language": "pt-BR"}},
          {"codec_type": "audio", "tags": {"language": "eng", "title": "Original"}}], False),
        ([{"codec_type": "audio", "tags": {"language": "pt-BR"}},
          {"codec_type": "audio", "disposition": {"original": 1},
           "tags": {"language": "eng"}}], False),
        ([{"codec_type": "audio", "disposition": {"original": 1},
           "tags": {"language": "pt-BR"}},
          {"codec_type": "audio", "disposition": {"original": 1},
           "tags": {"language": "eng"}}], False),
        ([{"codec_type": "audio", "tags": {"language": "eng"}},
          {"codec_type": "audio", "tags": {"language": "pt-BR"}}], False),
    ],
)
def test_ptbr_audio_exemption_requires_original_track_evidence(audio, expected):
    probe = MediaProbe(
        width=1920, height=1080,
        audio_languages=tuple(
            str(stream.get("tags", {}).get("language", "und")).lower()
            for stream in audio
        ),
        subtitle_languages=(),
        raw={"streams": [{"codec_type": "video", "width": 1920, "height": 1080}, *audio]},
    )
    assert audio_is_brazilian_portuguese(probe) is expected


@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ({"codec_type": "subtitle", "codec_name": "subrip",
          "tags": {"language": "eng"}, "disposition": {"forced": 0}}, True),
        ({"codec_type": "subtitle", "codec_name": "ass",
          "tags": {"language": "en", "title": "SDH"}}, True),
        ({"codec_type": "subtitle", "codec_name": "subrip",
          "tags": {"language": "eng"}, "disposition": {"forced": 1}}, False),
        ({"codec_type": "subtitle", "codec_name": "subrip",
          "tags": {"language": "eng", "title": "Forced"}}, False),
        ({"codec_type": "subtitle", "codec_name": "subrip",
          "tags": {"language": "eng", "title": "Signs & Songs"}}, False),
        ({"codec_type": "subtitle", "codec_name": "subrip",
          "tags": {"language": "eng", "title": "Foreign Parts"}}, False),
        ({"codec_type": "subtitle", "codec_name": "subrip",
          "tags": {"language": "por"}}, False),
        ({"codec_type": "subtitle", "codec_name": "subrip",
          "tags": {"language": "und", "title": "English"}}, False),
        ({"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle",
          "tags": {"language": "eng"}}, False),
    ],
)
def test_embedded_english_fallback_requires_usable_full_text_track(stream, expected):
    probe = MediaProbe(
        width=1920, height=1080, audio_languages=("eng",),
        subtitle_languages=(str(stream.get("tags", {}).get("language", "und")),),
        raw={"streams": [{"codec_type": "video", "width": 1920, "height": 1080}, stream]},
    )
    assert has_embedded_english_subtitle(probe) is expected
