from __future__ import annotations

import hmac


def token_matches(presented: str | None, expected: str) -> bool:
    """Compare service credentials without leaking length/content timing."""

    if presented is None or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
