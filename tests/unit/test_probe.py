import shutil
import sys
from pathlib import Path

import pytest

from homeserver_control.domain.media_probe import MediaProbeError, probe_media


def _fixture_root() -> Path:
    path = Path(".runtime") / "probe-fixtures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_probe_validates_video_audio_and_subtitle_streams() -> None:
    root = _fixture_root()
    binary = root / "ffprobe-valid.py"
    binary.write_text(
        (
            'import json; print(json.dumps({"streams":[{"codec_type":"video",'
            '"width":3840,"height":2160},{"codec_type":"audio",'
            '"tags":{"language":"eng"}},{"codec_type":"subtitle",'
            '"tags":{"language":"por"}}]}))'
        ),
        encoding="utf-8",
    )
    media = root / "movie.mkv"
    media.write_bytes(b"fixture")
    try:
        result = probe_media(media, ffprobe_binary=[sys.executable, str(binary)])
        assert result.width == 3840
        assert result.height == 2160
        assert result.audio_languages == ("eng",)
        assert result.subtitle_languages == ("por",)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_probe_rejects_invalid_ffprobe_output() -> None:
    root = _fixture_root()
    binary = root / "ffprobe-invalid.py"
    binary.write_text("print('not-json')", encoding="utf-8")
    media = root / "bad.mkv"
    media.write_bytes(b"fixture")
    try:
        with pytest.raises(MediaProbeError):
            probe_media(media, ffprobe_binary=[sys.executable, str(binary)])
    finally:
        shutil.rmtree(root, ignore_errors=True)
