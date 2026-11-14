from __future__ import annotations

import asyncio
import gzip
import json
import logging
import socket

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
@pytest.mark.parametrize('trusted', [False, True])
@pytest.mark.parametrize('kind', ['http', 'websocket'])
@pytest.mark.parametrize('xff', ['203.0.113.9', '203.0.113.9, 127.0.0.1', 'invalid', 'duplicate'])
async def test_forwarded_identity_requires_actual_proxy_peer(monkeypatch, trusted, kind, xff):
    from fastapi import Request, WebSocket

    from homeserver_control.api.jellyfin_labels import JellyfinLabelProxy

    async def peers(_self):
        return {'172.23.0.7'}

    monkeypatch.setattr(JellyfinLabelProxy, '_trusted_proxy_peers', peers, raising=False)
    peer = '172.23.0.7' if trusted else '198.51.100.6'
    incoming = [(b'host', b'private-stream.mywire.org'),
                (b'x-forwarded-for', xff.encode()), (b'x-forwarded-proto', b'https'),
                (b'x-forwarded-host', b'attacker.invalid'), (b'forwarded', b'for=127.0.0.1'),
                (b'x-forwarded-prefix', b'/evil'), (b'x-real-ip', b'127.0.0.1')]
    if xff == 'duplicate':
        incoming += [(b'x-forwarded-for', b'203.0.113.9')]
    scope = {'type': kind, 'path': '/Items', 'raw_path': b'/Items', 'query_string': b'',
             'headers': incoming, 'scheme': 'ws' if kind == 'websocket' else 'http',
             'client': (peer, 1234), 'method': 'GET'}
    captured = {}

    def native(request):
        captured.update(request.headers)
        return native_response(200)

    async def connector(_url, **kwargs):
        captured.update(dict(kwargs['additional_headers']))
        raise OSError('fixture handshake ends')

    async def send(_message):
        pass

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as upstream:
        proxy = JellyfinLabelProxy('http://jellyfin:8096', client=upstream,
                                   socket_connect=connector)
        if kind == 'http':
            await proxy.forward(Request(scope, receive=receive))
        else:
            await proxy.socket(WebSocket(scope, receive=receive, send=send))
    valid = trusted and xff == '203.0.113.9'
    assert captured['x-forwarded-for'] == ('203.0.113.9' if valid else peer)
    assert captured['x-real-ip'] == captured['x-forwarded-for']
    assert captured['x-forwarded-proto'] == ('https' if valid else 'http')
    assert captured['x-forwarded-host'] == 'private-stream.mywire.org'
    assert 'forwarded' not in captured
    assert 'x-forwarded-prefix' not in captured


@pytest.mark.asyncio
async def test_proxy_dns_failure_recovers_with_short_cache(monkeypatch):
    from homeserver_control.api.jellyfin_labels import JellyfinLabelProxy

    clock = [100.0]
    calls = []

    async def lookup(host, *_args, **_kwargs):
        assert host == 'jellyfin-proxy'
        calls.append(host)
        if len(calls) == 1:
            raise socket.gaierror('not started')
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('172.23.0.7', 8096))]

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
    monkeypatch.setattr('homeserver_control.api.jellyfin_labels._TRUSTED_PROXY_HOSTS',
                        ('jellyfin-proxy',), raising=False)
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    proxy = JellyfinLabelProxy('http://jellyfin:8096')
    try:
        assert await proxy._trusted_proxy_peers() == set()
        clock[0] += 4
        assert await proxy._trusted_proxy_peers() == set()
        assert len(calls) == 1
        clock[0] += 2
        assert await proxy._trusted_proxy_peers() == {'172.23.0.7'}
        clock[0] += 29
        assert await proxy._trusted_proxy_peers() == {'172.23.0.7'}
        assert len(calls) == 2
        clock[0] += 2
        await proxy._trusted_proxy_peers()
        assert len(calls) == 3
    finally:
        await proxy.aclose()


@pytest.mark.asyncio
async def test_proxy_dns_tracks_both_dedicated_proxies_and_recreated_ipv6_peers(monkeypatch):
    from homeserver_control.api.jellyfin_labels import JellyfinLabelProxy

    clock = [100.0]
    addresses = {'jellyfin-proxy': '172.23.0.7', 'public-proxy': 'fd00::8'}
    calls = []

    async def lookup(host, *_args, **_kwargs):
        calls.append(host)
        address = addresses[host]
        family = socket.AF_INET6 if ':' in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, '', (address, 8096))]

    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', lookup)
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    proxy = JellyfinLabelProxy('http://jellyfin:8096')
    try:
        assert await proxy._trusted_proxy_peers() == {'172.23.0.7', 'fd00::8'}
        addresses['public-proxy'] = 'fd00::9'
        clock[0] += 31
        assert await proxy._trusted_proxy_peers() == {'172.23.0.7', 'fd00::9'}
        assert set(calls) == {'jellyfin-proxy', 'public-proxy'}
    finally:
        await proxy.aclose()


def native_response(status, *, content=b'', json_body=None, headers=None):
    headers = dict(headers or {})
    if json_body is not None:
        content = json.dumps(json_body).encode()
        headers['Content-Type'] = 'application/json'
    headers.setdefault('Content-Length', str(len(content)))
    return httpx.Response(status, stream=httpx.ByteStream(content), headers=headers)


def proxy_app(handler, **options):
    from homeserver_control.api.jellyfin_labels import (
        JellyfinLabelProxy,
        register_jellyfin_label_routes,
    )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = JellyfinLabelProxy('http://jellyfin:8096/jellyfin', client=client, **options)
    app = FastAPI()
    register_jellyfin_label_routes(app, proxy)
    return app, client


@pytest.mark.asyncio
@pytest.mark.parametrize('prefix', ['', '/jellyfin'])
async def test_playback_proxy_uses_client_auth_and_preserves_encoded_query_and_body(prefix):
    def native(request):
        assert str(request.url) == (
            'http://jellyfin:8096/jellyfin/Items/fixture/PlaybackInfo'
            '?api_key=client-secret&term=a%2Fb&term=a%20b'
        )
        assert request.method == 'POST'
        assert request.content == b'{"SubtitleStreamIndex":7}'
        assert request.headers['X-Emby-Token'] == 'client-token'
        assert request.headers['Authorization'] == 'MediaBrowser Token="client-auth"'
        assert request.headers['Cookie'] == 'native=session'
        assert request.headers['Accept-Encoding'] == 'identity'
        return native_response(200, headers={'ETag': 'native', 'X-Native': 'kept'}, json_body={
            'MediaSources': [{'Id': 'source', 'MediaStreams': [
                {'Type': 'Subtitle', 'Index': 7, 'Language': 'pob', 'Codec': 'subrip',
                 'DeliveryUrl': '/Videos/fixture/source/Subtitles/7/Stream.srt',
                 'IsForced': True, 'DisplayTitle': 'Pob - Forçado - SUBRIP - Externo'},
            ]}], 'DefaultSubtitleStreamIndex': 7,
        })

    app, upstream = proxy_app(native)
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        response = await browser.post(prefix + '/Items/fixture/PlaybackInfo'
            '?api_key=client-secret&term=a%2Fb&term=a%20b',
            content=b'{"SubtitleStreamIndex":7}', headers={
                'X-Emby-Token': 'client-token',
                'Authorization': 'MediaBrowser Token="client-auth"', 'Cookie': 'native=session',
            })
    assert response.status_code == 200
    stream = response.json()['MediaSources'][0]['MediaStreams'][0]
    assert stream == {'Type': 'Subtitle', 'Index': 7, 'Language': 'pob', 'Codec': 'subrip',
        'DeliveryUrl': '/Videos/fixture/source/Subtitles/7/Stream.srt',
        'IsForced': True, 'DisplayTitle': 'Português BR - Forçada'}
    assert 'etag' not in response.headers
    assert response.headers['x-native'] == 'kept'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 404, 429, 500, 304])
async def test_native_status_and_bodies_are_not_replaced(status):
    body = b'' if status == 304 else b'{"Message":"native denial"}'
    app, upstream = proxy_app(lambda r: native_response(status, content=body,
        headers={'Content-Type': 'application/json', 'ETag': 'native', 'Retry-After': '3'}))
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        response = await browser.get('/Items/fixture')
    assert response.status_code == status
    assert response.content == body
    assert response.headers['etag'] == 'native'
    assert response.headers['retry-after'] == '3'


@pytest.mark.asyncio
@pytest.mark.parametrize('path,method', [
    ('/Videos/fixture/stream.mkv', 'GET'), ('/Items/fixture/Images/Primary', 'GET'),
    ('/Items/fixture', 'DELETE'), ('/Collections/fixture/Items', 'POST'),
    ('/Items/fixture', 'POST'), ('/socket', 'GET'),
])
async def test_media_admin_writes_and_http_socket_never_reach_label_proxy(path, method):
    def native(request):
        raise AssertionError('non-label request reached upstream')

    app, upstream = proxy_app(native)
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        response = await browser.request(method, path)
    assert response.status_code in (404, 405)


@pytest.mark.asyncio
@pytest.mark.parametrize('enabled', [True, False])
async def test_gzip_is_decoded_only_when_labels_change(enabled):
    content = json.dumps({'MediaStreams': [{'Type': 'Subtitle', 'Language': 'eng',
        'DisplayTitle': 'English - SUBRIP'}]}).encode()
    compressed = gzip.compress(content)
    app, upstream = proxy_app(lambda r: native_response(200, content=compressed,
        headers={'Content-Type': 'application/json', 'Content-Encoding': 'gzip',
                 'Content-Length': str(len(compressed)), 'ETag': 'native'}), enabled=enabled)
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        response = await browser.get('/Items/fixture')
    if enabled:
        assert response.json()['MediaStreams'][0]['DisplayTitle'] == 'English - US'
        assert 'content-encoding' not in response.headers
        assert 'etag' not in response.headers
    else:
        assert response.content == content
        assert response.headers['content-encoding'] == 'gzip'
        assert response.headers['etag'] == 'native'


@pytest.mark.asyncio
@pytest.mark.parametrize('body,content_type', [
    (b'{malformed', 'application/json'), (b'plain body', 'text/plain'),
    (b'{"MediaStreams":[]}', 'application/json'),
])
async def test_unmodified_payload_preserves_bytes_and_validators(body, content_type):
    app, upstream = proxy_app(lambda r: native_response(200, content=body,
        headers={'Content-Type': content_type, 'ETag': 'native'}))
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        response = await browser.get('/Items/fixture')
    assert response.content == body
    assert response.headers['etag'] == 'native'


@pytest.mark.asyncio
async def test_oversized_json_passthrough_closes_native_stream():
    class Body(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'{"MediaStreams":'
            yield b' ' * 200
            yield b'[]}'

        async def aclose(self):
            self.closed = True

    stream = Body()
    app, upstream = proxy_app(lambda r: httpx.Response(200, stream=stream,
        headers={'Content-Type': 'application/json', 'ETag': 'native'}), max_response_bytes=100)
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        response = await browser.get('/Items/fixture')
    assert response.content == b'{"MediaStreams":' + b' ' * 200 + b'[]}'
    assert response.headers['etag'] == 'native'
    assert stream.closed


@pytest.mark.asyncio
async def test_shared_native_client_never_supplies_admin_auth_or_another_users_cookie():
    from homeserver_control.api.jellyfin_labels import (
        JellyfinLabelProxy,
        register_jellyfin_label_routes,
    )

    def native(request):
        assert 'X-Emby-Token' not in request.headers
        assert 'Authorization' not in request.headers
        assert 'Cookie' not in request.headers
        return native_response(200, content=b'{}', headers={
            'Content-Type': 'application/json', 'Set-Cookie': 'native=other-user; Path=/',
        })

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(native),
        headers={'X-Emby-Token': 'must-never-forward'}, auth=('admin', 'admin-password'))
    proxy = JellyfinLabelProxy('http://jellyfin:8096/jellyfin', client=upstream)
    app = FastAPI()
    register_jellyfin_label_routes(app, proxy)
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        for _ in range(2):
            browser.cookies.clear()
            assert (await browser.get('/Items/fixture')).status_code == 200


def test_uvicorn_socket_and_httpx_url_logs_redact_only_secret_query_values():
    from homeserver_control.api.jellyfin_labels import register_jellyfin_label_routes

    app, _ = proxy_app(lambda r: native_response(200))
    # Registration installs the narrowly scoped filter on the emitters.
    assert app is not None
    for name in ('uvicorn.error', 'httpx'):
        record = logging.LogRecord(name, logging.INFO, '', 1, '%s - "%s" [accepted]',
            ('client', '/jellyfin/socket?api_key=secret%2Fvalue&deviceId=visible'
             '&Access_Token=second'),
            None)
        for log_filter in logging.getLogger(name).filters:
            log_filter.filter(record)
        rendered = record.getMessage()
        assert 'secret%2Fvalue' not in rendered
        assert 'second' not in rendered
        assert 'deviceId=visible' in rendered
    assert register_jellyfin_label_routes is not None


@pytest.mark.asyncio
async def test_socket_native_handshake_messages_and_close_preserve_client_identity():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from websockets.asyncio.server import serve

    from homeserver_control.api.jellyfin_labels import (
        JellyfinLabelProxy,
        register_jellyfin_label_routes,
    )

    entered = asyncio.Event()

    def handshake(connection, request, response):
        response.headers['Set-Cookie'] = 'native=owner; Path=/jellyfin'
        return response

    async def native(socket):
        assert socket.request.path == '/jellyfin/SOCKET/?api_key=client%2Ftoken&deviceId=device'
        assert socket.request.headers['Cookie'] == 'native=client'
        assert socket.request.headers['X-Emby-Token'] == 'client-token'
        assert socket.subprotocol == 'native-protocol'
        entered.set()
        assert await socket.recv() == 'SessionsStart'
        await socket.send('{"MessageType":"Sessions","Data":[{"NowPlayingItem":{'
            '"MediaStreams":[{"Type":"Subtitle","Index":4,"Language":"pob",'
            '"DisplayTitle":"Pob - SUBRIP"}]}}]}')
        await socket.send(b'\x00native binary')
        await socket.send('native non-json')
        await socket.close(code=1001, reason='native shutdown')

    async with serve(native, '127.0.0.1', 0, subprotocols=['native-protocol'],
                     process_response=handshake) as server:
        port = server.sockets[0].getsockname()[1]
        proxy = JellyfinLabelProxy(f'http://127.0.0.1:{port}/jellyfin')
        app = FastAPI()
        register_jellyfin_label_routes(app, proxy)

        def browser():
            with TestClient(app) as client, client.websocket_connect(
                '/JELLYFIN/SOCKET/?api_key=client%2Ftoken&deviceId=device',
                headers={'Cookie': 'native=client', 'X-Emby-Token': 'client-token'},
                subprotocols=['native-protocol'],
            ) as socket:
                assert socket.accepted_subprotocol == 'native-protocol'
                assert (b'set-cookie', b'native=owner; Path=/jellyfin') in socket.extra_headers
                socket.send_text('SessionsStart')
                payload = socket.receive_json()
                assert entered.is_set()
                assert payload['Data'][0]['NowPlayingItem']['MediaStreams'][0] == {
                    'Type': 'Subtitle', 'Index': 4, 'Language': 'pob',
                    'DisplayTitle': 'Português BR',
                }
                assert socket.receive_bytes() == b'\x00native binary'
                assert socket.receive_text() == 'native non-json'
                with pytest.raises(WebSocketDisconnect) as closed:
                    socket.receive_text()
                assert closed.value.code == 1001
                assert closed.value.reason == 'native shutdown'

        try:
            await asyncio.wait_for(asyncio.to_thread(browser), timeout=15)
        finally:
            await proxy.aclose()


@pytest.mark.asyncio
async def test_socket_native_denial_is_sent_before_client_is_accepted():
    from fastapi.testclient import TestClient
    from starlette.testclient import WebSocketDenialResponse
    from websockets.asyncio.server import serve

    from homeserver_control.api.jellyfin_labels import (
        JellyfinLabelProxy,
        register_jellyfin_label_routes,
    )

    def reject(connection, request):
        return connection.respond(401, 'native authentication required')

    async def unused(socket):
        raise AssertionError('unauthenticated socket accepted')

    async with serve(unused, '127.0.0.1', 0, process_request=reject) as server:
        port = server.sockets[0].getsockname()[1]
        proxy = JellyfinLabelProxy(f'http://127.0.0.1:{port}/jellyfin')
        app = FastAPI()
        register_jellyfin_label_routes(app, proxy)

        def browser():
            with TestClient(app) as client, pytest.raises(WebSocketDenialResponse) as denied:
                with client.websocket_connect('/socket'):
                    pytest.fail('native authentication bypassed')
            assert denied.value.status_code == 401
            assert denied.value.text == 'native authentication required'

        try:
            await asyncio.wait_for(asyncio.to_thread(browser), timeout=15)
        finally:
            await proxy.aclose()


@pytest.mark.asyncio
async def test_socket_client_message_limit_closes_before_forwarding_oversized_message():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from websockets.asyncio.server import serve
    from websockets.exceptions import ConnectionClosed

    from homeserver_control.api.jellyfin_labels import (
        JellyfinLabelProxy,
        register_jellyfin_label_routes,
    )

    native_closed = asyncio.Event()

    async def native(socket):
        try:
            await socket.recv()
            await socket.send('oversized message reached native')
        except ConnectionClosed:
            assert socket.close_code == 1009
            native_closed.set()

    async with serve(native, '127.0.0.1', 0) as server:
        port = server.sockets[0].getsockname()[1]
        proxy = JellyfinLabelProxy(f'http://127.0.0.1:{port}/jellyfin', max_response_bytes=64)
        app = FastAPI()
        register_jellyfin_label_routes(app, proxy)

        def browser():
            with TestClient(app) as client, client.websocket_connect('/socket') as socket:
                socket.send_text('x' * 200)
                with pytest.raises(WebSocketDisconnect) as closed:
                    socket.receive_text()
                assert closed.value.code == 1009

        try:
            await asyncio.wait_for(asyncio.to_thread(browser), timeout=15)
            await asyncio.wait_for(native_closed.wait(), timeout=3)
        finally:
            await proxy.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('path,expected_path', [
    ('/items/fixture/', '/jellyfin/items/fixture/'),
    ('/JELLYFIN/items/fixture/', '/jellyfin/items/fixture/'),
])
async def test_native_case_and_trailing_slash_do_not_change_catalog_semantics(path, expected_path):
    def native(request):
        assert request.url.path == expected_path
        return native_response(200, json_body={'MediaStreams': [
            {'Type': 'Subtitle', 'Language': 'eng', 'DisplayTitle': 'English'},
        ]})

    app, upstream = proxy_app(native)
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        response = await browser.get(path)
    assert response.status_code == 200
    assert response.json()['MediaStreams'][0]['DisplayTitle'] == 'English - US'


@pytest.mark.asyncio
async def test_http_native_redirect_never_changes_fixed_upstream_host():
    def native(request):
        assert request.url.host == 'jellyfin'
        return native_response(302, headers={'Location': 'http://untrusted.invalid/Items/fixture'})

    app, upstream = proxy_app(native)
    upstream.follow_redirects = True
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                          base_url='http://public') as browser:
        response = await browser.get('/Items/fixture')
    assert response.status_code == 302
    assert response.headers['location'] == 'http://untrusted.invalid/Items/fixture'


@pytest.mark.asyncio
async def test_socket_redirect_cannot_escape_fixed_host_or_expose_location_in_an_error():
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from websockets.asyncio.server import serve

    from homeserver_control.api.jellyfin_labels import (
        JellyfinLabelProxy,
        register_jellyfin_label_routes,
    )

    def redirect(connection, request):
        response = connection.respond(302, 'redirect')
        response.headers['Location'] = 'ws://untrusted.invalid/socket?api_key=client-secret'
        return response

    async def unused(socket):
        raise AssertionError('redirected socket accepted')

    async with serve(unused, '127.0.0.1', 0, process_request=redirect) as server:
        port = server.sockets[0].getsockname()[1]
        proxy = JellyfinLabelProxy(f'http://127.0.0.1:{port}/jellyfin')
        app = FastAPI()
        register_jellyfin_label_routes(app, proxy)

        def browser():
            with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as denied:
                with client.websocket_connect('/socket'):
                    pytest.fail('redirected socket accepted')
            assert denied.value.code == 1011

        try:
            await asyncio.wait_for(asyncio.to_thread(browser), timeout=15)
        finally:
            await proxy.aclose()


@pytest.mark.asyncio
async def test_socket_client_departure_is_quiet_with_real_uvicorn(caplog):
    import uvicorn
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve

    from homeserver_control.api.jellyfin_labels import (
        JellyfinLabelProxy,
        register_jellyfin_label_routes,
    )

    native_closes = []

    async def native(connection):
        assert await connection.recv() == 'ready'
        await connection.send('ready')
        await connection.wait_closed()
        native_closes.append(connection.close_code)

    caplog.set_level(logging.ERROR, logger='uvicorn.error')
    async with serve(native, '127.0.0.1', 0, server_header='native-fixture') as upstream:
        upstream_port = upstream.sockets[0].getsockname()[1]
        proxy = JellyfinLabelProxy(f'http://127.0.0.1:{upstream_port}/jellyfin')
        app = FastAPI()
        register_jellyfin_label_routes(app, proxy)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.setblocking(False)
        frontend_port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(
            app, lifespan='off', ws='websockets', log_config=None, access_log=False,
        ))
        server_task = asyncio.create_task(server.serve(sockets=[listener]))

        async def started():
            while not server.started:
                if server_task.done():
                    server_task.result()
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(started(), timeout=3)
            for abrupt in (False, True):
                async with connect(f'ws://127.0.0.1:{frontend_port}/socket', proxy=None) as client:
                    assert len(client.response.headers.get_all('Date')) == 1
                    assert client.response.headers.get_all('Server') == ['uvicorn']
                    await client.send('ready')
                    assert await client.recv() == 'ready'
                    if abrupt:
                        client.transport.abort()
                        await client.wait_closed()
                    else:
                        await client.close(code=1000, reason='client finished')
        finally:
            server.should_exit = True
            await asyncio.wait_for(server_task, timeout=5)
            listener.close()
            await proxy.aclose()

    assert native_closes == [1000, 1000]
    assert not [record for record in caplog.records
                if record.name == 'uvicorn.error' and record.levelno >= logging.ERROR]


@pytest.mark.asyncio
@pytest.mark.parametrize('departure', ['accept', 'send', 'native-close', 'final-close',
                                      'denial', 'upstream-unavailable'])
async def test_socket_transport_departure_races_clean_up_without_raising(departure):
    from types import SimpleNamespace

    from starlette.websockets import WebSocket
    from websockets.exceptions import ConnectionClosedOK, InvalidStatus
    from websockets.frames import Close
    from websockets.http11 import Response

    from homeserver_control.api.jellyfin_labels import JellyfinLabelProxy

    close_calls = []

    async def native_close(**options):
        close_calls.append(options)

    async def native_receive():
        if departure == 'native-close':
            raise ConnectionClosedOK(Close(1000, ''), Close(1000, ''), True)
        if departure == 'send':
            return 'native message'
        await asyncio.Future()

    native = SimpleNamespace(
        response=SimpleNamespace(headers=httpx.Headers()), subprotocol=None,
        recv=native_receive, close=native_close, close_code=1000, close_reason='',
    )

    async def connector(*args, **options):
        if departure == 'upstream-unavailable':
            raise OSError('native unavailable')
        if departure == 'denial':
            raise InvalidStatus(Response(401, 'Unauthorized', httpx.Headers(), b'auth required'))
        return native

    first_receive = True

    async def receive():
        nonlocal first_receive
        if first_receive:
            first_receive = False
            return {'type': 'websocket.connect'}
        if departure == 'final-close':
            return {'type': 'websocket.disconnect', 'code': 1000}
        await asyncio.Future()

    async def send(message):
        if message['type'] != 'websocket.accept' or departure == 'accept':
            raise OSError('client transport left')

    frontend = WebSocket({
        'type': 'websocket', 'path': '/socket', 'raw_path': b'/socket',
        'query_string': b'', 'headers': [], 'subprotocols': [],
        'extensions': {'websocket.http.response': {}},
    }, receive, send)
    proxy = JellyfinLabelProxy('http://jellyfin:8096/jellyfin', socket_connect=connector)
    try:
        await asyncio.wait_for(proxy.socket(frontend), timeout=2)
    finally:
        await proxy.aclose()
    assert close_calls or departure in {'denial', 'upstream-unavailable'}
