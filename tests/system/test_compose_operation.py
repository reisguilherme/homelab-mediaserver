"""The documented Docker entry point preserves capacity and API isolation."""

from pathlib import Path

import yaml


def test_direct_compose_builds_project_services_and_restarts_runtime():
    stack = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    services = stack["services"]
    assert services["control-api"]["build"]["dockerfile"] == "deploy/Dockerfile.control"
    assert services["telemetry"]["build"]["dockerfile"] == "deploy/Dockerfile.telemetry"
    for name, service in services.items():
        if name not in {"init", "operator"}:
            assert service["restart"] == "unless-stopped", name


def test_capacity_is_collected_by_docker_without_a_host_service():
    stack = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    services = stack["services"]
    metrics = services["host-metrics"]
    assert metrics["network_mode"] == "host"
    assert metrics["pid"] == "host"
    assert "--loop" in metrics["command"]
    assert not any("docker.sock" in str(volume) for volume in metrics["volumes"])
    assert any(
        volume["target"] == "/run/homeserver" and volume["read_only"]
        for volume in services["control-worker"]["volumes"]
    )


def test_qbittorrent_api_is_private_and_monitor_is_loopback_only():
    services = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))["services"]
    assert all(port["target"] != 8080 for port in services["qbittorrent"]["ports"])
    assert all(port["host_ip"] == "127.0.0.1" for port in services["qbit-monitor"]["ports"])
    assert all(port["target"] == 8096 for port in services["jellyfin-proxy"]["ports"])
    assert "ports" not in services["download-gateway"]
