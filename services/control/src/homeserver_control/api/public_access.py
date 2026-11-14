"""Native-session guard and bounded login mediation for the dedicated public proxy.

Run uvicorn with --factory and --no-proxy-headers: peer trust must use the actual
TCP connection. No admin credentials, controller state or media mounts are used.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import sqlite3
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response

from homeserver_control.api.public_limits import LoginLimited, LoginLimits
from homeserver_control.api.public_policy import (
    IMAGE_COOKIE_NAME,
    PublicDenied,
    anonymous,
    jellyfin_image,
    jellyfin_image_cookie,
    jellyfin_token,
    permitted,
    single_header,
    target,
    token_header,
)


@dataclass(frozen=True)
class PublicAccessSettings:
    jellyfin_url: str = 'http://jellyfin:8096/jellyfin'
    seerr_url: str = 'http://seerr:5055'
    state_path: Path = Path('/state/limits.sqlite3')
    trusted_proxy_ips: tuple[str, ...] = ()
    seerr_origin: str = ''

    @classmethod
    def from_environment(cls) -> PublicAccessSettings:
        return cls(
            jellyfin_url=os.environ.get('HOMESERVER_JELLYFIN_URL', cls.jellyfin_url),
            seerr_url=os.environ.get('HOMESERVER_SEERR_URL', cls.seerr_url),
            state_path=Path(os.environ.get(
                'HOMESERVER_PUBLIC_ACCESS_STATE_PATH', '/state/limits.sqlite3')),
            trusted_proxy_ips=tuple(value.strip() for value in os.environ.get(
                'HOMESERVER_PUBLIC_ACCESS_TRUSTED_PROXY_IPS', '').split(',') if value.strip()),
            seerr_origin=os.environ.get('HOMESERVER_PUBLIC_SEERR_ORIGIN', ''),
        )


def _json_object(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON field')
            result[key] = value
        return result
    try:
        body = json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as error:
        raise PublicDenied(400) from error
    if not isinstance(body, dict):
        raise PublicDenied(400)
    return body


def _cookies(raw: str) -> SimpleCookie:
    if len(raw) > 8192:
        raise PublicDenied()
    parsed = SimpleCookie()
    try:
        parsed.load(raw)
    except CookieError as error:
        raise PublicDenied() from error
    return parsed


def _image_cookie(response: Response, token: str, *, delete: bool = False) -> None:
    # Reuse the native session; this cookie never authorizes other API routes.
    response.set_cookie(IMAGE_COOKIE_NAME, '' if delete else token,
                        max_age=0 if delete else 1800, path='/',
                        secure=True, httponly=True, samesite='strict')


class PublicAccess:
    def __init__(self, settings: PublicAccessSettings, client: httpx.AsyncClient | None,
                 clock: Callable[[], float]) -> None:
        self.settings, self.clock = settings, clock
        self.client = client or httpx.AsyncClient(
            timeout=8, follow_redirects=False, trust_env=False,
            limits=httpx.Limits(max_connections=24, max_keepalive_connections=12))
        self.owns_client = client is None
        self.limits = LoginLimits(settings.state_path)
        self.peers: set[str] = set()
        self.peers_until = 0.0
        self.peer_lock = asyncio.Lock()
        self.static_peers = {
            str(ipaddress.ip_address(value)) for value in settings.trusted_proxy_ips}
        for base in (settings.jellyfin_url, settings.seerr_url):
            url = httpx.URL(base)
            if (url.scheme not in {'http', 'https'} or not url.host or url.userinfo
                    or url.query or url.fragment):
                raise ValueError('Public access requires fixed HTTP upstreams')
        if settings.seerr_origin:
            origin = httpx.URL(settings.seerr_origin)
            if (origin.scheme != 'https' or not origin.host or origin.userinfo
                    or origin.query or origin.fragment or origin.path != '/'):
                raise ValueError('Public Seerr origin must be an HTTPS origin')

    async def trusted_peers(self) -> set[str]:
        if self.static_peers:
            return self.static_peers
        async with self.peer_lock:
            if time.monotonic() < self.peers_until:
                return self.peers
            try:
                rows = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                    'public-proxy', 8080, type=socket.SOCK_STREAM), timeout=2)
                self.peers = {str(ipaddress.ip_address(row[4][0])) for row in rows}
                ttl = 30
            except (OSError, ValueError, TimeoutError):
                self.peers, ttl = set(), 5
            self.peers_until = time.monotonic() + ttl
            return self.peers

    async def context(self, request: Request, service: str) -> tuple[str, str, str, list]:
        try:
            peer = str(ipaddress.ip_address(request.client.host)) if request.client else ''
            if peer not in await self.trusted_peers():
                raise PublicDenied()
            address = str(ipaddress.ip_address(single_header(request.headers, 'x-real-ip')))
        except ValueError as error:
            raise PublicDenied() from error
        method = single_header(request.headers, 'x-original-method')
        if method not in {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}:
            raise PublicDenied()
        path, query = target(single_header(request.headers, 'x-original-uri'), service)
        if service == 'seerr' and method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = single_header(request.headers, 'origin')
            if not self.settings.seerr_origin or origin != self.settings.seerr_origin:
                raise PublicDenied()
        return address, method, path, query

    async def native(
        self, service: str, path: str, *, method: str = 'GET',
        headers: dict | None = None, body: bytes = b'',
    ) -> tuple[int, httpx.Headers, bytes]:
        base = self.settings.jellyfin_url if service == 'jellyfin' else self.settings.seerr_url
        # Explicit Request avoids the shared client's cookie jar carrying identity
        # between users. Paths are selected by code; redirects are never followed.
        outgoing = httpx.Request(method, base.rstrip('/') + path, headers={
            'Accept': 'application/json', 'Accept-Encoding': 'identity', **(headers or {})},
            content=body, extensions={'timeout': {'connect': 3, 'read': 8, 'write': 5, 'pool': 2}})
        response = None
        try:
            async with asyncio.timeout(10):
                response = await self.client.send(outgoing, stream=True, follow_redirects=False)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 256 * 1024:
                        raise PublicDenied(503)
                    chunks.append(chunk)
            if response.status_code >= 500 or 300 <= response.status_code < 400:
                raise PublicDenied(503)
            return response.status_code, response.headers, b''.join(chunks)
        except (httpx.HTTPError, TimeoutError) as error:
            raise PublicDenied(503) from error
        finally:
            if response is not None:
                await response.aclose()

    async def jellyfin_identity(self, token: str) -> str:
        status, _, raw = await self.native('jellyfin', '/Users/Me', headers=token_header(token))
        if status != 200:
            raise PublicDenied(401 if status == 401 else 403)
        user = _json_object(raw)
        policy = user.get('Policy')
        if (not isinstance(user.get('Id'), str) or not user['Id'] or not isinstance(policy, dict)
                or policy.get('IsAdministrator') is not False
                or policy.get('IsDisabled') is not False):
            raise PublicDenied()
        return user['Id']

    async def seerr_identity(self, cookie: str) -> int:
        if not cookie or len(cookie) > 8192:
            raise PublicDenied(401)
        parsed = _cookies(cookie)
        # Native Seerr uses a signed Express session cookie. No API key is sent.
        if 'connect.sid' not in parsed or cookie.lower().count('connect.sid=') != 1:
            raise PublicDenied(401)
        status, _, raw = await self.native('seerr', '/api/v1/auth/me', headers={'Cookie': cookie})
        if status != 200:
            raise PublicDenied(401 if status == 401 else 403)
        user = _json_object(raw)
        permissions = user.get('permissions')
        # Request moderation (16) stays native; global administration remains private.
        privileged = 2 | 4 | 8 | 1048576 | 268435456
        if (type(user.get('id')) is not int or user['id'] <= 1
                or type(permissions) is not int or permissions < 0 or permissions & privileged):
            raise PublicDenied()
        return user['id']

    async def authorize(self, request: Request, service: str) -> Response:
        _, method, path, query = await self.context(request, service)
        if not permitted(service, path, method):
            raise PublicDenied()
        if anonymous(service, path, method):
            return Response(status_code=204)
        explicit_token = None
        if service == 'jellyfin':
            explicit_token = jellyfin_token(request.headers, query)
            image_token = jellyfin_image_cookie(request.headers) if jellyfin_image(
                path, method) else None
            if explicit_token and image_token and explicit_token != image_token:
                raise PublicDenied()
            token = explicit_token or image_token
            if token:
                user_id = await self.jellyfin_identity(token)
                user_path = re.match(r'/users/([^/]+)(?:/|$)', path)
                if user_path and user_path[1] not in {'me', user_id.lower()}:
                    raise PublicDenied()
            else:
                raise PublicDenied(401)
        else:
            if (single_header(request.headers, 'x-api-key')
                    or single_header(request.headers, 'authorization')):
                raise PublicDenied()
            cookie = single_header(request.headers, 'cookie')
            if 'connect.sid' in _cookies(cookie):
                user_id = await self.seerr_identity(cookie)
                user_path = re.match(r'/api/v1/user/(\d+)(?:/|$)', path)
                if user_path and int(user_path[1]) != user_id:
                    raise PublicDenied()
            else:
                raise PublicDenied(401)
        headers = {'X-Verified-Authorization': f'MediaBrowser Token="{token}"'} if (
            service == 'jellyfin' and token) else {}
        response = Response(status_code=204, headers=headers)
        if explicit_token:
            _image_cookie(response, explicit_token,
                          delete=method == 'POST' and path == '/sessions/logout')
        return response

    async def login(self, request: Request, service: str) -> Response:
        address, method, path, query = await self.context(request, service)
        paths = {'/users/authenticatebyname': '/Users/AuthenticateByName'}
        if service == 'seerr':
            paths = {
            '/api/v1/auth/local': '/api/v1/auth/local',
            '/api/v1/auth/jellyfin': '/api/v1/auth/jellyfin',
            }
        if method != 'POST' or path not in paths or query:
            raise PublicDenied()
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 16384:
                raise PublicDenied(413)
            chunks.append(chunk)
        payload = _json_object(b''.join(chunks))
        account_key = 'Username' if service == 'jellyfin' else (
            'email' if path.endswith('/local') else 'username')
        password_key = 'Pw' if service == 'jellyfin' else 'password'
        if service == 'seerr' and path == '/api/v1/auth/jellyfin' and 'email' in payload:
            # Seerr's native form repeats username as email. Existing imported
            # accounts authenticate by username; do not forward an email update.
            email = payload.pop('email')
            if (not isinstance(email, str) or len(email) > 256
                    or email != payload.get('username')):
                raise PublicDenied(400)
        if (set(payload) != {account_key, password_key}
                or not isinstance(payload[account_key], str) or not payload[account_key].strip()
                or len(payload[account_key]) > 256 or not isinstance(payload[password_key], str)
                or len(payload[password_key]) > 4096):
            raise PublicDenied(400)
        headers = {'Content-Type': 'application/json', 'X-Forwarded-For': address,
                   'X-Real-IP': address, 'X-Forwarded-Proto': 'https'}
        if service == 'jellyfin':
            if jellyfin_token(request.headers, query):
                raise PublicDenied()
            auth = single_header(request.headers, 'authorization') or single_header(
                request.headers, 'x-emby-authorization')
            headers['Authorization'] = auth or (
                'MediaBrowser Client="HomeServer", Device="Public login", '
                'DeviceId="public-login", Version="1"')
        else:
            headers['Origin'] = self.settings.seerr_origin
            csrf = _cookies(single_header(request.headers, 'cookie')).get('_csrf')
            if csrf:
                headers['Cookie'] = f'_csrf={csrf.coded_value}'
            for name in ('x-xsrf-token', 'x-csrf-token'):
                value = single_header(request.headers, name)
                if value:
                    headers[name] = value
        identity_service = 'jellyfin' if path == '/api/v1/auth/jellyfin' else service
        lease = await asyncio.to_thread(self.limits.reserve, identity_service, address,
                                        payload[account_key], self.clock())
        failed = False
        try:
            status, response_headers, raw = await self.native(
                service, paths[path], method='POST', headers=headers,
                body=json.dumps(payload, ensure_ascii=True).encode())
            failed = status in {401, 403}
            if status == 200:
                if service == 'jellyfin':
                    token = _json_object(raw).get('AccessToken')
                    if not isinstance(token, str):
                        raise PublicDenied(503)
                    await self.jellyfin_identity(token)
                else:
                    cookies = _cookies('; '.join(response_headers.get_list('set-cookie')))
                    cookie = '; '.join(
                        f'{key}={value.coded_value}' for key, value in cookies.items())
                    await self.seerr_identity(cookie)
            elif status not in {400, 401, 403, 429}:
                raise PublicDenied(503)
            response = Response(raw, status_code=status, headers={
                'Content-Type': response_headers.get('content-type', 'application/json'),
                'Cache-Control': 'no-store',
            })
            if status == 200:
                if service == 'jellyfin':
                    _image_cookie(response, token)
                for value in response_headers.get_list('set-cookie'):
                    if service == 'seerr':
                        attributes = ['Secure', 'SameSite=Lax']
                        if value.startswith('connect.sid='):
                            attributes.append('HttpOnly')
                        for attribute in attributes:
                            if attribute.split('=')[0].lower() not in value.lower():
                                value += '; ' + attribute
                    response.headers.append('set-cookie', value)
            if status == 429:
                response.headers['Retry-After'] = '60'
            return response
        finally:
            await asyncio.to_thread(self.limits.finish, lease, failed, self.clock())


def create_app(settings: PublicAccessSettings | None = None, *,
               client: httpx.AsyncClient | None = None,
               clock: Callable[[], float] = time.time) -> FastAPI:
    guard = PublicAccess(settings or PublicAccessSettings.from_environment(), client, clock)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if guard.owns_client:
            await guard.client.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(PublicDenied)
    async def denied(_request, error):
        return Response(status_code=error.status, headers={'Cache-Control': 'no-store'})

    @app.exception_handler(LoginLimited)
    async def limited(_request, error):
        return Response(status_code=429, headers={'Retry-After': str(error.retry_after),
                                                 'Cache-Control': 'no-store'})

    @app.exception_handler(sqlite3.Error)
    async def unavailable(_request, _error):
        return Response(status_code=503, headers={'Cache-Control': 'no-store'})

    @app.get('/authorize/{service}')
    async def authorize(service: str, request: Request):
        if service not in {'jellyfin', 'seerr'}:
            raise PublicDenied()
        return await guard.authorize(request, service)

    @app.post('/login/{service}')
    async def login(service: str, request: Request):
        if service not in {'jellyfin', 'seerr'}:
            raise PublicDenied()
        return await guard.login(request, service)

    return app
