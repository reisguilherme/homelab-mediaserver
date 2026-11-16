"""Standalone edge wiring and real Nginx/ASGI public policy fixtures."""

import base64
import hashlib
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
import yaml

ROOT = Path(__file__).resolve().parents[2]


def config():
    path = ROOT / "compose.edge.yaml"
    assert path.exists(), "standalone edge manifest is missing"
    return yaml.safe_load(path.read_text())


def test_edge_has_only_bounded_public_services_and_no_core_authority():
    data = config()
    assert set(data["services"]) == {"edge-tls", "public-proxy", "public-security"}
    total = 0
    for name, service in data["services"].items():
        assert service["pull_policy"] == "never" and service["image"].startswith("sha256:${EDGE_")
        assert service["user"] == "1000:1000" and service["read_only"] is True
        assert service["cap_drop"] == ["ALL"] and not service.get("cap_add")
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["entrypoint"][:2] == ["/usr/bin/env", "-i"]
        assert service["restart"] == "no" and service["healthcheck"] == {"disable": True}
        assert service["logging"] == {"driver": "none"}
        assert service["memswap_limit"] == service["mem_limit"]
        total += int(service["mem_limit"].removesuffix("m"))
        assert not set(service) & {"env_file", "environment", "network_mode", "privileged",
                                   "devices", "pid", "build", "extends"}
        for mount in service["volumes"]:
            assert mount["type"] == "bind" and mount["bind"]["create_host_path"] is False
            assert mount["source"].startswith(("./deploy/edge/", "/srv/appdata/edge/"))
        if name != "edge-tls":
            assert not service.get("ports")
    assert total <= 448
    ports = data["services"]["edge-tls"]["ports"]
    assert {(p["published"], p["target"], p["protocol"]) for p in ports} == {
        ("80", 8080, "tcp"), ("443", 8443, "tcp")}
    assert all(p["host_ip"] == "${EDGE_BIND_IP:?verified private edge address required}"
               for p in ports)
    certificate = next((mount for mount in data["services"]["edge-tls"]["volumes"]
        if mount["target"] == "/etc/homelab-tls/streaming"), None)
    assert certificate is not None, "edge TLS must consume the separately delivered certificate"
    assert certificate["source"] == "/srv/appdata/edge/tls/streaming"
    assert certificate["read_only"] is True
    guard = data["services"]["public-security"]
    assert "HOMESERVER_JELLYFIN_URL=http://172.28.64.20:8096/jellyfin" in guard["entrypoint"]
    assert "HOMESERVER_PUBLIC_ACCESS_TRUSTED_PROXY_IPS=172.30.248.3" in guard["entrypoint"]
    assert "--no-proxy-headers" in guard["command"] and "--no-access-log" in guard["command"]
    assert guard["command"][0] == "homeserver_control.api.public_access:create_app"
    assert data["networks"]["edge"]["internal"] is False


def test_edge_compose_renders_without_private_environment(tmp_path):
    config()
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker Compose is not installed")
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "EDGE_BIND_IP": "192.0.2.30"}
    environment.update({"EDGE_" + role + "_IMAGE_ID": "a" * 64
                        for role in ("CADDY", "NGINX", "PYTHON")})
    result = subprocess.run([docker, "compose", "--env-file", "/dev/null", "-f",
                             str(ROOT / "compose.edge.yaml"), "config", "--format", "json"],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered["services"]["edge-tls"]["ports"][0]["host_ip"] == "192.0.2.30"
    assert all(s["image"] == "sha256:" + "a" * 64 for s in rendered["services"].values())


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_port(port, process=None):
    until = time.monotonic() + 8
    while time.monotonic() < until:
        assert process is None or process.poll() is None, "fixture proxy stopped"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.02)
    pytest.fail("fixture listener did not start")


def test_edge_caddy_preserves_external_authority_and_redirect(tmp_path):
    caddy = os.environ.get("HOMESERVER_TEST_CADDY") or shutil.which("caddy")
    if not caddy:
        pytest.skip("real Caddy fixture binary is required")
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is required for a disposable TLS certificate")
    certificate, key = tmp_path / "fullchain.pem", tmp_path / "privkey.pem"
    generated = subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(certificate), "-days", "1",
        "-subj", "/CN=streaming.greis.dev.br",
        "-addext", "subjectAltName=DNS:streaming.greis.dev.br"],
        capture_output=True, text=True, timeout=15)
    assert generated.returncode == 0, generated.stderr
    key.chmod(0o600)
    fixture_config = tmp_path / "Caddyfile"
    fixture_config.write_text((ROOT / "deploy/edge/Caddyfile").read_text().replace(
        "/etc/homelab-tls/streaming", str(tmp_path)))
    adapted = subprocess.run([caddy, "adapt", "--config", str(fixture_config),
                              "--adapter", "caddyfile"], capture_output=True, text=True, timeout=10)
    assert adapted.returncode == 0, adapted.stderr
    data = json.loads(adapted.stdout)
    certificates = data["apps"].get("tls", {}).get("certificates", {})
    assert certificates.get("load_files") == [{
        "certificate": str(certificate), "key": str(key), "tags": ["cert0"]}]
    assert not certificates.get("automate"), "the edge must not request ACME certificates"
    servers = data["apps"]["http"]["servers"]
    assert {address for server in servers.values() for address in server["listen"]} == {
        ":8080", ":8443"}
    https = next(server for server in servers.values() if ":8443" in server["listen"])
    assert https["automatic_https"]["disable_redirects"] is True

    def handlers(value):
        if isinstance(value, dict):
            if "handler" in value:
                yield value
            for child in value.values():
                yield from handlers(child)
        elif isinstance(value, list):
            for child in value:
                yield from handlers(child)

    proxy = next(item for item in handlers(https) if item["handler"] == "reverse_proxy")
    headers = {key.lower(): value for key, value in proxy["headers"]["request"]["set"].items()}
    assert headers["host"] == ["streaming.greis.dev.br:10443"]
    assert headers["x-forwarded-host"] == ["streaming.greis.dev.br:10443"]
    assert headers["x-forwarded-port"] == ["10443"]
    # Exercise the delivered leaf and redirect locally without real ACME or upstream traffic.
    http_server = next(server for server in servers.values() if ":8080" in server["listen"])
    port, tls_port = free_port(), free_port()
    http_server["listen"] = [f"127.0.0.1:{port}"]
    http_server["automatic_https"] = {"disable": True}
    https["listen"] = [f"127.0.0.1:{tls_port}"]
    data["apps"]["http"]["http_port"] = port
    data["apps"]["http"]["https_port"] = tls_port
    path = tmp_path / "caddy.json"
    path.write_text(json.dumps(data))
    process = subprocess.Popen([caddy, "run", "--config", str(path)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)})
    try:
        wait_port(port, process)
        wait_port(tls_port, process)
        context = ssl.create_default_context(cafile=str(certificate))
        with socket.create_connection(("127.0.0.1", tls_port), timeout=5) as connection:
            with context.wrap_socket(connection, server_hostname="streaming.greis.dev.br") as tls:
                assert ("DNS", "streaming.greis.dev.br") in tls.getpeercert()["subjectAltName"]
        for host in ("streaming.greis.dev.br", "streaming.greis.dev.br:10443"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request("GET", "/jellyfin/web/index.html?fixture=1", headers={
                    "Host": host, "X-Forwarded-Host": "attacker.example",
                    "X-Forwarded-Port": "443"})
                response = conn.getresponse()
                assert response.status == 308
                assert response.getheader("Location") == (
                    "https://streaming.greis.dev.br:10443/jellyfin/web/index.html?fixture=1")
                response.read()
            finally:
                conn.close()
    finally:
        process.terminate()
        process.wait(5)
    # Missing delivery is a startup error, never a reason to fall back to HTTP-01.
    key.unlink()
    checked = subprocess.run([caddy, "validate", "--config", str(path)],
                             capture_output=True, text=True, timeout=10)
    assert checked.returncode != 0
    assert "privkey.pem" in checked.stderr


@contextmanager
def proxy_fixture(tmp_path):
    path = ROOT / "deploy/edge/nginx.conf"
    assert path.exists(), "standalone edge Nginx configuration is missing"
    nginx = os.environ.get("HOMESERVER_TEST_NGINX") or shutil.which("nginx")
    if not nginx:
        pytest.skip("real Nginx fixture binary is required")
    from homeserver_control.api.public_access import PublicAccessSettings, create_app

    calls = []

    class Native(BaseHTTPRequestHandler):
        def handle_request(self):
            calls.append((self.command, self.path))
            headers = dict(self.headers)
            status = 200
            payload = {"path": self.path, "headers": headers}
            if self.path == "/jellyfin/Users/Me":
                auth = self.headers.get("Authorization", "")
                payload = {"Id": "viewer", "Policy": {"IsDisabled": False,
                    "IsAdministrator": "admin-token" in auth}}
                if "viewer-token" not in auth and "admin-token" not in auth:
                    status = 401
            elif self.path == "/jellyfin/Users/AuthenticateByName":
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payload = {"AccessToken": body["Username"] + "-token"}
            elif self.path.startswith("/jellyfin/socket"):
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                key = self.headers["Sec-WebSocket-Key"]
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                self.send_header("Sec-WebSocket-Accept", accept.decode())
                self.end_headers()
                return
            elif self.headers.get("Range"):
                status = 206
            blob = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)

        do_GET = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

        def log_message(self, *_args):
            pass

    native = ThreadingHTTPServer(("127.0.0.1", 0), Native)
    native_thread = threading.Thread(target=native.serve_forever, daemon=True)
    native_thread.start()
    native_port = native.server_port
    guard_port, proxy_port = free_port(), free_port()
    app = create_app(PublicAccessSettings(jellyfin_url=f"http://127.0.0.1:{native_port}/jellyfin",
        seerr_url="http://127.0.0.1:9", state_path=tmp_path / "limits.sqlite3",
        trusted_proxy_ips=("127.0.0.1",)))
    guard = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=guard_port,
        access_log=False, log_level="critical", proxy_headers=False))
    guard_thread = threading.Thread(target=guard.run, daemon=True)
    guard_thread.start()
    process = None
    try:
        wait_port(guard_port)
        text = path.read_text()
        for old, new in {
            "pid /tmp/nginx.pid;": f"pid {tmp_path}/nginx.pid;",
            "listen 8097": f"listen 127.0.0.1:{proxy_port}",
            "172.30.248.2": "127.0.0.2",
            "172.30.248.4:8080": f"127.0.0.1:{guard_port}",
            "172.28.64.20:8096": f"127.0.0.1:{native_port}",
        }.items():
            text = text.replace(old, new)
        text = text.replace("http {", f"http {{\nclient_body_temp_path {tmp_path}/body;\n"
            f"proxy_temp_path {tmp_path}/proxy;\nfastcgi_temp_path {tmp_path}/fastcgi;\n"
            f"scgi_temp_path {tmp_path}/scgi;\nuwsgi_temp_path {tmp_path}/uwsgi;")
        fixture_config = tmp_path / "nginx.conf"
        fixture_config.write_text(text)
        command = [nginx, "-p", str(tmp_path), "-c", str(fixture_config)]
        checked = subprocess.run([*command, "-t"], capture_output=True, text=True, timeout=5)
        assert checked.returncode == 0, checked.stderr
        process = subprocess.Popen([*command, "-g", "daemon off;"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_port(proxy_port, process)

        def request(method, path, *, headers=None, body=None, source="127.0.0.2"):
            connection = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=5,
                                                    source_address=(source, 0))
            try:
                connection.request(method, path, body=body, headers={
                    "Host": "streaming.greis.dev.br:10443",
                    "Origin": "https://streaming.greis.dev.br:10443",
                    "X-Forwarded-For": "203.0.113.9",
                    **(headers or {})})
                response = connection.getresponse()
                return SimpleNamespace(status=response.status, body=response.read(),
                                       headers={k.lower(): v for k, v in response.getheaders()})
            finally:
                connection.close()

        def stop_guard():
            guard.should_exit = True
            guard_thread.join(5)
            assert not guard_thread.is_alive()

        yield SimpleNamespace(request=request, calls=calls, stop_guard=stop_guard)
    finally:
        if process:
            process.terminate()
            process.wait(5)
        guard.should_exit = True
        guard_thread.join(5)
        native.shutdown()
        native.server_close()
        native_thread.join(5)


@pytest.mark.parametrize("path,method", [("/System/Restart", "POST"), ("/Auth/Keys", "GET"),
    ("/Items/id", "DELETE"), ("/jellyfin/Items/%2e%2e/Users", "GET")])
def test_edge_admin_operations_never_reach_playback(tmp_path, path, method):
    with proxy_fixture(tmp_path) as proxy:
        result = proxy.request(method, path, headers={"X-Emby-Token": "admin-token"})
        assert result.status == 403 and proxy.calls == []


def test_edge_viewer_login_range_and_elevated_session_denial(tmp_path):
    with proxy_fixture(tmp_path) as proxy:
        login = proxy.request("POST", "/Users/AuthenticateByName", body=json.dumps({
            "Username": "viewer", "Pw": "fixture"}), headers={"Content-Type": "application/json"})
        assert login.status == 200 and "Secure" in login.headers["set-cookie"]
        assert proxy.request("POST", "/jellyfin/Users/AuthenticateByName", body=json.dumps({
            "Username": "admin", "Pw": "fixture"}),
            headers={"Content-Type": "application/json"}).status == 403
        assert proxy.request("GET", "/Items", headers={"X-Emby-Token": "admin-token"}).status == 403
        result = proxy.request("GET", "/Videos/id/stream.mkv?api_key=viewer-token", headers={
            "Range": "bytes=0-99", "Forwarded": "for=198.51.100.1", "X-Real-IP": "198.51.100.2",
            "X-Forwarded-Host": "attacker.example:8443", "X-Forwarded-Port": "8443"})
        assert result.status == 206
        headers = {k.lower(): v for k, v in json.loads(result.body)["headers"].items()}
        assert headers["x-real-ip"] == "203.0.113.9" and "forwarded" not in headers
        assert headers["range"] == "bytes=0-99" and "viewer-token" in headers["authorization"]
        assert headers["host"] == "streaming.greis.dev.br:10443"
        assert headers["x-forwarded-host"] == "streaming.greis.dev.br:10443"
        assert headers["origin"] == "https://streaming.greis.dev.br:10443"
        assert headers["x-forwarded-proto"] == "https" and headers["x-forwarded-port"] == "10443"


def test_edge_websocket_basepath_and_guard_failure(tmp_path):
    with proxy_fixture(tmp_path) as proxy:
        result = proxy.request("GET", "/jellyfin/socket?api_key=viewer-token", headers={
            "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        assert result.status == 101 and result.headers["upgrade"].lower() == "websocket"
        proxy.stop_guard()
        count = len(proxy.calls)
        assert proxy.request("GET", "/Items",
                             headers={"X-Emby-Token": "viewer-token"}).status == 500
        assert len(proxy.calls) == count


def test_edge_rejects_direct_non_tls_peer_and_unknown_host(tmp_path):
    with proxy_fixture(tmp_path) as proxy:
        assert proxy.request("GET", "/web/index.html", source="127.0.0.1").status == 403
        with pytest.raises(http.client.RemoteDisconnected):
            proxy.request("GET", "/web/index.html", headers={"Host": "other.example"})
        assert proxy.calls == []
