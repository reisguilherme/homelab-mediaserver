from __future__ import annotations

import httpx


class UpstreamError(RuntimeError):
    """A bounded upstream request failed without an accepted side effect."""


class CredentialError(UpstreamError):
    """Upstream rejected the configured credential; retrying is unsafe."""


class RateLimitError(UpstreamError):
    """Upstream requested backoff."""

    def __init__(self, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ContractError(UpstreamError):
    """Response shape or endpoint contract is incompatible."""


class EffectUncertain(UpstreamError):
    """A mutation may have been accepted before the response was lost."""


def endpoint(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    json_body: object | None = None,
    params: dict[str, object] | None = None,
    uncertain_on_timeout: bool = False,
) -> object:
    try:
        response = await client.request(method, url, headers=headers, json=json_body, params=params)
    except httpx.TimeoutException as error:
        if uncertain_on_timeout:
            raise EffectUncertain("upstream mutation timed out after dispatch") from error
        raise UpstreamError("upstream request timed out") from error
    except httpx.HTTPError as error:
        if uncertain_on_timeout:
            raise EffectUncertain("upstream mutation failed after dispatch") from error
        raise UpstreamError("upstream request failed") from error

    if response.status_code in (401, 403):
        raise CredentialError("upstream credential rejected")
    if response.status_code == 429:
        raise RateLimitError("upstream rate limit", response.headers.get("retry-after"))
    if response.status_code >= 400:
        raise UpstreamError(f"upstream returned HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as error:
        raise ContractError("upstream response is not JSON") from error
