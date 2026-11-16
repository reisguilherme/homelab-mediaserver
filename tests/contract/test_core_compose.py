"""The prepared core cannot inherit the monolith or start a native writer."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "sonarr",
    "radarr",
    "prowlarr",
    "bazarr",
    "qbittorrent",
    "seerr",
    "control-api",
    "control-worker",
    "download-gateway",
    "qbit-monitor",
}


def manifest():
    path = ROOT / "compose.core.yaml"
    assert path.is_file(), "standalone prepared core manifest is missing"
    return yaml.safe_load(path.read_text())


def test_core_has_explicit_images_and_budget_with_guest_headroom():
    config = manifest()
    assert set(config["services"]) == EXPECTED
    assert config["name"] == "homeserver-core"
    total = 0
    for service in config["services"].values():
        assert service["image"].startswith("sha256:${CORE_")
        assert ":?" in service["image"]
        assert service["pull_policy"] == "never"
        assert "build" not in service
        assert service["mem_limit"].endswith("m")
        total += int(service["mem_limit"][:-1])
        assert service["memswap_limit"] == service["mem_limit"]
        assert 0 < service["pids_limit"] <= 256
    assert total <= 3072


def test_every_explicit_service_start_is_guarded_and_inherited_healthchecks_disabled():
    for name, service in manifest()["services"].items():
        assert service["entrypoint"] == ["/bin/sh", "/usr/local/bin/core-entrypoint.sh"]
        assert service["command"] == [name]
        assert service["restart"] == "no"
        assert service["init"] is False
        assert service["healthcheck"] == {"disable": True}
        assert service["profiles"] == ["core-prepared"]
        assert service["user"] == "1000:1000"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert not service.get("privileged")
        assert not set(service) & {"devices", "network_mode", "pid", "ipc", "env_file", "extends"}
        assert not service.get("environment")


def test_prepared_binds_are_readonly_explicit_and_do_not_create_host_paths():
    for service in manifest()["services"].values():
        sources = set()
        for mount in service["volumes"]:
            assert mount["type"] == "bind"
            assert mount["read_only"] is True
            assert mount["bind"]["create_host_path"] is False
            assert mount["source"].startswith(
                ("/srv/appdata/core/", "./scripts/core-", "/srv/external3/homeserver")
            )
            assert mount["source"] not in {"/srv", "/", "/srv/appdata", "/srv/appdata/core"}
            sources.add(mount["source"])
        assert "./scripts/core-entrypoint.sh" in sources
    text = (ROOT / "compose.core.yaml").read_text()
    for forbidden in ("docker.sock", "compose.yaml", "/run/storage-admission", "/init"):
        assert forbidden not in text


def test_native_qbit_is_internal_and_other_ports_require_private_address():
    config = manifest()
    assert "ports" not in config["services"]["qbittorrent"]
    assert "ports" not in config["services"]["control-worker"]
    for service in config["services"].values():
        assert service["networks"] == ["core"]
        for port in service.get("ports", []):
            assert port["host_ip"] == "${CORE_BIND_IP:?literal private VM address required}"
    assert config["networks"]["core"]["internal"] is True


def test_example_has_no_mutable_defaults_or_enable_switch():
    path = ROOT / ".env.core.example"
    assert path.is_file()
    text = path.read_text()
    assignments = dict(
        line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#")
    )
    assert set(assignments) == {"CORE_BIND_IP"} | {
        "CORE_" + name.upper().replace("-", "_") + "_IMAGE_ID" for name in EXPECTED
    }
    assert all(value == "" for value in assignments.values())
