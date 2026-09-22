from __future__ import annotations

import hmac


def valid_csrf_token(presented: str | None, expected: str) -> bool:
    if presented is None or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
