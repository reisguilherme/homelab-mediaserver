"""Accept only single-hash v1 magnets for pre-inspected torrent metadata."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit


def magnet_infohash(value: object) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= 16_384:
        return None
    if any(char in value for char in "\r\n\x00"):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "magnet" or parsed.netloc or parsed.path or parsed.fragment:
            return None
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=80)
    except ValueError:
        return None
    if not pairs or any(key not in {"xt", "dn", "tr"} for key, _ in pairs):
        return None
    xt = [item for key, item in pairs if key == "xt"]
    if len(xt) != 1 or sum(key == "dn" for key, _ in pairs) > 1:
        return None
    match = re.fullmatch(r"urn:btih:([0-9a-f]{40})", xt[0], flags=re.IGNORECASE)
    return match.group(1).lower() if match else None
