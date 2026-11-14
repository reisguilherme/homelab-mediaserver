"""Exact native Seerr root logos are public assets, never a general SVG route."""
from __future__ import annotations

import httpx
import pytest

from homeserver_control.api.public_access import PublicAccessSettings, create_app


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/logo_full.svg', '/logo_stacked.svg'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
@pytest.mark.parametrize('cookie', ['', 'connect.sid=expired'])
async def test_exact_seerr_logos_are_anonymous_reads(tmp_path, path, method, cookie):
    assert await authorize(tmp_path, path, method, cookie) == 204


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/logo_full.svg', '/logo_stacked.svg'])
@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
async def test_seerr_logo_writes_stay_forbidden(tmp_path, path, method):
    assert await authorize(tmp_path, path, method) == 403


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/logo_full.svg/extra', '/logo_stacked.svg/admin',
                                 '/other.svg', '/api/v1/settings/main.svg'])
async def test_seerr_logo_permission_does_not_expand_to_other_svg_or_prefixes(tmp_path, path):
    assert await authorize(tmp_path, path, 'GET') == 403


async def authorize(tmp_path, path, method, cookie=''):
    settings = PublicAccessSettings(
        jellyfin_url='http://jellyfin:8096/jellyfin', seerr_url='http://seerr:5055',
        trusted_proxy_ips=('172.30.0.8',), state_path=tmp_path / 'limits.sqlite3',
        seerr_origin='https://requests.example:54443',
    )
    def forbidden(_request):
        pytest.fail('Static asset classification must not query native identities')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as upstream:
        app = create_app(settings, client=upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=('172.30.0.8', 12345)),
            base_url='http://guard',
        ) as client:
            response = await client.get('/authorize/seerr', headers={
                'X-Original-URI': path, 'X-Original-Method': method, 'X-Real-IP': '203.0.113.9',
                'Origin': 'https://requests.example:54443', 'Cookie': cookie})
    return response.status_code
