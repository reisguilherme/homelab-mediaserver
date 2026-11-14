import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest


def relay_module():
    assert importlib.util.find_spec('homeserver_control.api.jellyfin_relay') is not None
    from homeserver_control.api import jellyfin_relay

    return jellyfin_relay


@pytest.mark.asyncio
async def test_relay_exposes_only_presentation_and_health_without_controller_routes():
    module = relay_module()

    def native(request):
        assert request.url.host == 'jellyfin'
        assert request.url.path == '/jellyfin/Items/fixture'
        assert request.headers['authorization'] == 'MediaBrowser Token="viewer"'
        assert 'x-admin-token' not in request.headers
        return httpx.Response(200, stream=httpx.ByteStream(b'{"Id":"fixture"}'),
                              headers={'content-type': 'application/json'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as upstream:
        app = module.create_relay_app('http://jellyfin:8096/jellyfin', client=upstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url='http://relay') as browser:
            assert (await browser.get('/health/live')).status_code == 200
            response = await browser.get('/Items/fixture', headers={
                'authorization': 'MediaBrowser Token="viewer"',
                'x-admin-token': 'must-not-forward', 'x-csrf-token': 'must-not-forward',
            })
            assert response.json() == {'Id': 'fixture'}
            for path in ('/api/v1/capacity', '/api/v1/downloads', '/api/v1/deletions/jobs',
                         '/ui/queue', '/docs', '/openapi.json', '/config'):
                assert (await browser.get(path)).status_code == 404
            assert (await browser.delete('/Items/fixture')).status_code == 405
    assert not vars(app.state).get('_state')


def test_relay_factory_import_and_startup_never_load_privileged_dependencies(monkeypatch):
    relay_module()
    monkeypatch.setenv('HOMESERVER_ADMIN_TOKEN', 'must-not-read')
    monkeypatch.setenv('HOMESERVER_DB_PATH', 'must-not-open')
    code = '''
import builtins
import os
original_import = builtins.__import__
forbidden = ('homeserver_control.api.app', 'homeserver_control.persistence',
             'homeserver_control.configuration', 'homeserver_common.settings',
             'homeserver_common.storage', 'sqlite3')
def guarded_import(name, *args, **kwargs):
    assert not name.startswith(forbidden), name
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
original_get = os.environ.get
def guarded_get(name, *args):
    assert name not in {'HOMESERVER_ADMIN_TOKEN', 'HOMESERVER_DB_PATH',
                        'HOMESERVER_JELLYFIN_API_KEY', 'HOMESERVER_ENV_PATH'}, name
    return original_get(name, *args)
os.environ.get = guarded_get
from homeserver_control.api.jellyfin_relay import create_app
from fastapi.testclient import TestClient
with TestClient(create_app()) as client:
    assert client.get('/health/live').status_code == 200
'''
    project = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    # pytest's pythonpath setting updates sys.path, not a child's environment.
    environment['PYTHONPATH'] = os.pathsep.join(
        str(project / 'services' / service / 'src')
        for service in ('common', 'control', 'telemetry')
    )
    result = subprocess.run(
        [sys.executable, '-c', code], capture_output=True, text=True, env=environment,
    )
    assert result.returncode == 0, result.stderr


def test_controller_does_not_register_presentation_routes():
    from homeserver_control.api.app import app

    routes = {(route.path, method) for route in app.routes
              for method in getattr(route, 'methods', ())}
    assert ('/Items', 'GET') not in routes
    assert ('/Items/{item_id}/PlaybackInfo', 'POST') not in routes
    assert ('/Items/{item_id}', 'DELETE') in routes
    assert not any(route.path in {'/socket', '/jellyfin/socket'} for route in app.routes)
