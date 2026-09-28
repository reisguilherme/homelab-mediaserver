"""Conservative Brazilian Portuguese labels for external subtitle files."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath

from homeserver_control.domain.media_probe import MediaProbe

LANGUAGES = {"pt-BR": ("BR_PT", "pt-BR"), "en-US": ("EN", "en-US")}


@dataclass(frozen=True)
class SubtitlePolicy:
    languages: tuple[str, ...] = ("pt-BR", "en-US")
    match_modes: tuple[str, ...] = ("release", "compatible_edition")
    skip_original_audio_languages: tuple[str, ...] = ("pt-BR",)
    allow_generic_english: bool = True
    credits_margin_seconds: float = 600
    providers: tuple[str, ...] = ("bazarr", "subdl")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]):
        def ordered(name, default):
            return tuple(
                value.strip()
                for value in environment.get("HOMESERVER_" + name, ",".join(default)).split(",")
                if value.strip()
            )

        languages = ordered("SUBTITLE_LANGUAGES", ("pt-BR", "en-US"))
        skip = ordered("SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES", ("pt-BR",))
        if not languages or any(language not in LANGUAGES for language in languages):
            raise ValueError("HOMESERVER_SUBTITLE_LANGUAGES: unsupported language")
        if any(language != "pt-BR" for language in skip):
            raise ValueError(
                "HOMESERVER_SUBTITLE_SKIP_ORIGINAL_AUDIO_LANGUAGES: unsupported detector"
            )
        modes = ordered("SUBTITLE_MATCH_MODES", ("release", "compatible_edition"))
        if not modes or any(mode not in ("release", "compatible_edition") for mode in modes):
            raise ValueError("HOMESERVER_SUBTITLE_MATCH_MODES: unsupported mode")
        return cls(
            languages,
            modes,
            skip,
            environment.get("HOMESERVER_SUBTITLE_ALLOW_GENERIC_ENGLISH", "true") == "true",
            float(environment.get("HOMESERVER_SUBTITLE_CREDITS_MARGIN_SECONDS", "600")),
            ordered("SUBTITLE_PROVIDERS", ("bazarr", "subdl")),
        )

    @property
    def attempts(self):
        modes = {"release": "exact", "compatible_edition": "same_duration"}
        return tuple(
            (LANGUAGES[language][0], modes[mode])
            for language in self.languages
            for mode in self.match_modes
        )

    def matches(self, path: str):
        if "pt-BR" in self.languages and is_brazilian_portuguese_subtitle(path):
            return True
        if "en-US" not in self.languages or not is_english_subtitle(path):
            return False
        tokens = _tokens(PurePosixPath(path).stem)
        while tokens and tokens[-1] in {"cc", "default", "forced", "hi", "sdh"}:
            tokens.pop()
        if len(tokens) >= 2 and tokens[-1] in {"au", "ca", "gb", "uk", "us"}:
            return tokens[-1] == "us"
        return self.allow_generic_english

    def select_sidecars(self, files: Sequence[str]) -> tuple[str, ...]:
        return tuple(path for path in files if self.matches(path))

    def waives_subtitles(self, probe: MediaProbe) -> bool:
        # en-US has no reliable original-region detector yet; do not infer it.
        return "pt-BR" in self.skip_original_audio_languages and audio_is_brazilian_portuguese(
            probe
        )


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
        language = (
            next(
                (
                    value
                    for key, value in tags.items()
                    if str(key).lower() == "language" and isinstance(value, str)
                ),
                "",
            )
            .strip()
            .lower()
            .replace("_", "-")
        )
        if language not in {"en", "eng", "english", "en-us", "en-gb", "en-uk", "en-au", "en-ca"}:
            continue
        title = " ".join(
            value
            for key, value in tags.items()
            if str(key).lower() in {"title", "handler_name"} and isinstance(value, str)
        )
        if set(_tokens(title)) & {
            "forced",
            "sign",
            "signs",
            "song",
            "songs",
            "foreign",
            "partial",
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
    audio = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if not audio:
        return False

    def title(stream: dict[str, object]) -> str:
        tags = stream.get("tags")
        if not isinstance(tags, dict):
            return ""
        return " ".join(
            str(value)
            for key, value in tags.items()
            if str(key).lower() in {"title", "handler_name"} and isinstance(value, str)
        )

    originals = [
        stream
        for stream in audio
        if (
            isinstance(stream.get("disposition"), dict)
            and stream["disposition"].get("original") == 1
        )
        or "original" in _tokens(title(stream))
    ]
    if len(originals) != 1:
        return False
    candidate = originals[0]
    candidate_title = title(candidate)
    if any(
        token in {"dub", "dubbed", "dublado", "dublada", "dublagem", "commentary"}
        for token in _tokens(candidate_title)
    ):
        return False
    tags = candidate.get("tags")
    if not isinstance(tags, dict):
        return False
    language = next(
        (
            value
            for key, value in tags.items()
            if str(key).lower() == "language" and isinstance(value, str)
        ),
        "",
    )
    return _explicit_ptbr(language) or (
        _tokens(language) in (["pt"], ["por"], ["portuguese"], ["portugues"])
        and _explicit_ptbr(candidate_title)
    )
