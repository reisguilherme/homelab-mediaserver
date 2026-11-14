"""Real Caddy regression: a root-path SID must not shadow a fresh qBit SID."""

import os
import shutil
import socket
import subprocess
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cookie_router(tmp_path_factory):
    caddy = os.environ.get("HOMESERVER_TEST_CADDY") or shutil.which("caddy")
    if not caddy:
        pytest.skip("Caddy unavailable; set HOMESERVER_TEST_CADDY for real cookie regression")

    class NativeQBit(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            cookies = [
                pair.strip().split("=", 1)[1]
                for pair in self.headers.get("Cookie", "").split(";")
                if pair.strip().startswith("SID=")
            ]
            authenticated = bool(cookies) and cookies[-1] == "fresh-fixture"
            login = self.command == "POST" and self.path == "/api/v2/auth/login"
            valid = parse_qs(body.decode()).get("password") == ["valid-fixture"]
            status, payload, new_session = 200, b"fixture", False
            if login:
                payload, new_session = (b"Ok.", True) if valid else (b"Fails.", False)
            elif self.path == "/api/v2/app/preferences":
                status, payload = (
                    (200, b'{"max_active_downloads":25}') if authenticated else (403, b"Forbidden")
                )
            elif self.path == "/":
                payload = (
                    b'<div id="transfer-list"></div>'
                    if authenticated
                    else b'<form id="loginform"></form>'
                )
            self.send_response(status)
            if new_session:
                self.send_header(
                    "Set-Cookie", "SID=fresh-fixture; HttpOnly; Path=/; SameSite=Strict"
                )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), NativeQBit)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    temp = tmp_path_factory.mktemp("qbit-cookie-router")
    config = (ROOT / "deploy/service-proxy/Caddyfile").read_text()
    config = config.replace(":80 {", f"http://:{port} {{\n bind 127.0.0.1", 1)
    for backend in (
        "jellyfin-proxy:8096",
        "sonarr:8989",
        "radarr:7878",
        "prowlarr:9696",
        "bazarr:6767",
        "qbit-monitor:8080",
        "telemetry:8080",
    ):
        config = config.replace(backend, f"127.0.0.1:{upstream.server_port}")
    config_path = temp / "Caddyfile"
    if config.lstrip().startswith("{"):
        config = config.replace("{", "{\n admin off", 1)
    else:
        config = "{\n admin off\n}\n" + config
    config_path.write_text(config)
    environment = os.environ | {
        "XDG_CONFIG_HOME": str(temp / "config"),
        "XDG_DATA_HOME": str(temp / "data"),
    }
    with (temp / "caddy.log").open("w+") as log:
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
                    pytest.fail(f"Fixture Caddy startup failed: {log.read()}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                pytest.fail("Fixture Caddy listener timeout")
            yield f"http://127.0.0.1:{port}"
        finally:
            process.terminate()
            process.wait(timeout=5)
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)


def legacy_client(base):
    client = httpx.Client(base_url=base, trust_env=False, follow_redirects=False)
    client.cookies.set("SID", "stale-root-fixture", domain="127.0.0.1", path="/")
    client.cookies.set("other", "preserved-fixture", domain="127.0.0.1", path="/")
    return client


def login(client, *, password="valid-fixture"):
    return client.post(
        "/qbittorrent/api/v2/auth/login", data={"username": "root", "password": password}
    )


def test_scoped_login_retires_root_sid_and_allows_next_read(cookie_router):
    with legacy_client(cookie_router) as client:
        response = login(client)
        assert response.status_code == 200 and response.text == "Ok."
        observed = client.get("/qbittorrent/api/v2/app/preferences")
        assert observed.status_code == 200
        assert observed.json()["max_active_downloads"] == 25
        sids = [cookie for cookie in client.cookies.jar if cookie.name == "SID"]
        assert [(cookie.path, cookie.value) for cookie in sids] == [
            ("/qbittorrent/", "fresh-fixture")
        ]
        assert client.cookies.get("other") == "preserved-fixture"
        assert "loginform" not in client.get("/qbittorrent/").text


def test_login_returns_separate_native_and_root_expiration_cookies(cookie_router):
    with legacy_client(cookie_router) as client:
        response = login(client)
        headers = response.headers.get_list("set-cookie")
        assert len(headers) == 2
        cookies = []
        for header in headers:
            parsed = SimpleCookie()
            parsed.load(header)
            cookies.extend(parsed.values())
        fresh = next(cookie for cookie in cookies if cookie.value == "fresh-fixture")
        expired = next(cookie for cookie in cookies if cookie.value == "")
        assert fresh["path"] == "/qbittorrent/" and not fresh["max-age"]
        assert fresh["httponly"] and fresh["samesite"] == "Strict"
        assert expired["path"] == "/" and expired["max-age"] == "0"
        assert expired["httponly"] and expired["samesite"] == "Strict"


def test_invalid_login_does_not_expire_root_cookie(cookie_router):
    with legacy_client(cookie_router) as client:
        response = login(client, password="invalid-fixture")
        assert response.status_code == 200 and response.text == "Fails."
        assert not response.headers.get_list("set-cookie")
        assert client.cookies.get("SID") == "stale-root-fixture"


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/qbittorrent/"),
        ("GET", "/qbittorrent/api/v2/auth/login"),
        ("POST", "/qbittorrent/api/v2/torrents/stop"),
        ("POST", "/radarr/api/v3/command"),
    ],
)
def test_other_requests_do_not_expire_root_cookie(cookie_router, method, path):
    with legacy_client(cookie_router) as client:
        response = client.request(method, path)
        assert not response.headers.get_list("set-cookie")
        assert client.cookies.get("SID") == "stale-root-fixture"


def test_existing_scoped_session_reads_without_receiving_expiration(cookie_router):
    with httpx.Client(base_url=cookie_router, trust_env=False) as client:
        client.cookies.set("SID", "fresh-fixture", domain="127.0.0.1", path="/qbittorrent/")
        response = client.get("/qbittorrent/api/v2/app/preferences")
        assert response.status_code == 200
        assert not response.headers.get_list("set-cookie")
