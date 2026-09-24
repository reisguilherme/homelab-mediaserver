"""Conservative Brazilian Portuguese labels for external subtitle files."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

from homeserver_control.domain.media_probe import MediaProbe


def _tokens(value: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return [part for part in re.split(r"[^a-z0-9]+", plain) if part]


def is_brazilian_portuguese_subtitle(path: str) -> bool:
    stem = PurePosixPath(path).stem
    tokens = _tokens(stem)
    if not tokens or "portugal" in tokens or "european" in tokens:
        return False
    if any(a == "pt" and b == "pt" for a, b in zip(tokens, tokens[1:], strict=False)):
        return False
    if any(part in {"ptbr", "pob", "brazilian", "brasileiro", "brasileira"} for part in tokens):
        return True
    return any(
        a in {"pt", "por", "portuguese", "portugues"} and b in {"br", "brazil", "brasil"}
        for a, b in zip(tokens, tokens[1:], strict=False)
    )


def is_english_subtitle(path: str) -> bool:
    """Require an English suffix marker, not an English word in the show title."""
    if is_brazilian_portuguese_subtitle(path):
        return False
    tokens = _tokens(PurePosixPath(path).stem)
    while tokens and tokens[-1] in {"cc", "default", "forced", "hi", "sdh"}:
        tokens.pop()
    if len(tokens) >= 2 and tokens[-1] in {"au", "ca", "gb", "uk", "us"}:
        return tokens[-2] in {"en", "eng"}
    return bool(tokens) and tokens[-1] in {"en", "eng", "english"}


def has_embedded_english_subtitle(probe: MediaProbe) -> bool:
    """Accept only a clearly labeled, full text track in the media itself."""
    streams = probe.raw.get("streams")
    if not isinstance(streams, list):
        return False
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "subtitle":
            continue
        if stream.get("codec_name") not in {"subrip", "ass", "ssa", "webvtt", "mov_text"}:
            continue
        disposition = stream.get("disposition")
        if isinstance(disposition, dict) and disposition.get("forced") == 1:
            continue
        tags = stream.get("tags")
        if not isinstance(tags, dict):
            continue
        language = next(
            (value for key, value in tags.items()
             if str(key).lower() == "language" and isinstance(value, str)),
            "",
        ).strip().lower().replace("_", "-")
        if language not in {
            "en", "eng", "english", "en-us", "en-gb", "en-uk", "en-au", "en-ca"
        }:
            continue
        title = " ".join(
            value for key, value in tags.items()
            if str(key).lower() in {"title", "handler_name"} and isinstance(value, str)
        )
        if set(_tokens(title)) & {
            "forced", "sign", "signs", "song", "songs", "foreign", "partial",
            "commentary",
        }:
            continue
        return True
    return False


def _explicit_ptbr(value: str) -> bool:
    tokens = _tokens(value)
    if any(token in {"pob", "ptbr"} for token in tokens):
        return True
    return any(
        a in {"pt", "por", "portuguese", "portugues"}
        and b in {"br", "brazil", "brasil", "brazilian", "brasileiro", "brasileira"}
        or a in {"br", "brazil", "brasil", "brazilian", "brasileiro", "brasileira"}
        and b in {"pt", "por", "portuguese", "portugues"}
        for a, b in zip(tokens, tokens[1:], strict=False)
    )


def audio_is_brazilian_portuguese(probe: MediaProbe) -> bool:
    """Waive subtitles only when ffprobe explicitly labels the original audio pt-BR.

    Neither the first-stream position nor generic ``por`` establishes an original
    Brazilian track. Dubbed/commentary tracks never qualify.
    """
    streams = probe.raw.get("streams")
    if not isinstance(streams, list):
        return False
    audio = [stream for stream in streams if isinstance(stream, dict)
             and stream.get("codec_type") == "audio"]
    if not audio:
        return False

    def title(stream: dict[str, object]) -> str:
        tags = stream.get("tags")
        if not isinstance(tags, dict):
            return ""
        return " ".join(str(value) for key, value in tags.items()
                        if str(key).lower() in {"title", "handler_name"}
                        and isinstance(value, str))

    originals = [stream for stream in audio
                 if (isinstance(stream.get("disposition"), dict)
                     and stream["disposition"].get("original") == 1)
                 or "original" in _tokens(title(stream))]
    if len(originals) != 1:
        return False
    candidate = originals[0]
    candidate_title = title(candidate)
    if any(token in {"dub", "dubbed", "dublado", "dublada", "dublagem", "commentary"}
           for token in _tokens(candidate_title)):
        return False
    tags = candidate.get("tags")
    if not isinstance(tags, dict):
        return False
    language = next((value for key, value in tags.items()
                     if str(key).lower() == "language" and isinstance(value, str)), "")
    return _explicit_ptbr(language) or (
        _tokens(language) in (["pt"], ["por"], ["portuguese"], ["portugues"])
        and _explicit_ptbr(candidate_title)
    )
