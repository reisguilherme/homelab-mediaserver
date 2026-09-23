from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteRule:
    method: str
    path: str
    mutation: bool = False


class GatewayAllowlist:
    """Exact method/path allowlist; unknown upstream APIs are never proxied."""

    def __init__(self, rules: tuple[RouteRule, ...] | None = None) -> None:
        self._rules = rules or (
            RouteRule("POST", "/api/v2/auth/login"),
            RouteRule("GET", "/api/v2/app/version"),
            RouteRule("GET", "/api/v2/app/webapiVersion"),
            RouteRule("GET", "/api/v2/app/preferences"),
            RouteRule("GET", "/api/v2/torrents/categories"),
            RouteRule("GET", "/api/v2/torrents/info"),
            RouteRule("GET", "/api/v2/torrents/properties"),
            RouteRule("GET", "/api/v2/torrents/files"),
            RouteRule("POST", "/api/v2/torrents/add", mutation=True),
        )

    def permits(self, method: str, path: str) -> bool:
        return any(rule.method == method.upper() and rule.path == path for rule in self._rules)
