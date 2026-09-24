"""Safe, bounded media metadata probing.

The probe is deliberately a small adapter around ``ffprobe``.  It never
passes a media path through a shell, validates the JSON shape, and exposes
only the metadata needed by admission and deletion workflows.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class MediaProbeError(ValueError):
    """Raised when media metadata cannot be obtained or is unsafe to use."""


@dataclass(frozen=True)
class MediaProbe:
    width: int
    height: int
    audio_languages: tuple[str, ...]
    subtitle_languages: tuple[str, ...]
    raw: dict[str, object]
    duration_seconds: float | None = None


def _command(binary: str | Sequence[str]) -> list[str]:
    if isinstance(binary, str):
        return [binary]
    parts = [str(part) for part in binary]
    if not parts:
        raise MediaProbeError("ffprobe command is empty")
    return parts


def _language(stream: dict[str, object]) -> str:
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return "und"
    language = tags.get("language") or tags.get("LANGUAGE")
    if not isinstance(language, str) or not language.strip():
        return "und"
    return language.strip().lower()


def probe_media(
    media_path: str | Path,
    *,
    ffprobe_binary: str | Sequence[str] = "ffprobe",
    timeout_seconds: float = 15.0,
) -> MediaProbe:
    """Return validated video dimensions, stream languages, and duration for *media_path*.

    ``ffprobe_binary`` may be a program name/path or an argv prefix, which is
    useful for hermetic tests and wrappers.  The media path remains one argv
    element in either case.
    """

    path = Path(media_path)
    if not path.is_file():
        raise MediaProbeError(f"media file does not exist: {path}")
    if timeout_seconds <= 0:
        raise MediaProbeError("ffprobe timeout must be positive")

    command = _command(ffprobe_binary)
    command.extend(
        [
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaProbeError(f"ffprobe failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip()[:300]
        raise MediaProbeError(f"ffprobe returned {completed.returncode}: {detail}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MediaProbeError("ffprobe output is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        raise MediaProbeError("ffprobe output has no streams list")

    streams = [stream for stream in payload["streams"] if isinstance(stream, dict)]
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    if not video_streams:
        raise MediaProbeError("ffprobe output has no video stream")
    video = video_streams[0]
    width = video.get("width")
    height = video.get("height")
    if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
        raise MediaProbeError("ffprobe video dimensions are invalid")

    audio_languages = tuple(
        _language(stream) for stream in streams if stream.get("codec_type") == "audio"
    )
    subtitle_languages = tuple(
        _language(stream) for stream in streams if stream.get("codec_type") == "subtitle"
    )
    duration_seconds = None
    format_metadata = payload.get("format")
    if isinstance(format_metadata, dict):
        raw_duration = format_metadata.get("duration")
        if isinstance(raw_duration, (str, int, float)) and not isinstance(raw_duration, bool):
            try:
                parsed_duration = float(raw_duration)
            except (ValueError, OverflowError):
                pass
            else:
                if math.isfinite(parsed_duration) and parsed_duration > 0:
                    duration_seconds = parsed_duration

    return MediaProbe(
        width=width,
        height=height,
        audio_languages=audio_languages,
        subtitle_languages=subtitle_languages,
        raw=payload,
        duration_seconds=duration_seconds,
    )
