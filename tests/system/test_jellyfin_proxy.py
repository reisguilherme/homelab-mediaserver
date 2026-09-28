import re
from pathlib import Path

import yaml


def _compose_services():
    return yaml.safe_load(Path("compose.yaml").read_text())["services"]


def test_only_proxy_publishes_jellyfin_port():
    services = _compose_services()
    assert "ports" not in services["jellyfin"]
    assert {(p["host_ip"], int(p["published"]), p["target"])
            for p in services["jellyfin-proxy"]["ports"]} == {("0.0.0.0",8096,8096)}
    assert any(v["target"] == "/data/media" and v["read_only"]
               for v in services["jellyfin"]["volumes"])


def test_delete_services_load_credentials_from_env_and_use_internal_urls():
    services = _compose_services()
    for name in ("control-api", "control-worker"):
        service = services[name]
        assert service["environment"]["HOMESERVER_JELLYFIN_URL"] == "http://jellyfin:8096"
        assert service["environment"]["HOMESERVER_RADARR_URL"] == "http://radarr:7878"
        assert service["environment"]["HOMESERVER_SONARR_URL"] == "http://sonarr:8989"
        assert "HOMESERVER_JELLYFIN_API_KEY" not in service["environment"]
        assert service["entrypoint"] == ["python", "-m", "homeserver_common.runtime"]
        assert any(v["target"] == "/project" and v["read_only"] for v in service["volumes"])


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
