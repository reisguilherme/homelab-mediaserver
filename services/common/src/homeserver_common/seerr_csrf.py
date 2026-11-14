"""Native Seerr CSRF handshake for internal API clients.

Seerr protects API-key mutations too. Its browser cookies are Secure even when
the fixed container hop is HTTP, so echo the paired secret explicitly on one
retry after the native middleware rejects a request before dispatching it.
"""

import re
from http.cookies import SimpleCookie
from urllib.parse import unquote

import httpx

_COOKIE_VALUE = re.compile(r'[A-Za-z0-9._~%+/-]+\Z')


class SeerrClient:
    def __init__(self, client, *, base_url=None):
        self._client = client
        self._seerr_url = httpx.URL(base_url or client.base_url)

    def __getattr__(self, name):
        return getattr(self._client, name)

    async def request(self, method, url, **kwargs):
        method = method.upper()
        target = httpx.URL(url)
        if not target.is_absolute_url:
            target = self._client.base_url.join(target)
        def origin(value):
            return value.scheme, value.host, value.port
        scoped = (origin(target) == origin(self._seerr_url)
                  and target.path.startswith(self._seerr_url.path.rstrip('/') + '/api/v1/'))
        if not scoped or method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return await self._client.request(method, url, **kwargs)
        kwargs['follow_redirects'] = False
        response = await self._client.request(method, url, **kwargs)
        if response.status_code != 403:
            return response
        try:
            rejected = response.json().get('message') == 'invalid csrf token'
        except (ValueError, AttributeError):
            rejected = False
        if not rejected:
            return response
        public = str(self._seerr_url).rstrip('/') + '/api/v1/settings/public'
        bootstrap = await self._client.get(public, follow_redirects=False)
        if bootstrap.status_code != 200:
            return response
        try:
            secret = bootstrap.cookies.get('_csrf')
            token = bootstrap.cookies.get('XSRF-TOKEN')
        except httpx.CookieConflict:
            return response
        if not all(isinstance(value, str) and _COOKIE_VALUE.fullmatch(value)
                   for value in (secret, token)):
            return response
        decoded = unquote(token)
        if not re.fullmatch(r'[A-Za-z0-9._~+/-]+', decoded):
            return response
        cookies = SimpleCookie()
        cookies.load(response.request.headers.get('cookie', ''))
        cookies['_csrf'] = secret
        headers = httpx.Headers(kwargs.get('headers', {}))
        headers['Cookie'] = '; '.join(morsel.OutputString() for morsel in cookies.values())
        headers['X-CSRF-Token'] = decoded
        await response.aclose()
        return await self._client.request(method, url, **(kwargs | {'headers': headers}))

    async def get(self, url, **kwargs):
        return await self.request('GET', url, **kwargs)

    async def post(self, url, **kwargs):
        return await self.request('POST', url, **kwargs)

    async def put(self, url, **kwargs):
        return await self.request('PUT', url, **kwargs)

    async def patch(self, url, **kwargs):
        return await self.request('PATCH', url, **kwargs)

    async def delete(self, url, **kwargs):
        return await self.request('DELETE', url, **kwargs)
