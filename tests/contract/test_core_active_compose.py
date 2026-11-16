"""Active core wiring must preserve per-role admission and private storage boundaries."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "compose.core.active.yaml"
NATIVE = {"sonarr", "radarr", "prowlarr", "bazarr", "qbittorrent", "seerr", "byparr"}
PYTHON = {"control-api", "control-worker", "download-gateway"}
WRITERS = {"sonarr", "radarr", "bazarr", "qbittorrent", "control-worker"}
CAPS = {"CHOWN", "DAC_OVERRIDE", "SETUID", "SETGID", "KILL"}


def manifest():
    assert MANIFEST.is_file(), "active core manifest is missing"
    return yaml.safe_load(MANIFEST.read_text())


def mounts(service):
    rows = service["volumes"]
    assert len({row["target"] for row in rows}) == len(rows)
    return {row["target"]: row for row in rows}


def test_active_services_are_separate_pinned_and_bounded_without_automatic_restarts():
    config = manifest()
    assert config["name"] == "homeserver-core-active"
    assert set(config["services"]) == NATIVE | PYTHON | {"qbit-monitor"}
    baseline = 0
    for role, service in config["services"].items():
        assert re.fullmatch(r"sha256:\$\{CORE_[A-Z_]+_IMAGE_ID:\?[^}]+\}", service["image"])
        assert service["pull_policy"] == "never" and "build" not in service
        assert service["restart"] == "no" and service["init"] is False
        assert service["read_only"] is True and service["healthcheck"] == {"disable": True}
        assert service["cap_drop"] == ["ALL"]
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert set(service.get("cap_add", [])) == (CAPS if role == "control-worker" else set())
        assert service["user"] == ("0:0" if role == "control-worker" else "1000:1000")
        assert not set(service) & {"env_file", "privileged", "network_mode", "pid", "devices",
                                   "extends", "container_name", "post_start"}
        assert not service.get("environment")
        assert service["memswap_limit"] == service["mem_limit"]
        assert 0 < int(service["mem_limit"].removesuffix("m")) <= 768
        assert 0 < service["pids_limit"] <= 128 and 0 < service["cpus"] <= 1
        assert service["logging"]["options"] == {"max-size": "10m", "max-file": "3"}
        if role == "byparr":
            assert service["profiles"] == ["byparr"]
        else:
            assert not service.get("profiles")
            baseline += int(service["mem_limit"].removesuffix("m"))
    assert baseline <= 3072


def test_guarded_roles_require_correct_cli_receipts_and_clean_interpreters():
    for role, service in manifest()["services"].items():
        entry = service["entrypoint"]
        assert entry[:2] == ["/usr/bin/env", "-i"]
        if role == "qbit-monitor":
            assert entry[-1] == "/usr/bin/caddy"
            assert service["command"] == ["run", "--config", "/etc/caddy/Caddyfile",
                                           "--adapter", "caddyfile"]
            continue
        variable = role.upper().replace("-", "_")
        assert service["command"] == ["--run", role, "--filesystem-uuid",
            "${CORE_FILESYSTEM_UUID:?verified ext4 UUID required}", "--activation-sha256",
            "${CORE_" + variable + "_ACTIVATION_SHA256:?audited activation receipt hash required}"]
        if role in PYTHON:
            assert "/app/.venv/bin/python" in entry
            assert entry[-1] == "/app/scripts/core-python-entrypoint.py"
            assert "PYTHONPATH=/app/services/common/src:/app/services/control/src" in entry
        else:
            interpreter = {"bazarr": "/lsiopy/bin/python3",
                           "byparr": "/app/.venv/bin/python"}.get(role, "/usr/bin/python3")
            assert entry[-4:] == [interpreter, "-I", "-B",
                                  "/usr/local/libexec/core-storage-guard.py"]
        bound = mounts(service)
        for target, source in {
            "/run/core-writer-admission": "/var/lib/homelab-storage/core-writer-admission",
            "/run/guest-boot-id": "/proc/sys/kernel/random/boot_id",
            "/run/core-filesystem-uuid": "/srv/external3/.homelab-volume-id",
            "/run/host-mountinfo": "/proc/1/mountinfo",
            "/run/core-activation": "/srv/appdata/core-activation/" + role,
        }.items():
            assert bound[target]["source"] == source and bound[target]["read_only"] is True


def test_all_binds_are_explicit_and_media_write_is_role_limited():
    for role, service in manifest()["services"].items():
        bound = mounts(service)
        for row in bound.values():
            assert row["type"] == "bind" and row["bind"]["create_host_path"] is False
            assert type(row["read_only"]) is bool
            assert row["source"] not in {"/", "/srv", "/srv/appdata", "/srv/appdata/core",
                                          ".", "/var/run/docker.sock"}
            assert "restored" not in row["source"] and "backup" not in row["source"]
        if role in WRITERS | {"control-api", "download-gateway"}:
            assert bound["/data"]["source"] == "/srv/external3/homeserver"
            assert bound["/data"]["read_only"] is (role not in WRITERS)
        else:
            assert "/data" not in bound
        if role in NATIVE - {"byparr"}:
            config_target = "/app/config" if role == "seerr" else "/config"
            assert bound[config_target]["source"] == "/srv/appdata/core/" + role
            assert bound[config_target]["read_only"] is False
        if role in {"bazarr", "qbittorrent", "byparr"}:
            assert bound["/usr/local/libexec/core-storage-guard.py"]["source"] == (
                "./scripts/core-storage-guard.py")
            assert bound["/usr/local/libexec/core-storage-guard.py"]["read_only"] is True


def test_python_state_capacity_claims_and_env_have_separate_aliases():
    for role in PYTHON:
        service = manifest()["services"][role]
        bound = mounts(service)
        assert bound["/var/lib/homeserver"]["source"] == "/srv/appdata/core/control"
        assert bound["/var/lib/homeserver"]["read_only"] is False
        expected = {
            "/run/homeserver": ("/srv/appdata/core-runtime/status", True),
            "/run/homeserver/storage.json": ("/srv/appdata/core-runtime/storage.json", True),
            "/run/core-runtime.env": (f"/srv/appdata/core-runtime/{role}.env", True),
            "/run/host-uuids": ("/dev/disk/by-uuid", True),
            "/run/core-capacity": ("/srv/appdata/core-capacity", role != "control-worker"),
            "/var/lib/homeserver/storage-placements": (
                "/srv/appdata/core-placements", role != "control-worker"),
        }
        for target, (source, readonly) in expected.items():
            assert (bound[target]["source"], bound[target]["read_only"]) == (source, readonly)
        assert not any(row["source"] == "/srv/appdata/core-runtime" for row in bound.values())


def test_guard_input_ancestors_remain_root_owned_and_tmpfs_are_bounded():
    for role, service in manifest()["services"].items():
        temporary = dict(value.split(":", 1) for value in service["tmpfs"])
        for options in temporary.values():
            assert {"nosuid", "nodev", "noexec"} <= set(options.split(","))
            assert re.search(r"(?:^|,)size=[1-9][0-9]*m(?:,|$)", options)
        if role != "qbit-monitor":
            assert {"uid=0", "gid=0", "mode=0755"} <= set(temporary["/run"].split(","))


def test_networks_and_private_ports_do_not_publish_native_qbit_or_gateway():
    config = manifest()
    assert config["networks"]["core"]["internal"] is True
    assert config["networks"]["egress"]["internal"] is False
    for role, service in config["services"].items():
        assert set(service["networks"]) == (
            {"core"} if role in {"qbit-monitor", "download-gateway"} else {"core", "egress"})
        for port in service.get("ports", []):
            assert port["host_ip"] == "${CORE_BIND_IP:?literal private VM address required}"
            assert port["protocol"] == "tcp"
    assert not config["services"]["qbittorrent"].get("ports")
    assert not config["services"]["download-gateway"].get("ports")
    assert not config["services"]["control-worker"].get("ports")
    assert not config["services"]["byparr"].get("ports")
    proxy = config["services"]["qbit-monitor"]
    assert proxy["ports"][0]["published"] == "18080"
    assert proxy["ports"][0]["target"] == 8080
    assert set(mounts(proxy)) == {"/etc/caddy/Caddyfile"}


def compose_config(tmp_path, *, missing=None):
    assert MANIFEST.is_file(), "active core manifest is missing"
    executable = shutil.which("docker")
    if not executable:
        pytest.skip("Docker Compose renderer unavailable")
    names = set(re.findall(r"\$\{([A-Z_0-9]+):\?", MANIFEST.read_text()))
    values = {name: "a" * 64 for name in names}
    values.update(CORE_FILESYSTEM_UUID="11111111-2222-3333-4444-555555555555",
                  CORE_BIND_IP="10.77.0.10")
    if missing:
        del values[missing]
    environment = tmp_path / "synthetic.env"
    environment.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    return subprocess.run([executable, "compose", "--env-file", str(environment),
                           "--file", str(MANIFEST), "config", "--format", "json"],
                          env={"PATH": os.defpath, "HOME": str(tmp_path)},
                          capture_output=True, text=True, timeout=20)


def test_real_compose_renderer_accepts_synthetic_pins_and_preserves_mount_policies(tmp_path):
    result = compose_config(tmp_path)
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["services"]["sonarr"]["image"] == "sha256:" + "a" * 64
    assert mounts(parsed["services"]["control-worker"])["/data"].get("read_only", False) is False
    assert mounts(parsed["services"]["control-api"])["/data"]["read_only"] is True
    assert not parsed["services"]["qbittorrent"].get("ports")


@pytest.mark.parametrize("missing", ["CORE_FILESYSTEM_UUID", "CORE_BIND_IP",
                                      "CORE_SONARR_IMAGE_ID", "CORE_SONARR_ACTIVATION_SHA256"])
def test_real_compose_renderer_rejects_missing_required_input(tmp_path, missing):
    result = compose_config(tmp_path, missing=missing)
    assert result.returncode != 0 and missing in result.stderr
