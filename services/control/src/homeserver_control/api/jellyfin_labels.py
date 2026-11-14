"""A narrow native API/notification relay that changes subtitle presentation only."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
import zlib
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from homeserver_control.domain.jellyfin_labels import normalize_subtitle_labels

# No media, image, subtitle-delivery or mutation routes are registered here.
GET_ROUTES = (
    '/Items', '/Items/{item_id}', '/Items/Root', '/Items/Latest',
    '/Items/{item_id}/Intros', '/Items/{item_id}/LocalTrailers',
    '/Items/{item_id}/SpecialFeatures', '/Items/{item_id}/PlaybackInfo',
    '/Users/{user_id}/Items', '/Users/{user_id}/Items/{item_id}',
    '/Users/{user_id}/Items/Root', '/Users/{user_id}/Items/Latest',
    '/Users/{user_id}/Items/Resume', '/Users/{user_id}/Items/{item_id}/Intros',
    '/Users/{user_id}/Items/{item_id}/LocalTrailers',
    '/Users/{user_id}/Items/{item_id}/SpecialFeatures', '/UserItems/Resume',
    '/Shows/NextUp', '/Shows/Upcoming', '/Shows/{series_id}/Episodes',
    '/Shows/{series_id}/Seasons', '/Videos/{item_id}/AdditionalParts', '/Sessions',
    '/LiveTv/Channels', '/LiveTv/Channels/{channel_id}',
    '/LiveTv/Recordings', '/LiveTv/Recordings/{recording_id}',
    '/LiveTv/Recordings/Series', '/LiveTv/Recordings/Groups', '/LiveTv/Recordings/Folders',
    '/LiveTv/Programs', '/LiveTv/Programs/{program_id}', '/LiveTv/Programs/Recommended',
    '/Channels/{channel_id}/Items',
)
POST_ROUTES = ('/Items/{item_id}/PlaybackInfo', '/LiveStreams/Open', '/LiveTv/Programs')
_HOP_HEADERS = {
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te',
    'trailer', 'transfer-encoding', 'upgrade',
}
_CHANGED_HEADERS = {'content-length', 'content-encoding', 'etag', 'content-md5', 'digest'}
_TRUSTED_PROXY_HOSTS = ('jellyfin-proxy', 'public-proxy')
_SOCKET_LOGGER = logging.Logger('homeserver.jellyfin_label_socket', level=logging.CRITICAL + 1)
_SOCKET_LOGGER.addHandler(logging.NullHandler())
_SOCKET_LOGGER.propagate = False
_SECRET_QUERY = re.compile(
    r'([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|authorization|'
    r'x[-_]emby[-_]token)=)[^&\s"\']*', re.IGNORECASE,
)


class _SecretQueryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = _SECRET_QUERY.sub(r'\1[redacted]', rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = ()
        return True


def _headers(headers: Any, *, remove: set[str] | None = None) -> list[tuple[str, str]]:
    blocked = _HOP_HEADERS | (remove or set())
    blocked |= {part.strip().lower() for part in headers.get('connection', '').split(',')}
    items = (headers.multi_items() if hasattr(headers, 'multi_items') else
             headers.raw_items() if hasattr(headers, 'raw_items') else headers.items())
    return [(key, value) for key, value in items if key.lower() not in blocked]


def _set_response_headers(response: Response, headers: list[tuple[str, str]]) -> Response:
    # Preserve repeated end-to-end headers, including Set-Cookie.
    response.raw_headers = [(key.lower().encode('latin-1'), value.encode('latin-1'))
                            for key, value in headers]
    return response


class JellyfinLabelProxy:
    def __init__(
        self, base_url: str, *, client: httpx.AsyncClient | None = None,
        enabled: bool = True, english_us_label: str = 'English - US',
        portuguese_br_label: str = 'Português BR', max_response_bytes: int = 8 * 1024 * 1024,
        socket_connect: Callable[..., Any] | None = None,
    ) -> None:
        self.base = httpx.URL(base_url.rstrip('/'))
        if (self.base.scheme not in {'http', 'https'} or not self.base.host
                or self.base.userinfo or self.base.query or self.base.fragment):
            raise ValueError('Jellyfin label proxy requires a fixed HTTP upstream')
        if max_response_bytes <= 0:
            raise ValueError('Jellyfin label proxy response limit must be positive')
        self.enabled = enabled
        self.english_us_label = english_us_label
        self.portuguese_br_label = portuguese_br_label
        self.max_response_bytes = max_response_bytes
        self.socket_connect = socket_connect
        self._proxy_peers: set[str] = set()
        self._proxy_peers_until = 0.0
        self._proxy_dns_lock = asyncio.Lock()
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=30, trust_env=False,
                                                 follow_redirects=False)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _trusted_proxy_peers(self) -> set[str]:
        # Nginx depends on this service at boot, so DNS failure must be retried.
        async with self._proxy_dns_lock:
            if time.monotonic() >= self._proxy_peers_until:
                resolved = await asyncio.gather(*(
                    asyncio.wait_for(
                        asyncio.get_running_loop().getaddrinfo(
                            host, 8096, type=socket.SOCK_STREAM), timeout=2,
                    )
                    for host in _TRUSTED_PROXY_HOSTS
                ), return_exceptions=True)
                peers: set[str] = set()
                incomplete = False
                for addresses in resolved:
                    if isinstance(addresses, BaseException):
                        incomplete = True
                        continue
                    try:
                        peers.update(str(ipaddress.ip_address(item[4][0])) for item in addresses)
                    except ValueError:
                        incomplete = True
                self._proxy_peers = peers
                ttl = 5 if incomplete or not peers else 30
                self._proxy_peers_until = time.monotonic() + ttl
            return self._proxy_peers

    async def _forward_headers(self, connection: Any, *, remove: set[str]) -> list[tuple[str, str]]:
        headers = [(key, value) for key, value in _headers(connection.headers, remove=remove)
                   if key.lower() not in {'forwarded', 'x-real-ip', 'x-admin-token',
                                          'x-csrf-token', 'x-arr-token'}
                   and not key.lower().startswith('x-forwarded-')]
        peer = (connection.scope.get('client') or ('', 0))[0]
        scheme = 'https' if connection.scope.get('scheme') in {'https', 'wss'} else 'http'
        address = peer
        forwarded = connection.headers.getlist('x-forwarded-for')
        protocols = connection.headers.getlist('x-forwarded-proto')
        if (peer in await self._trusted_proxy_peers() and len(forwarded) == 1
                and len(protocols) == 1 and protocols[0] in {'http', 'https'}):
            try:
                address = str(ipaddress.ip_address(forwarded[0]))
                scheme = protocols[0]
            except ValueError:
                pass
        if address:
            headers += [('x-forwarded-for', address), ('x-real-ip', address)]
        headers.append(('x-forwarded-proto', scheme))
        if host := connection.headers.get('host'):
            headers.append(('x-forwarded-host', host))
        return headers

    def _url(self, scope: dict[str, Any], *, socket: bool = False) -> httpx.URL:
        raw = scope.get('raw_path', scope['path'].encode('utf-8'))
        # Nginx accepts legacy paths and the native /jellyfin BaseUrl.
        if raw.lower() == b'/jellyfin' or raw.lower().startswith(b'/jellyfin/'):
            raw = raw[len(b'/jellyfin'):]
        path = self.base.raw_path.rstrip(b'/') + raw
        query = scope.get('query_string', b'')
        url = self.base.copy_with(raw_path=path + (b'?' + query if query else b''))
        if socket:
            url = url.copy_with(scheme='wss' if self.base.scheme == 'https' else 'ws')
        return url

    def _normalized_json(self, content: bytes, encoding: str = '') -> bytes | None:
        decoded = content
        if encoding.lower() not in ('', 'identity'):
            if encoding.lower() not in ('gzip', 'deflate'):
                return None
            try:
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == 'gzip'
                                            else zlib.MAX_WBITS)
                decoded = decoder.decompress(content, self.max_response_bytes + 1)
                if (len(decoded) > self.max_response_bytes or decoder.unconsumed_tail
                        or decoder.unused_data or not decoder.eof):
                    return None
            except zlib.error:
                return None
        try:
            payload = json.loads(decoded)
            changed = normalize_subtitle_labels(payload, english_us_label=self.english_us_label,
                                                portuguese_br_label=self.portuguese_br_label)
            if not changed:
                return None
            return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        except (ValueError, UnicodeError, RecursionError):
            return None

    async def forward(self, request: Request) -> Response:
        headers = await self._forward_headers(
            request, remove={'host', 'content-length', 'accept-encoding'},
        )
        headers.append(('accept-encoding', 'identity'))
        # Construct directly: a shared client must not add its own auth/cookie jar.
        native_request = httpx.Request(
            request.method, self._url(request.scope), headers=headers, content=request.stream(),
        )
        try:
            native = await self.client.send(native_request, stream=True,
                                            follow_redirects=False, auth=None)
        except httpx.HTTPError:
            raise HTTPException(502, 'Jellyfin upstream unavailable') from None
        native_headers = _headers(native.headers)
        content_type = native.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        candidate = (self.enabled and native.status_code == 200
                     and (content_type == 'application/json' or content_type.endswith('+json')))
        chunks: list[bytes] = []
        size = 0
        iterator = native.aiter_raw(chunk_size=64 * 1024)

        async def passthrough():
            try:
                for chunk in chunks:
                    yield chunk
                async for chunk in iterator:
                    yield chunk
            finally:
                await native.aclose()

        if candidate:
            try:
                async for chunk in iterator:
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > self.max_response_bytes:
                        break
                else:
                    content = b''.join(chunks)
                    replacement = self._normalized_json(
                        content, native.headers.get('content-encoding', '').lower(),
                    )
                    await native.aclose()
                    if replacement is not None:
                        changed_headers = _headers(native.headers, remove=_CHANGED_HEADERS)
                        return _set_response_headers(Response(replacement, native.status_code),
                                                     changed_headers)
                    return _set_response_headers(Response(content, native.status_code),
                                                 native_headers)
            except BaseException:
                await native.aclose()
                raise
        return _set_response_headers(StreamingResponse(passthrough(),
                                     status_code=native.status_code), native_headers)

    async def socket(self, websocket: WebSocket) -> None:
        # The client can leave during the native handshake, accept, send or close.
        # Starlette converts a lost transport to WebSocketDisconnect after accept.
        with suppress(WebSocketDisconnect, OSError):
            await self._socket(websocket)

    async def _socket(self, websocket: WebSocket) -> None:
        # Import lazily; HTTP relaying remains available when sockets aren't installed.
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

        connector = self.socket_connect or connect
        headers = await self._forward_headers(websocket, remove={
            'host', 'origin', 'sec-websocket-key', 'sec-websocket-version',
            'sec-websocket-extensions', 'sec-websocket-protocol',
        })
        subprotocols = websocket.scope.get('subprotocols') or None
        try:
            native = await connector(str(self._url(websocket.scope, socket=True)),
                additional_headers=headers, origin=websocket.headers.get('origin'),
                subprotocols=subprotocols, user_agent_header=None, proxy=None,
                compression=None, max_size=self.max_response_bytes, max_queue=4,
                host=self.base.host,
                port=self.base.port or (443 if self.base.scheme == 'https' else 80),
                open_timeout=15, close_timeout=5, logger=_SOCKET_LOGGER)
        except InvalidStatus as error:
            response = error.response
            denial = Response(bytes(response.body), status_code=response.status_code)
            _set_response_headers(denial, _headers(response.headers, remove={'content-length'}))
            if 'websocket.http.response' in websocket.scope.get('extensions', {}):
                await websocket.send_denial_response(denial)
            else:
                await websocket.close(code=1008)
            return
        except (OSError, TimeoutError, InvalidHandshake, ValueError):
            await websocket.close(code=1011)
            return
        tasks: list[asyncio.Task] = []
        try:
            # Client is accepted only after native authentication/handshake succeeded.
            handshake_headers = _headers(native.response.headers, remove={
                'sec-websocket-accept', 'sec-websocket-protocol', 'sec-websocket-extensions',
                'content-length', 'date', 'server',
            })
            await websocket.accept(subprotocol=native.subprotocol, headers=[
                (key.lower().encode('latin-1'), value.encode('latin-1'))
                for key, value in handshake_headers
            ])

            async def to_native():
                while True:
                    message = await websocket.receive()
                    if message['type'] == 'websocket.disconnect':
                        code = message.get('code', 1000)
                        await native.close(code=1000 if code in (1005, 1006, 1015) else code,
                                           reason=message.get('reason', ''))
                        return
                    value = message.get('bytes')
                    if value is None:
                        value = message.get('text', '')
                    size = len(value) if isinstance(value, bytes) else len(value.encode('utf-8'))
                    if size > self.max_response_bytes:
                        await websocket.close(code=1009)
                        await native.close(code=1009)
                        return
                    await native.send(value)

            async def to_client():
                try:
                    while True:
                        message = await native.recv()
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            replacement = (self._normalized_json(message.encode('utf-8'))
                                           if self.enabled else None)
                            await websocket.send_text(replacement.decode('utf-8')
                                                      if replacement is not None else message)
                except ConnectionClosed as error:
                    code = (error.rcvd.code if error.rcvd is not None else
                            error.sent.code if error.sent is not None else
                            native.close_code or 1000)
                    await websocket.close(code=1011 if code in (1005, 1006, 1015) else code,
                                          reason=native.close_reason or '')

            tasks = [asyncio.create_task(to_native()), asyncio.create_task(to_client())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                with suppress(ConnectionClosed, WebSocketDisconnect, RuntimeError):
                    task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await native.close()
            if websocket.application_state == WebSocketState.CONNECTED:
                with suppress(WebSocketDisconnect, RuntimeError, OSError):
                    await websocket.close(code=1000)


def register_jellyfin_label_routes(app: FastAPI, proxy: JellyfinLabelProxy) -> None:
    for name in ('uvicorn.error', 'uvicorn.access', 'httpx'):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _SecretQueryFilter) for item in logger.filters):
            logger.addFilter(_SecretQueryFilter())
    for prefix in ('', '/jellyfin'):
        for path in GET_ROUTES:
            app.add_api_route(prefix + path, proxy.forward, methods=['GET'],
                              include_in_schema=False)
            route = app.router.routes[-1]
            route.path_regex = re.compile(route.path_regex.pattern[:-1] + '/?$', re.IGNORECASE)
        for path in POST_ROUTES:
            app.add_api_route(prefix + path, proxy.forward, methods=['POST'],
                              include_in_schema=False)
            route = app.router.routes[-1]
            route.path_regex = re.compile(route.path_regex.pattern[:-1] + '/?$', re.IGNORECASE)
        app.add_api_websocket_route(prefix + '/socket', proxy.socket)
        route = app.router.routes[-1]
        route.path_regex = re.compile(route.path_regex.pattern[:-1] + '/?$', re.IGNORECASE)
