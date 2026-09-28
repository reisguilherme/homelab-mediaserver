import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def _compose_services(overlay: str) -> dict:
    docker = shutil.which("docker.exe") or shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is unavailable")
    if (
        subprocess.run(
            [docker, "compose", "version"], capture_output=True, text=True, check=False
        ).returncode
        != 0
    ):
        pytest.skip("Docker Compose is unavailable")

    env = os.environ.copy()
    env.update(
        HOMESERVER_ADMIN_TOKEN="test-admin",
        HOMESERVER_CSRF_TOKEN="test-csrf",
        HOMESERVER_COLLECTOR_TOKEN="test-collector",
        HOMESERVER_ARR_TOKEN="test-arr",
        HOMESERVER_MEDIA_UUID="test-media-uuid",
        HOMESERVER_JELLYFIN_API_KEY="test-jellyfin-key",
        HOMESERVER_RADARR_URL="http://radarr:7878",
        HOMESERVER_RADARR_API_KEY="test-radarr-key",
        HOMESERVER_SONARR_URL="http://sonarr:8989",
        HOMESERVER_SONARR_API_KEY="test-sonarr-key",
        HOMESERVER_LAN_BIND_IP="192.0.2.10",
        HOMESERVER_TAILSCALE_BIND_IP="100.64.1.2",
    )
    if os.name != "nt" and docker.endswith(".exe"):
        # WSL must explicitly pass fixture variables to the Windows Compose CLI.
        variables = (
            "HOMESERVER_ADMIN_TOKEN",
            "HOMESERVER_CSRF_TOKEN",
            "HOMESERVER_COLLECTOR_TOKEN",
            "HOMESERVER_ARR_TOKEN",
            "HOMESERVER_MEDIA_UUID",
            "HOMESERVER_JELLYFIN_API_KEY",
            "HOMESERVER_RADARR_URL",
            "HOMESERVER_RADARR_API_KEY",
            "HOMESERVER_SONARR_URL",
            "HOMESERVER_SONARR_API_KEY",
            "HOMESERVER_LAN_BIND_IP",
            "HOMESERVER_TAILSCALE_BIND_IP",
        )
        env["WSLENV"] = ":".join([env.get("WSLENV", ""), *(f"{name}/w" for name in variables)])
    result = subprocess.run(
        [
            docker,
            "compose",
            "-f",
            "deploy/compose.yaml",
            "-f",
            overlay,
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)["services"]


@pytest.mark.parametrize(
    ("overlay", "expected_ports"),
    [
        ("deploy/compose.dev.yaml", {("127.0.0.1", 18096, 8096)}),
        (
            "deploy/compose.prod.yaml",
            {("192.0.2.10", 8096, 8096), ("100.64.1.2", 8096, 8096)},
        ),
    ],
)
def test_only_proxy_publishes_jellyfin_port(overlay: str, expected_ports: set[tuple]) -> None:
    services = _compose_services(overlay)
    jellyfin = services["jellyfin"]
    proxy = services["jellyfin-proxy"]

    assert not jellyfin.get("ports")
    assert {
        (port["host_ip"], int(port["published"]), port["target"]) for port in proxy["ports"]
    } == expected_ports
    assert "apps" in proxy["networks"]
    assert "edge_control" in proxy["networks"]
    assert any(
        mount["target"] == "/data/media" and mount["read_only"] for mount in jellyfin["volumes"]
    )
    assert any(
        mount["target"] == "/etc/nginx/nginx.conf" and mount["read_only"]
        for mount in proxy["volumes"]
    )


def test_production_delete_services_receive_their_upstream_settings() -> None:
    services = _compose_services("deploy/compose.prod.yaml")
    control_api = services["control-api"]["environment"]
    worker = services["control-worker"]["environment"]

    for service in (control_api, worker):
        assert service["HOMESERVER_MEDIA_UUID"] == "test-media-uuid"
        assert service["HOMESERVER_JELLYFIN_URL"] == "http://jellyfin:8096"
    for name, expected in (
        ("HOMESERVER_RADARR_URL", "http://radarr:7878"),
        ("HOMESERVER_RADARR_API_KEY", "test-radarr-key"),
        ("HOMESERVER_SONARR_URL", "http://sonarr:8989"),
        ("HOMESERVER_SONARR_API_KEY", "test-sonarr-key"),
    ):
        assert control_api[name] == expected
    assert worker["HOMESERVER_JELLYFIN_API_KEY"] == "test-jellyfin-key"


def test_items_delete_route_covers_normalized_paths() -> None:
    config = Path("deploy/jellyfin-proxy/nginx.conf").read_text(encoding="utf-8")
    route = re.search(r"(?m)^\s*~\*(\S+)\s+(\S+);$", config)
    assert route is not None
    pattern, upstream = route.groups()
    assert upstream == "control-api:8080"
    assert re.search(r"(?m)^\s*default jellyfin:8096;$", config)
    assert "proxy_pass http://$jellyfin_destination$request_uri;" in config
    matcher = re.compile(pattern, re.IGNORECASE)

    for request in (
        "DELETE:/Items",
        "DELETE:/Items/abc",
        "DELETE:/items/abc",
        "DELETE:/jellyfin/Items/abc",
    ):
        assert matcher.search(request), request
    for request in ("GET:/Items/abc", "POST:/Items/abc", "DELETE:/Other/abc"):
        assert not matcher.search(request), request


def test_proxy_preserves_playback_range_and_websocket_headers() -> None:
    config = Path("deploy/jellyfin-proxy/nginx.conf").read_text(encoding="utf-8")
    headers = dict(re.findall(r"proxy_set_header\s+(\S+)\s+(\S+);", config))

    assert "proxy_http_version 1.1;" in config
    assert headers["Upgrade"] == "$http_upgrade"
    assert headers["Connection"] == "$connection_upgrade"
    assert headers["Range"] == "$http_range"
    assert headers["If-Range"] == "$http_if_range"
    assert headers["Authorization"] == "$http_authorization"
    assert headers["X-Emby-Token"] == "$http_x_emby_token"
    assert "proxy_pass_request_headers on;" in config
    assert "log_format homeserver" in config
    assert "$request_method $uri $server_protocol" in config
    assert "$request_method $request_uri" not in config
