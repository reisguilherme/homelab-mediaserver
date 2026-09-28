from pathlib import Path

from homeserver_common.env import load_settings
from homeserver_common.render import render_stack, write_stack


def settings_at(tmp_path: Path, extra: str = ""):
    env = tmp_path / ".env"
    env.write_text("HOMESERVER_ENVIRONMENT=dev\n" + extra, encoding="utf-8")
    return load_settings(env, mode="dev")


def test_cpu_stack_preserves_shared_data_and_persistent_appdata(tmp_path: Path) -> None:
    settings = settings_at(tmp_path, "HOMESERVER_SERVICE_UID=1234\nHOMESERVER_SERVICE_GID=2345\n")
    services = render_stack(settings)["services"]
    assert not services["jellyfin"].get("devices")
    assert "mosquitto" not in services
    for name in ("sonarr", "radarr", "qbittorrent", "control-worker"):
        assert any(
            v["target"] == "/data" and v["source"] == str(Path(settings.media_root))
            for v in services[name]["volumes"]
        )
    for name, target in (("seerr", "/app/config"), ("prowlarr", "/config")):
        assert any(
            v["target"] == target
            and v["source"] == str(Path(settings.appdata_root) / name)
            and v["bind"]["create_host_path"] is False
            for v in services[name]["volumes"]
        )
    assert services["sonarr"]["environment"]["PUID"] == "1234"
    assert services["sonarr"]["environment"]["PGID"] == "2345"
    assert any(
        volume["target"] == "/data"
        and volume["source"] == str(Path(settings.media_root))
        and volume["read_only"] is True
        for volume in services["telemetry"]["volumes"]
    )
    assert all(service["restart"] == "no" for service in services.values())


def test_intel_device_is_added_only_when_selected(tmp_path: Path) -> None:
    settings = settings_at(
        tmp_path,
        "HOMESERVER_TRANSCODE_MODE=intel\nHOMESERVER_INTEL_RENDER_DEVICE=/dev/dri/renderD129\nHOMESERVER_RENDER_GID=109\nHOMESERVER_VIDEO_GID=44\n",
    )
    jellyfin = render_stack(settings)["services"]["jellyfin"]
    assert jellyfin["devices"] == ["/dev/dri/renderD129:/dev/dri/renderD129"]
    assert jellyfin["group_add"] == ["109", "44"]


def test_qbit_publishes_peers_and_never_native_api(tmp_path: Path) -> None:
    services = render_stack(settings_at(tmp_path, "HOMESERVER_QBIT_PEER_PORT=51413\n"))["services"]
    assert {(p["target"], p["protocol"]) for p in services["qbittorrent"]["ports"]} == {
        (51413, "tcp"),
        (51413, "udp"),
    }
    assert set(services["download-gateway"]["networks"]) == {"apps", "transfer"}
    assert not services["download-gateway"].get("ports")
    assert "egress" in services["prowlarr"]["networks"]


def test_generated_stack_treats_dollars_as_literal_and_is_private(tmp_path: Path) -> None:
    import json
    import stat

    settings = settings_at(tmp_path, 'HOMESERVER_QBIT_PASSWORD="pass$VALUE #literal"\n')
    target = tmp_path / "generated stack.json"
    write_stack(settings, target)
    rendered = json.loads(target.read_text())
    assert (
        rendered["services"]["download-gateway"]["environment"]["HOMESERVER_QBIT_PASSWORD"]
        == "pass$$VALUE #literal"
    )
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_runtime_readiness_checks_worker_progress_and_ready_http(tmp_path: Path) -> None:
    services = render_stack(settings_at(tmp_path))["services"]
    assert "WorkerHeartbeatStore" in services["control-worker"]["healthcheck"]["test"][3]
    assert "/health/ready" in services["control-api"]["healthcheck"]["test"][3]
    assert "/health/ready" in services["telemetry"]["healthcheck"]["test"][3]
    gateway_check = services["download-gateway"]["healthcheck"]["test"]
    assert gateway_check[:3] == ["CMD", "python", "-c"]
    assert "http://127.0.0.1:8081/health/live" in gateway_check[3]
    assert "/health/ready" not in gateway_check[3]
    assert services["operator"]["profiles"] == ["operator"]
    assert services["operator"]["user"] == "0:0"


def test_http_processes_use_the_selected_log_level(tmp_path: Path) -> None:
    services = render_stack(settings_at(tmp_path, "HOMESERVER_LOG_LEVEL=WARNING\n"))["services"]
    for name in ("control-api", "download-gateway", "telemetry"):
        command = services[name]["command"]
        assert command[command.index("--log-level") + 1] == "warning"


def test_telemetry_uses_the_configured_capacity_identity_and_age(tmp_path: Path) -> None:
    settings = settings_at(tmp_path, "HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS=7\n")
    telemetry = render_stack(settings)["services"]["telemetry"]
    assert telemetry["environment"]["HOMESERVER_MEDIA_ROOT"] == "/data"
    assert telemetry["environment"]["HOMESERVER_MEDIA_UUID"] == settings.media_uuid
    assert float(telemetry["environment"]["HOMESERVER_CAPACITY_SNAPSHOT_MAX_AGE_SECONDS"]) == 7
