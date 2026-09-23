"""Conservative Brazilian Portuguese labels for external subtitle files."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath


def is_brazilian_portuguese_subtitle(path: str) -> bool:
    stem = PurePosixPath(path).stem
    plain = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode().lower()
    tokens = [part for part in re.split(r"[^a-z0-9]+", plain) if part]
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
