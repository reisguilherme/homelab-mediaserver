import json
import os
import shutil
import subprocess

import pytest


@pytest.mark.parametrize(
    ("lan_ip", "peer_port", "expected_ip", "expected_port"),
    [
        (None, None, "127.0.0.1", 6881),
        ("192.0.2.20", "51413", "192.0.2.20", 51413),
    ],
)
def test_production_publishes_only_qbittorrent_peer_port_on_lan_ip(
    lan_ip: str | None,
    peer_port: str | None,
    expected_ip: str,
    expected_port: int,
) -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose is unavailable")
    if subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    ).returncode != 0:
        pytest.skip("Docker Compose is unavailable")

    env = os.environ.copy()
    env.update(
        HOMESERVER_ADMIN_TOKEN="test-admin",
        HOMESERVER_CSRF_TOKEN="test-csrf",
        HOMESERVER_COLLECTOR_TOKEN="test-collector",
        HOMESERVER_ARR_TOKEN="test-arr",
    )
    env.pop("LAN_BIND_IP", None)
    env.pop("QBITTORRENT_PEER_PORT", None)
    if lan_ip is not None:
        env["LAN_BIND_IP"] = lan_ip
    if peer_port is not None:
        env["QBITTORRENT_PEER_PORT"] = peer_port

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/compose.yaml",
            "-f",
            "deploy/compose.prod.yaml",
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    qbit = json.loads(result.stdout)["services"]["qbittorrent"]

    assert {
        (port["host_ip"], int(port["published"]), port["target"], port["protocol"])
        for port in qbit["ports"]
    } == {
        (expected_ip, expected_port, expected_port, "tcp"),
        (expected_ip, expected_port, expected_port, "udp"),
    }
    assert qbit["environment"]["TORRENTING_PORT"] == str(expected_port)
