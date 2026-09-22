from pathlib import Path


def test_production_deploy_is_manual_and_serialized() -> None:
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in workflow
    assert "concurrency:" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "scripts/deploy.sh" in workflow
    assert "tailscale" in workflow.lower()
    assert "on:\n  push:" not in workflow


def test_ci_runs_on_pull_requests_without_production_secrets() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pull_request" in workflow
    assert "make test-unit" in workflow
    assert "make compose-check" in workflow
    assert "production" not in workflow.lower()
