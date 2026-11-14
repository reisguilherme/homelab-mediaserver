import httpx
import pytest

from homeserver_common.seerr_csrf import SeerrClient


@pytest.mark.asyncio
async def test_native_secure_csrf_cookie_and_token_retry_only_rejected_mutation():
    writes = []
    calls = []

    def native(request):
        calls.append((request.method, request.url.path))
        if request.method == 'GET':
            assert request.url.path == '/api/v1/settings/public'
            return httpx.Response(200, json={}, headers=[
                ('set-cookie', '_csrf=fixture-secret; Secure; HttpOnly; Path=/'),
                ('set-cookie', 'XSRF-TOKEN=fixture-token; Secure; Path=/'),
            ])
        if request.headers.get('X-CSRF-Token') != 'fixture-token':
            return httpx.Response(403, json={'message': 'invalid csrf token'})
        assert '_csrf=fixture-secret' in request.headers['cookie']
        assert 'connect.sid=fixture-session' in request.headers['cookie']
        assert request.headers['X-Api-Key'] == 'fixture-key'
        writes.append(request.content)
        return httpx.Response(204)

    async with httpx.AsyncClient(base_url='http://seerr:5055',
                                transport=httpx.MockTransport(native)) as raw:
        client = SeerrClient(raw)
        response = await client.put('/api/v1/settings/main', json={'newPlexLogin': False},
                                    headers={'X-Api-Key': 'fixture-key',
                                             'Cookie': 'connect.sid=fixture-session'})
    assert response.status_code == 204
    assert len(writes) == 1
    assert calls == [('PUT', '/api/v1/settings/main'), ('GET', '/api/v1/settings/public'),
                     ('PUT', '/api/v1/settings/main')]


@pytest.mark.asyncio
@pytest.mark.parametrize('status,message,method,target', [
    (403, 'Forbidden', 'POST', 'http://seerr:5055/api/v1/request'),
    (401, 'invalid csrf token', 'POST', 'http://seerr:5055/api/v1/request'),
    (403, 'invalid csrf token', 'GET', 'http://seerr:5055/api/v1/request'),
    (403, 'invalid csrf token', 'POST', 'http://other:5055/api/v1/request'),
])
async def test_other_failures_and_other_origins_never_retry(status, message, method, target):
    calls = []

    def native(request):
        calls.append(request.url)
        return httpx.Response(status, json={'message': message})

    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as raw:
        client = SeerrClient(raw, base_url='http://seerr:5055')
        response = await client.request(method, target)
    assert response.status_code == status and len(calls) == 1


@pytest.mark.asyncio
async def test_csrf_bootstrap_does_not_follow_redirects_or_replay_without_pair():
    calls = []

    def native(request):
        calls.append(request.url.host)
        if request.method == 'GET':
            return httpx.Response(302, headers={'location': 'http://untrusted/'})
        return httpx.Response(403, json={'message': 'invalid csrf token'})

    async with httpx.AsyncClient(base_url='http://seerr', follow_redirects=True,
                                transport=httpx.MockTransport(native)) as raw:
        response = await SeerrClient(raw).delete('/api/v1/request/42')
    assert response.status_code == 403 and calls == ['seerr', 'seerr']
