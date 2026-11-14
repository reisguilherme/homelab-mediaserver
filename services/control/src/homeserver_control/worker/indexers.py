"""Canonical identities from exact Arr indexer labels."""

from __future__ import annotations

import re
from collections.abc import Mapping

_ALIASES = {
    "uindex": "uindex", "uindex.org": "uindex", "www.uindex.org": "uindex",
    "1337x": "1337x", "1337x.to": "1337x", "www.1337x.to": "1337x",
    "amigos share club": "amigosshare", "amigosshare": "amigosshare",
    "amigos-share.club": "amigosshare", "asc": "amigosshare",
}


def release_indexer(release: Mapping[str, object]) -> str | None:
    """Trust the Arr label, never a torrent title claiming another source."""
    name = release.get("indexer")
    if not isinstance(name, str):
        return None
    name = re.sub(r"\s*\(prowlarr\)\s*$", "", name.strip(), flags=re.I).casefold()
    return _ALIASES.get(name)
