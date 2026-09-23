from pathlib import Path


def test_production_deploy_is_manual_and_serialized() -> None:
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "scripts/deploy.sh" in workflow
    assert "tailscale" in workflow.lower()
    assert "on:\n  push:" not in workflow
    assert "manifest:" in workflow
    assert "Verify the requested commit has a successful CI run" in workflow
    assert "actions/runs?head_sha=" in workflow
    assert '.head_branch == "main"' in workflow
    assert "GH_TOKEN" in workflow
    assert 'scp "$MANIFEST_FILE"' in workflow
    assert "--manifest" in workflow


def test_ci_runs_on_pull_requests_without_production_secrets() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pull_request" in workflow
    assert "make test-unit" in workflow
    assert "make compose-check" in workflow
    assert "make smoke" in workflow
    assert "production" not in workflow.lower()


def test_capacity_snapshot_is_published_and_available_to_control_api() -> None:
    service = Path("deploy/systemd/homeserver-metrics.service").read_text(encoding="utf-8")
    compose = Path("deploy/compose.prod.yaml").read_text(encoding="utf-8")
    assert "capacity-snapshot.py" in service
    assert "HOMESERVER_MEDIA_UUID" in service
    assert "RuntimeDirectoryPreserve=yes" in service
    api = compose.split("  control-api:\n", 1)[1].split("  control-worker:\n", 1)[0]
    assert "HOMESERVER_CAPACITY_SNAPSHOT:" in api
    assert "source: /run/homeserver" in api


def test_jellyfin_is_bound_to_lan_and_tailscale_only() -> None:
    compose = Path("deploy/compose.prod.yaml").read_text(encoding="utf-8")
    jellyfin = compose.split("  jellyfin:\n", 1)[1].split("  seerr:\n", 1)[0]
    assert '"${LAN_BIND_IP:-127.0.0.1}:8096:8096"' in jellyfin
    assert '"${TAILSCALE_BIND_IP:-127.0.0.1}:8096:8096"' in jellyfin
