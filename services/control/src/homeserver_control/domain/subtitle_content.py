"""Bounded structural validation for an external SRT sidecar."""

from __future__ import annotations

import re

MAX_SRT_BYTES = 1_000_000
_CUE = re.compile(
    r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*"
    r"\d{2}:\d{2}:\d{2}[,.]\d{3}"
)


def valid_srt(data: bytes) -> bool:
    if not 30 <= len(data) <= MAX_SRT_BYTES:
        return False
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    return bool(_CUE.search(text[:16384])) and "\x00" not in text


def normalize_srt(data: bytes) -> bytes | None:
    """Return a structurally valid UTF-8 SRT, including legacy Windows-1252 files."""
    if valid_srt(data):
        return data
    if not 30 <= len(data) <= MAX_SRT_BYTES:
        return None
    try:
        normalized = data.decode("cp1252").encode("utf-8")
    except UnicodeDecodeError:
        return None
    return normalized if valid_srt(normalized) else None
