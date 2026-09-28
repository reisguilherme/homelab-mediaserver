import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml


def read_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def test_production_deploy_is_manual_and_serialized() -> None:
    parsed = read_yaml(".github/workflows/deploy.yml")
    assert set(parsed.get("on", parsed.get(True))) == {"workflow_dispatch"}
    assert parsed["concurrency"]["cancel-in-progress"] is False
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "scripts/deploy.sh" in workflow
    assert "tailscale" in workflow.lower()
    assert "on:\n  push:" not in workflow
    assert "release_run:" in workflow
    assert "actions/runs?head_sha=" in workflow
    assert '.head_branch == "main"' in workflow
    assert "GH_TOKEN" in workflow
    assert 'gh run download "$RELEASE_RUN"' in workflow
    assert '.path == ".github/workflows/release.yml"' in workflow
    assert "HOMESERVER_SSH_KNOWN_HOSTS" in workflow
    assert "--manifest" in workflow


def test_ci_runs_on_pull_requests_without_production_secrets() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pull_request" in workflow
    assert "make test-unit" in workflow
    assert "make compose-check" in workflow
    assert "make smoke" in workflow
    assert "${{ secrets." not in workflow
    assert "environment: production" not in workflow


def _deploy_step(name):
    steps = read_yaml(".github/workflows/deploy.yml")["jobs"]["deploy"]["steps"]
    return next(step["run"] for step in steps if step.get("name") == name)


def _release_run_selected(record, target_commit):
    """Evaluate the equality predicates sent to gh/jq against an API fixture."""
    commands = _deploy_step("Require the exact tested release")
    selectors = re.findall(r"--jq '([^']+)'", commands)
    selector = selectors[-1]
    conditions = re.findall(
        r'\.([a-z_]+)\s*==\s*(?:"([^"]+)"|env\.([A-Z_]+))', selector
    )
    assert conditions
    assert selector.count("==") == len(conditions)
    environment = {"TARGET_COMMIT": target_commit}
    return all(
        record.get(key) == (environment[variable] if variable else literal)
        for key, literal, variable in conditions
    )


def test_release_run_workflow_sha_can_differ_from_checkout_commit():
    # workflow_dispatch is recorded at the selected workflow ref. Its checkout
    # may build an older main commit explicitly selected by inputs.commit.
    record = {
        "path": ".github/workflows/release.yml",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": "b" * 40,
        "conclusion": "success",
        "id": 123,
    }
    assert _release_run_selected(record, "a" * 40)
    for key, invalid in (
        ("path", ".github/workflows/ci.yml"),
        ("event", "push"),
        ("conclusion", "failure"),
    ):
        assert not _release_run_selected(record | {key: invalid}, "a" * 40)


@pytest.mark.parametrize("invalid", [None, "commit", "checksum"])
def test_downloaded_release_content_is_validated_before_private_access(tmp_path, invalid):
    commit = "a" * 40
    output = tmp_path / "homeserver-release"
    output.mkdir()
    artifact = output / f"homeserver-{commit}.tar"
    with tarfile.open(artifact, "w") as archive:
        data = b"public fixture\n"
        member = tarfile.TarInfo("config/example.yaml")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    image_digest = "sha256:" + "c" * 64
    manifest = {
        "schema_version": 2,
        "git_commit": "b" * 40 if invalid == "commit" else commit,
        "database_schema": 1,
        "database_compatibility": {"minimum": 0, "maximum": 1},
        "config_version": 1,
        "requires_backup": True,
        "images": {"control-api": image_digest},
        "image_references": {"control-api": "fixture/control@" + image_digest},
        "config_checksums": {},
        "artifact_sha256": (
            "d" * 64 if invalid == "checksum" else hashlib.sha256(artifact.read_bytes()).hexdigest()
        ),
        "tools": {"python": "3.12", "uv": "0.8.0"},
    }
    (output / f"homeserver-{commit}.json").write_text(json.dumps(manifest))
    # Download transport is the external boundary; validate the actual workflow
    # step and validator against the files returned by it.
    tools = tmp_path / "bin"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text("#!/bin/sh\nexit 0\n")
    gh.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", _deploy_step("Download immutable release files")],
        env=os.environ
        | {
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "TARGET_COMMIT": commit,
            "RELEASE_RUN": "123",
            "RUNNER_TEMP": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert (result.returncode == 0) is (invalid is None), result.stdout + result.stderr


def test_capacity_snapshot_is_published_and_available_to_control_api() -> None:
    service = Path("deploy/systemd/homeserver-metrics.service").read_text(encoding="utf-8")
    compose = read_yaml("deploy/compose.yaml")
    assert "host-metrics.py" in service and "--env-file" in service and "--loop" in service
    api = compose["services"]["control-api"]
    assert api["environment"]["HOMESERVER_CAPACITY_SNAPSHOT"] == (
        "${HOMESERVER_CAPACITY_SNAPSHOT:-/run/homeserver/capacity.json}"
    )
    assert any(
        mount["target"] == "/run/homeserver" and mount["read_only"]
        for mount in api["volumes"]
    )


def test_jellyfin_proxy_is_bound_to_lan_and_tailscale_only() -> None:
    assert not read_yaml("deploy/compose.yaml")["services"]["jellyfin"].get("ports")
    ports = read_yaml("deploy/compose.prod.yaml")["services"]["jellyfin-proxy"]["ports"]
    assert {port["host_ip"] for port in ports} == {
        "${HOMESERVER_LAN_BIND_IP:-127.0.0.1}",
        "${HOMESERVER_TAILSCALE_BIND_IP:-127.0.0.1}",
    }
    assert all(port["target"] == 8096 for port in ports)
