"""Run the deployed path router against private loopback fixture backends."""

import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def service_router(tmp_path_factory):
    caddy = os.environ.get("HOMESERVER_TEST_CADDY") or shutil.which("caddy")
    image = 'caddy@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f'
    if caddy is None and (not shutil.which('docker') or os.name != 'posix'):
        pytest.skip('Caddy or Docker unavailable for the real routing fixture')
    if caddy is None:
        available = subprocess.run(['docker', 'image', 'inspect', image],
                                   capture_output=True, timeout=5)
        if available.returncode:
            pytest.skip('The pinned Caddy fixture image is not available locally')

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_DELETE(self):
            self.respond()

        def respond(self):
            if self.path.split('?')[0].endswith('/failure-fixture'):
                self.close_connection = True
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {key.lower(): value for key, value in self.headers.items()}
            payload = json.dumps({
                "path": self.path, "headers": headers, "method": self.command,
                "body": body.decode(),
            }).encode()
            self.send_response(302 if self.path == "/redirect-fixture" else 200)
            if self.path == "/redirect-fixture":
                self.send_header("Location", "/index.html?next=%2F")
            self.send_header("Set-Cookie", "SID=fixture; HttpOnly; Path=/; SameSite=Strict")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    temp = tmp_path_factory.mktemp("service-router")
    config = (ROOT / "deploy/service-proxy/Caddyfile").read_text()
    config = config.replace(":80 {", f"http://:{port} {{\n bind 127.0.0.1", 1)
    for backend in (
        "jellyfin-proxy:8096", "sonarr:8989", "radarr:7878", "prowlarr:9696",
        "bazarr:6767", "qbit-monitor:8080", "telemetry:8080",
    ):
        config = config.replace(backend, f"127.0.0.1:{upstream.server_port}")
    config_path = temp / "Caddyfile"
    if config.lstrip().startswith('{'):
        config = config.replace('{', '{\n admin off', 1)
    else:
        config = "{\n admin off\n}\n" + config
    config_path.write_text(config)
    environment = os.environ | {
        "XDG_CONFIG_HOME": str(temp / "config"), "XDG_DATA_HOME": str(temp / "data"),
    }
    command = [caddy] if caddy else [
        'docker', 'run', '--rm', '--network', 'host', '--read-only',
        '--cap-drop', 'ALL', '--cap-add', 'NET_BIND_SERVICE',
        '--security-opt', 'no-new-privileges:true',
        '--user', str(os.getuid()), '--entrypoint', 'caddy',
        '--mount', f'type=bind,source={temp},target={temp}',
        '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
        '-e', f'XDG_CONFIG_HOME={temp}/config', '-e', f'XDG_DATA_HOME={temp}/data', image,
    ]
    with (temp / "caddy.log").open("w+") as log:
        process = subprocess.Popen(
            [*command, "run", "--config", str(config_path), "--adapter", "caddyfile"],
            stdout=log, stderr=subprocess.STDOUT, env=environment,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(f"Router failed to start: {log.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail("Router did not open its fixture listener")

            def request(method, path, *, headers=None, body=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    connection.request(method, path, body=body, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, dict(response.getheaders()), response.read()
                finally:
                    connection.close()

            request.log_text = lambda: (temp / 'caddy.log').read_text()
            yield request
        finally:
            process.terminate()
            process.wait(timeout=5)
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)


@pytest.mark.parametrize("host", ["192.168.1.20", "100.74.194.46", "homelab.example.ts.net"])
def test_service_entrypoints_preserve_current_host_and_need_no_port(service_router, host):
    status, headers, _ = service_router("GET", "/", headers={"Host": host})
    assert status == 302
    assert headers["Location"] == "/status/"
    for name in ("jellyfin", "sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "status"):
        status, headers, _ = service_router("GET", f"/{name}", headers={"Host": host})
        assert status == 308
        assert headers["Location"] == f"/{name}/"
    for name in ("seer", "seerr"):
        status, headers, _ = service_router("GET", f"/{name}", headers={"Host": host})
        assert status == 302
        assert headers["Location"] == "https://homelab.coin-capella.ts.net/"


def test_upstream_error_never_logs_native_credentials(service_router):
    status, _, _ = service_router('GET',
        '/jellyfin/failure-fixture?api_key=fixture_query_secret',
        headers={'X-Emby-Token': 'fixture_header_secret'})
    assert status == 502
    # Read the actual process output, including Caddy's runtime error logger.
    time.sleep(0.1)
    log = service_router.log_text()
    assert 'fixture_query_secret' not in log
    assert 'fixture_header_secret' not in log


@pytest.mark.parametrize("service", ["jellyfin", "sonarr", "radarr", "prowlarr", "bazarr"])
def test_native_base_url_keeps_prefix_authentication_and_query(service_router, service):
    path = f"/{service}/api/fixture?query=dual%20audio"
    status, _, body = service_router("GET", path, headers={
        "Host": "homelab.example.ts.net", "Authorization": "Bearer fixture", "Cookie": "sid=x",
    })
    value = json.loads(body)
    assert status == 200
    assert value["path"] == path
    assert value["headers"]["host"] == "homelab.example.ts.net"
    assert value["headers"]["authorization"] == "Bearer fixture"
    assert value["headers"]["cookie"] == "sid=x"


def test_qbit_prefix_stripping_keeps_native_csrf_headers_and_scopes_cookie(service_router):
    headers = {
        "Host": "homelab.example.ts.net", "Origin": "http://evil.example",
        "Referer": "http://evil.example/page", "Cookie": "SID=fixture",
    }
    status, response_headers, body = service_router(
        "POST", "/qbittorrent/api/v2/torrents/stop", headers=headers, body="hashes=fixture",
    )
    value = json.loads(body)
    assert status == 200
    assert value["path"] == "/api/v2/torrents/stop"
    assert value["headers"]["origin"] == headers["Origin"]
    assert value["headers"]["referer"] == headers["Referer"]
    assert value["headers"]["cookie"] == "SID=fixture"
    assert value["body"] == "hashes=fixture"
    assert "Path=/qbittorrent/;" in response_headers["Set-Cookie"]
    assert "HttpOnly" in response_headers["Set-Cookie"]
    assert "SameSite=Strict" in response_headers["Set-Cookie"]
    status, response_headers, _ = service_router("GET", "/qbittorrent/redirect-fixture")
    assert status == 302
    assert response_headers["Location"] == "/qbittorrent/index.html?next=%2F"


def test_status_proxy_preserves_origin_and_does_not_publish_control_api(service_router):
    origin = "http://homelab.example.ts.net"
    status, _, body = service_router(
        "POST", "/status/api/v1/downloads/hash/replace", headers={
            "Host": "homelab.example.ts.net", "Origin": origin,
            "X-HomeServer-Action": "replace", "X-HomeServer-Path": "/injected",
        }, body='{"permit_id":"fixture"}',
    )
    value = json.loads(body)
    assert status == 200
    assert value["path"] == "/api/v1/downloads/hash/replace"
    assert value["headers"]["origin"] == origin
    assert value["headers"]["x-homeserver-path"] == "/status"
    for path in ("/api/v1/downloads", "/control/", "/unknown", "/radarrr/api"):
        assert service_router("GET", path)[0] == 404


def test_jellyfin_item_deletes_reach_capture_proxy_with_encoded_paths(service_router):
    path = "/jellyfin/Items/abc?api_key=fixture"
    status, _, body = service_router("DELETE", path, headers={"X-Emby-Token": "fixture"})
    value = json.loads(body)
    assert status == 200
    assert value["path"] == path
    assert value["method"] == "DELETE"
    assert value["headers"]["x-emby-token"] == "fixture"
