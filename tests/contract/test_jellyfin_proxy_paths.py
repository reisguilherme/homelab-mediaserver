"""Verify the real nginx proxy's legacy/base URL and deletion dispatch."""

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


@pytest.fixture(scope="module")
def jellyfin_path_proxy(tmp_path_factory):
    nginx = os.environ.get("HOMESERVER_TEST_NGINX") or shutil.which("nginx")
    if nginx is None:
        pytest.skip("nginx unavailable; set HOMESERVER_TEST_NGINX for real proxy tests")

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_DELETE(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            payload = json.dumps({
                "path": self.path, "method": self.command, "upstream": self.server.label,
                "headers": {key.lower(): value for key, value in self.headers.items()},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    servers = []
    threads = []
    for label in ("jellyfin", "capture"):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        server.label = label
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    temp = tmp_path_factory.mktemp("jellyfin-paths")
    root = Path(__file__).resolve().parents[2]
    config = (root / "deploy/jellyfin-proxy/nginx.conf").read_text()
    config = config.replace("worker_processes 2;", "worker_processes 1;")
    config = config.replace('172.30.240.1:https', '127.0.0.3:https')
    config = config.replace("listen 8096;", f"listen 127.0.0.1:{port};")
    config = config.replace("jellyfin:8096", f"127.0.0.1:{servers[0].server_port}")
    config = config.replace("control-api:8080", f"127.0.0.1:{servers[1].server_port}")
    config = config.replace("jellyfin-relay:8083", f"127.0.0.1:{servers[1].server_port}")
    config = config.replace("/var/log/nginx/access.log", str(temp / "access.log"))
    config = config.replace("pid /tmp/nginx.pid;", f"pid {temp}/nginx.pid;")
    # Debian package defaults use /var/lib/nginx; fixture state must stay private.
    config = config.replace("http {", f"http {{\nclient_body_temp_path {temp}/body;\n"
                            f"proxy_temp_path {temp}/proxy;\nfastcgi_temp_path {temp}/fastcgi;\n"
                            f"scgi_temp_path {temp}/scgi;\nuwsgi_temp_path {temp}/uwsgi;")
    config_path = temp / "nginx.conf"
    config_path.write_text(config)
    with (temp / "process.log").open("w+") as log:
        process = subprocess.Popen(
            [nginx, "-p", str(temp), "-c", str(config_path), "-g", "daemon off;"],
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(f"nginx fixture failed to start: {log.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail("nginx fixture did not open its loopback listener")

            def request(method, path, headers=None, source='127.0.0.1'):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3,
                                                        source_address=(source, 0))
                try:
                    connection.request(method, path, headers=headers or {})
                    response = connection.getresponse()
                    assert response.status == 200
                    return json.loads(response.read())
                finally:
                    connection.close()

            yield request
        finally:
            process.terminate()
            process.wait(timeout=5)
            for server, thread in zip(servers, threads, strict=True):
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


@pytest.mark.parametrize('source,expected_ip,expected_scheme', [
    ('127.0.0.1', '127.0.0.1', 'https'),
    ('127.0.0.2', '127.0.0.2', 'http'),
    ('127.0.0.3', '127.0.0.3', 'https'),
])
def test_private_proxy_uses_actual_peer_and_trusts_https_only_from_exact_peers(
    jellyfin_path_proxy, source, expected_ip, expected_scheme,
):
    headers = jellyfin_path_proxy('GET', '/System/Info/Public', {
        'Host': 'private-stream.mywire.org', 'X-Forwarded-For': '203.0.113.9',
        'X-Forwarded-Proto': 'https', 'Forwarded': 'for=127.0.0.1',
        'X-Forwarded-Host': 'attacker.invalid',
    }, source=source)['headers']
    assert headers['x-forwarded-for'] == expected_ip
    assert headers['x-real-ip'] == expected_ip
    assert headers['x-forwarded-proto'] == expected_scheme
    assert headers['x-forwarded-host'] == 'private-stream.mywire.org'
    assert 'forwarded' not in headers


@pytest.mark.parametrize("path,expected", [
    ("/System/Info/Public", "/jellyfin/System/Info/Public"),
    ("/jellyfin/System/Info/Public", "/jellyfin/System/Info/Public"),
    ("/web/index.html?key=fixture%2Fvalue", "/jellyfin/web/index.html?key=fixture%2Fvalue"),
    ("/jellyfin?key=fixture", "/jellyfin?key=fixture"),
])
def test_legacy_clients_and_new_prefix_reach_same_native_base(jellyfin_path_proxy, path, expected):
    value = jellyfin_path_proxy("GET", path)
    assert value["path"] == expected
    assert value["upstream"] == "jellyfin"


@pytest.mark.parametrize("prefix", ["", "/jellyfin"])
def test_deletes_are_captured_with_both_url_styles(jellyfin_path_proxy, prefix):
    value = jellyfin_path_proxy("DELETE", prefix + "/Items/fixture?api_key=fixture")
    assert value["path"] == "/jellyfin/Items/fixture?api_key=fixture"
    assert value["upstream"] == "capture"
    membership = jellyfin_path_proxy("DELETE", prefix + "/Collections/fixture/Items?ids=x")
    assert membership["path"] == "/jellyfin/Collections/fixture/Items?ids=x"
    assert membership["upstream"] == "jellyfin"


def test_media_and_websocket_headers_survive_legacy_prefix(jellyfin_path_proxy):
    headers = {
        "Host": "homelab.example.ts.net", "Range": "bytes=100-200", "If-Range": "fixture",
        "Upgrade": "websocket", "Connection": "Upgrade", "X-Emby-Token": "fixture-token",
    }
    value = jellyfin_path_proxy("GET", "/socket?api_key=fixture", headers)
    assert value["path"] == "/jellyfin/socket?api_key=fixture"
    for key in ("host", "range", "if-range", "upgrade", "x-emby-token"):
        assert value["headers"][key] == {name.lower(): val for name, val in headers.items()}[key]


@pytest.mark.parametrize('prefix', ['', '/jellyfin'])
@pytest.mark.parametrize('method,path,upstream', [
    ('GET', '/Items?Fields=MediaStreams', 'capture'),
    ('GET', '/Users/fixture/Items/film', 'capture'),
    ('GET', '/Shows/fixture/Episodes', 'capture'),
    ('GET', '/Sessions', 'capture'),
    ('GET', '/socket?api_key=fixture', 'capture'),
    ('POST', '/Items/film/PlaybackInfo', 'capture'),
    ('GET', '/Items/film/Images/Primary', 'jellyfin'),
    ('GET', '/Videos/film/stream.mkv', 'jellyfin'),
    ('POST', '/Items/film/Refresh', 'jellyfin'),
])
def test_subtitle_presentation_routes_preserve_native_delivery(
    jellyfin_path_proxy, prefix, method, path, upstream,
):
    value = jellyfin_path_proxy(method, prefix + path, {'X-Emby-Token': 'fixture-token'})
    assert value['upstream'] == upstream
    assert value['path'] == '/jellyfin' + path
    assert value['headers']['x-emby-token'] == 'fixture-token'
