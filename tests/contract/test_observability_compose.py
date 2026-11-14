"""The optional metrics stack stays isolated from media and administration."""

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from homeserver_telemetry.metrics import create_metrics_app

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/metrics"
METRICS = {
    "homeserver_snapshot_valid",
    "homeserver_snapshot_fresh",
    "homeserver_snapshot_age_seconds",
    "homeserver_host_cpu_percent",
    "homeserver_host_ram_percent",
    "homeserver_host_cpu_celsius",
    "homeserver_host_uptime_seconds",
    "homeserver_network_receive_bytes_per_second",
    "homeserver_network_transmit_bytes_per_second",
    "homeserver_pool_ready",
    "homeserver_pool_total_bytes",
    "homeserver_pool_free_bytes",
    "homeserver_pool_used_bytes",
}


def read_yaml(path):
    assert path.is_file(), f"Missing optional observability configuration: {path.name}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def read_stack():
    return read_yaml(ROOT / "compose.observability.yaml")


def documented_observability_commands():
    guide = (ROOT / "docs/observability.md").read_text(encoding="utf-8")
    return [
        shlex.split(command)
        for command in re.findall(r"docker compose[^`\n]+", guide)
        if "-f compose.observability.yaml " in command
    ]


def test_documented_commands_pin_project_even_when_main_dotenv_sets_another_name():
    commands = documented_observability_commands()
    assert len(commands) >= 6
    for command in commands:
        assert "-p" in command, "The private .env can override the YAML project name"
        assert command[command.index("-p") + 1] == "homeserver-observability"


def test_documented_config_keeps_project_with_conflicting_environment(tmp_path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI unavailable; config-only validation needs no daemon")
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("HOMESERVER_", "HOMELAB_", "COMPOSE_", "DOCKER_"))
    }
    environment.update(
        COMPOSE_PROJECT_NAME="main-project-from-process",
        DOCKER_CONFIG=str(tmp_path / "docker-config"),
    )
    version = subprocess.run(
        [docker, "compose", "version"], env=environment,
        capture_output=True, text=True, timeout=30,
    )
    if version.returncode:
        pytest.skip("Docker Compose CLI unavailable in this environment")
    fixture = tmp_path / "fixture.env"
    fixture.write_text(
        "COMPOSE_PROJECT_NAME=main-project-from-fixture\n",
        encoding="utf-8",
    )
    command = next(cmd for cmd in documented_observability_commands() if "config" in cmd)
    command[0] = docker
    command[command.index("--env-file") + 1] = str(fixture)
    command.remove("--quiet")
    result = subprocess.run(
        [*command, "--format", "json"], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert rendered["name"] == "homeserver-observability"
    assert set(rendered["services"]) == {"homeserver-metrics"}
    assert rendered["networks"]["metrics"]["external"] is True
    assert rendered["networks"]["metrics"]["name"] == "homelab-metrics"


def test_source_activation_rebuilds_shared_telemetry_image_before_recreation():
    guide = (ROOT / "docs/observability.md").read_text(encoding="utf-8")
    assert "homeserver-telemetry:dev" in guide
    assert guide.index("`build telemetry`") < guide.index(
        "`up -d --no-deps --force-recreate host-metrics`"
    )


def test_stack_has_only_private_exporter_on_external_platform_metrics_network():
    stack = read_stack()
    assert stack["name"] == "homeserver-observability"
    assert set(stack["services"]) == {"homeserver-metrics"}
    assert stack["networks"] == {"metrics": {"external": True, "name": "homelab-metrics"}}
    metrics = stack["services"]["homeserver-metrics"]
    assert metrics["networks"] == {"metrics": {"aliases": ["media-metrics"]}}
    assert not metrics.get("ports")
    assert "network_mode" not in metrics and "pid" not in metrics
    assert not metrics.get("depends_on") and not metrics.get("privileged")
    assert metrics["cap_drop"] == ["ALL"]
    assert metrics["security_opt"] == ["no-new-privileges:true"]
    assert metrics["mem_limit"] == "128m"


def test_exporter_uses_existing_build_with_only_dedicated_read_only_snapshots():
    stack = read_stack()
    metrics = stack["services"]["homeserver-metrics"]
    assert metrics["build"] == {"context": ".", "dockerfile": "deploy/Dockerfile.telemetry"}
    assert metrics["command"] == [
        "python", "-m", "uvicorn", "homeserver_telemetry.metrics:app",
        "--host", "0.0.0.0", "--port", "8090",
    ]
    assert metrics["environment"] == {
        "HOMESERVER_METRICS_SNAPSHOT_DIR": "/run/homeserver-metrics",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    assert metrics["user"] == "65534:65534"
    assert metrics["read_only"] is True
    assert metrics["volumes"] == [{
        "type": "bind",
        "source": "${HOMESERVER_OBSERVABILITY_DIR:-/srv/appdata/observability}/snapshots",
        "target": "/run/homeserver-metrics",
        "read_only": True,
        "bind": {"create_host_path": False},
    }]


def test_stack_cannot_receive_sensitive_mounts_or_whole_environment():
    stack = read_stack()
    allowed = {"${HOMESERVER_OBSERVABILITY_DIR:-/srv/appdata/observability}/snapshots"}
    for service in stack["services"].values():
        assert not service.get("env_file")
        assert not service.get("devices")
        assert not service.get("volumes_from")
        assert not service.get("secrets")
        assert not any(key.endswith("_FILE") for key in service.get("environment", {}))
        for mount in service["volumes"]:
            assert mount["type"] == "bind"
            assert mount["source"] in allowed
            assert mount["bind"]["create_host_path"] is False
            if mount["target"] not in {"/prometheus", "/var/lib/grafana"}:
                assert mount["read_only"] is True


def test_dashboard_covers_exporter_contract_and_does_not_turn_missing_into_zero():
    path = DEPLOY / "dashboards/homeserver.json"
    assert path.is_file(), "Missing provisioned dashboard"
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    assert dashboard["uid"] == "homeserver-overview"
    targets = [target for panel in dashboard["panels"] for target in panel.get("targets", [])]
    expressions = [target["expr"] for target in targets]
    referenced = set(re.findall(r"homeserver_[a-z_]+", " ".join(expressions)))
    assert referenced == METRICS
    assert any('up{job="homeserver"}' in expression for expression in expressions)
    assert not any(re.search(r"\bor\b|vector\s*\(|sum\s*\(", expr) for expr in expressions)
    for panel in dashboard["panels"]:
        if not panel.get("targets"):
            continue
        assert panel["datasource"]["uid"] == "homelab-prometheus"
        assert panel["fieldConfig"]["defaults"]["noValue"] == "Sem dados"
        assert not panel.get("transformations")
        if panel["type"] == "stat":
            assert all(target["instant"] for target in panel["targets"])
            assert panel["options"]["reduceOptions"]["calcs"] == ["last"]
        if panel["type"] == "timeseries":
            assert panel["fieldConfig"]["defaults"]["custom"]["spanNulls"] is False


def test_dashboard_metrics_exist_in_real_exporter_output(tmp_path):
    now = 1_800_000_000.0
    snapshots = {
        "host": {
            "generated_at": now,
            "host": {
                "cpu_percent": 25, "ram_percent": 40,
                "cpu_celsius": 50, "uptime_seconds": 600,
            },
            "network": {"rx_bps": 123, "tx_bps": 456},
        },
        "capacity": {
            "measured_at": now,
            "pools": [{
                "pool_id": "ssd", "filesystem_id": "fixture-opaque-identity",
                "state": "ready", "measured_at": now,
                "total_bytes": 1000, "free_bytes": 400, "used_bytes": 600,
            }],
        },
    }
    for source, payload in snapshots.items():
        (tmp_path / f"{source}.json").write_text(json.dumps(payload), encoding="utf-8")
    app = create_metrics_app(snapshot_dir=tmp_path, clock=lambda: now)
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    exported = set(re.findall(r"^(homeserver_[a-z_]+)[{ ]", response.text, re.MULTILINE))
    assert METRICS <= exported
