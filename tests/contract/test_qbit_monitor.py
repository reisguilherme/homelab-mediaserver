"""Exercise the deployed Caddy routes with a local authenticated upstream.

Use HOMESERVER_TEST_CADDY or an installed caddy binary. No Docker, network,
qBittorrent data, or production credentials are required by these tests.
"""

import http.client
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SESSION = "SID=monitor-fixture"
LOGIN_BODY = b"username=monitor-fixture&password=local-fixture"


@dataclass
class MonitorFixture:
    port: int
    requests: list[tuple[str, str, dict[str, str], bytes]]

    def request(self, method: str, target: str, *, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            # http.client preserves encoded paths and dot segments for bypass probes.
            connection.request(method, target, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()


@pytest.fixture(scope="module")
def monitor(tmp_path_factory):
    caddy = os.environ.get("HOMESERVER_TEST_CADDY") or shutil.which("caddy")
    if caddy is None:
        pytest.skip("Caddy runtime unavailable; set HOMESERVER_TEST_CADDY for real proxy tests")

    requests = []

    class Upstream(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_HEAD(self):
            self.respond()

        def do_PUT(self):
            self.respond()

        def do_PATCH(self):
            self.respond()

        def do_DELETE(self):
            self.respond()

        def do_OPTIONS(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {name.lower(): value for name, value in self.headers.items()}
            requests.append((self.command, self.path, headers, body))
            path = self.path.partition("?")[0]
            logged_in = headers.get("cookie") == SESSION
            if path == "/api/v2/auth/login":
                status, response_body = (200, b"Ok.") if body == LOGIN_BODY else (403, b"Fails.")
            elif path.startswith("/api/") and not logged_in:
                status, response_body = 403, b"Forbidden"
            else:
                # Unknown/mutation routes deliberately succeed if Caddy lets them through.
                status, response_body = 200, self.path.encode()
            self.send_response(status)
            self.send_header("X-Qbit-Upstream", "fixture")
            self.send_header("Content-Security-Policy", "default-src 'self'")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            if path == "/api/v2/auth/login" and status == 200:
                self.send_header("Set-Cookie", f"{SESSION}; HttpOnly; Path=/; SameSite=Strict")
            elif path == "/api/v2/auth/logout" and status == 200:
                self.send_header("Set-Cookie", "SID=; Max-Age=0; HttpOnly; Path=/")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response_body)

        def log_message(self, *_):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    temp = tmp_path_factory.mktemp("qbit-monitor")
    config = (ROOT / "deploy/qbit-monitor/Caddyfile").read_text(encoding="utf-8")
    # Only replace the listener and upstream; exercise the deployed routing verbatim.
    config = config.replace(":8080 {", f"http://127.0.0.1:{port} {{", 1)
    config = config.replace("qbittorrent:8080", f"127.0.0.1:{upstream.server_port}")
    config_path = temp / "Caddyfile"
    config_path.write_text("{\n    admin off\n}\n" + config, encoding="utf-8")
    environment = os.environ | {
        "XDG_CONFIG_HOME": str(temp / "config"),
        "XDG_DATA_HOME": str(temp / "data"),
    }
    with (temp / "caddy.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [caddy, "run", "--config", str(config_path), "--adapter", "caddyfile"],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    pytest.fail(f"Caddy fixture failed to start: {log.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail("Caddy fixture did not open its loopback listener")
            yield MonitorFixture(port, requests)
        finally:
            process.terminate()
            process.wait(timeout=5)
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=5)


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/torrents/add",
        "/api/v2/torrents/delete",
        "/api/v2/torrents/start",
        "/api/v2/torrents/resume",
        "/api/v2/torrents/stop",
        "/api/v2/torrents/pause",
        "/api/v2/torrents/setShareLimits",
        "/api/v2/torrents/setLocation",
        "/api/v2/torrents/recheck",
        "/api/v2/app/setPreferences",
        "/api/v2/app/shutdown",
        "/api/v2/transfer/setUploadLimit",
        "/api/v2/search/start",
        "/api/v2/rss/addFeed",
        "/api/v2/torrents/futureAction",
        "/api/v3/torrents/info",
    ],
)
def test_mutations_and_unknown_api_never_reach_qbit(monitor, method, path):
    before = len(monitor.requests)
    status, headers, _ = monitor.request(method, path, headers={"Cookie": SESSION})
    assert status in {403, 404}
    assert "X-Qbit-Upstream" not in headers
    assert len(monitor.requests) == before


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/app/version",
        "/api/v2/app/webapiVersion",
        "/api/v2/app/buildInfo",
        "/api/v2/app/preferences",
        "/api/v2/sync/maindata?rid=0",
        "/api/v2/sync/torrentPeers?hash=abc&rid=0",
        "/api/v2/transfer/info",
        "/api/v2/transfer/speedLimitsMode",
        "/api/v2/transfer/downloadLimit",
        "/api/v2/transfer/uploadLimit",
        "/api/v2/torrents/info?filter=all&sort=name&reverse=false",
        "/api/v2/torrents/properties?hash=abc",
        "/api/v2/torrents/trackers?hash=abc",
        "/api/v2/torrents/webseeds?hash=abc",
        "/api/v2/torrents/files?hash=abc",
        "/api/v2/torrents/pieceStates?hash=abc",
        "/api/v2/torrents/pieceHashes?hash=abc",
        "/api/v2/torrents/downloadLimit?hashes=abc%7Cdef",
        "/api/v2/torrents/uploadLimit?hashes=abc",
        "/api/v2/torrents/categories",
        "/api/v2/torrents/tags",
        "/api/v2/log/main?normal=true&last_known_id=-1",
        "/api/v2/log/peers?last_known_id=-1",
    ],
)
def test_authenticated_monitoring_gets_reach_qbit_unchanged(monitor, path):
    status, headers, body = monitor.request("GET", path, headers={"Cookie": SESSION})
    assert status == 200
    assert headers["X-Qbit-Upstream"] == "fixture"
    assert body == path.encode()
    assert monitor.requests[-1][:2] == ("GET", path)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/index.html",
        "/favicon.ico",
        "/css/style.css",
        "/scripts/client.js?v=fixture",
        "/scripts/lib/mootools-core.js",
        "/images/qbittorrent-tray.svg",
        "/images/flags/us.png",
        "/views/properties.html",
    ],
)
def test_native_ui_get_assets_are_available_without_bypassing_auth(monitor, path):
    status, headers, _ = monitor.request("GET", path)
    assert status == 200
    assert headers["X-Qbit-Upstream"] == "fixture"
    assert "cookie" not in monitor.requests[-1][2]
    assert "authorization" not in monitor.requests[-1][2]


def test_monitor_preserves_native_auth_session_and_security_headers(monitor):
    status, headers, body = monitor.request("GET", "/api/v2/torrents/info")
    assert (status, body) == (403, b"Forbidden")
    assert headers["X-Qbit-Upstream"] == "fixture"
    assert "cookie" not in monitor.requests[-1][2]
    assert "authorization" not in monitor.requests[-1][2]

    origin = f"http://127.0.0.1:{monitor.port}"
    status, headers, body = monitor.request(
        "POST",
        "/api/v2/auth/login",
        body=LOGIN_BODY,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Origin": origin},
    )
    assert (status, body) == (200, b"Ok.")
    assert headers["Set-Cookie"] == f"{SESSION}; HttpOnly; Path=/; SameSite=Strict"
    assert monitor.requests[-1][3] == LOGIN_BODY
    assert "authorization" not in monitor.requests[-1][2]

    status, headers, _ = monitor.request(
        "GET",
        "/api/v2/sync/maindata?rid=0",
        headers={"Cookie": SESSION, "Origin": origin, "Referer": f"{origin}/"},
    )
    assert status == 200
    assert headers["Content-Security-Policy"] == "default-src 'self'"
    assert headers["X-Frame-Options"] == "SAMEORIGIN"
    upstream_headers = monitor.requests[-1][2]
    assert upstream_headers["cookie"] == SESSION
    assert upstream_headers["origin"] == origin
    assert upstream_headers["referer"] == f"{origin}/"
    assert upstream_headers["host"] == f"127.0.0.1:{monitor.port}"
    assert "authorization" not in upstream_headers

    status, headers, _ = monitor.request(
        "POST", "/api/v2/auth/logout", headers={"Cookie": SESSION}
    )
    assert status == 200
    assert headers["Set-Cookie"] == "SID=; Max-Age=0; HttpOnly; Path=/"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v2/auth/login"),
        ("GET", "/api/v2/auth/logout"),
        ("POST", "/api/v2/torrents/info"),
        ("POST", "/scripts/client.js"),
        ("HEAD", "/api/v2/torrents/info"),
        ("PUT", "/api/v2/torrents/info"),
        ("PATCH", "/api/v2/torrents/info"),
        ("DELETE", "/api/v2/torrents/info"),
        ("OPTIONS", "/api/v2/auth/login"),
        ("GET", "/unlisted.html"),
    ],
)
def test_only_explicit_methods_and_paths_are_forwarded(monitor, method, path):
    before = len(monitor.requests)
    status, _, _ = monitor.request(method, path, headers={"Cookie": SESSION})
    assert status in {403, 404}
    assert len(monitor.requests) == before


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "path",
    [
        "//api/v2/torrents/info",
        "/api/v2/torrents/./info",
        "/api/v2/torrents/delete/../info",
        "/scripts/../api/v2/torrents/delete",
        "/api/v2/auth/login/../logout",
        "/api%2fv2%2ftorrents%2finfo",
        "/%61pi/v2/torrents/info",
        "/api/v2/torrents/%69nfo",
        "/api/v2/torrents%2fdelete",
        "/scripts/%2e%2e/api/v2/torrents/delete",
        "/scripts/%252e%252e/api/v2/torrents/delete",
        "/scripts/%2fapi/v2/torrents/delete",
        "/api/v2/torrents/info%3f/../delete",
        r"/api/v2/torrents\info",
        "/api/v2/torrents/%5cinfo",
    ],
)
def test_ambiguous_raw_paths_cannot_bypass_the_allowlist(monitor, method, path):
    before = len(monitor.requests)
    status, _, _ = monitor.request(method, path, headers={"Cookie": SESSION})
    assert status in {403, 404}
    assert len(monitor.requests) == before
