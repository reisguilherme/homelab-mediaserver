import importlib.util
from pathlib import Path

import pytest


def validator():
    path = Path(__file__).resolve().parents[2] / "scripts/validate-fresh-stack.py"
    spec = importlib.util.spec_from_file_location("fresh_validator", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_configuration_is_isolated_and_disables_external_sources(tmp_path):
    module = validator()
    env_file, settings = module.prepare_fixture(tmp_path / "fresh")
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert settings.environment == "dev"
    assert not settings.media_uuid
    assert not settings.byparr_enabled
    assert not settings.prowlarr_indexers
    assert not settings.bazarr_providers
    assert settings.transcode_mode == "cpu"
    assert settings.qbit_peer_bind_ip == "127.0.0.1"
    for key in ("media_root", "appdata_root", "run_root", "transcode_root"):
        assert Path(getattr(settings, key)).is_relative_to(tmp_path / "fresh")
    with pytest.raises(ValueError):
        module.prepare_fixture(tmp_path / "fresh")


def test_operator_outcomes_require_full_set_and_empty_reapply_diff():
    module = validator()
    rows = [
        dict(service=name, status="verified", changes=[], message="") for name in module.SERVICES
    ]
    module.check_outcomes(rows, unchanged=True)
    with pytest.raises(RuntimeError):
        module.check_outcomes(rows[:-1], unchanged=True)
    rows[0]["changes"] = [{"key": "fixture-drift"}]
    with pytest.raises(RuntimeError):
        module.check_outcomes(rows, unchanged=True)


def test_owned_project_cleanup_runs_after_failed_validation(monkeypatch, tmp_path):
    module = validator()
    calls = []
    monkeypatch.setattr(module, "require_docker", lambda: None)
    monkeypatch.setattr(module, "prepare_fixture", lambda root: (root / ".env", object()))
    monkeypatch.setattr(module.Runner, "__init__", lambda self, settings, env_file, timeout: None)
    monkeypatch.setattr(
        module.Runner, "execute", lambda self: (_ for _ in ()).throw(RuntimeError("private"))
    )
    monkeypatch.setattr(module.Runner, "cleanup", lambda self: calls.append("owned-cleanup"))
    assert module.main(["--root", str(tmp_path / "fresh")]) == 4
    assert calls == ["owned-cleanup"]


def test_unavailable_docker_reports_not_run_and_never_prepares_state(monkeypatch, capsys):
    module = validator()

    def missing():
        raise module.DependencyError("private detail")

    monkeypatch.setattr(module, "require_docker", missing)
    monkeypatch.setattr(
        module, "prepare_fixture", lambda root: pytest.fail("must not create fixture")
    )
    assert module.main([]) == 3
    output = capsys.readouterr().err
    assert "not run" in output
    assert "private detail" not in output


def test_runtime_readiness_accepts_completed_init_and_healthy_running_services():
    module = validator()
    rows = [
        {
            "Config": {"Labels": {"com.docker.compose.service": "init"}},
            "State": {"Status": "exited", "ExitCode": 0},
        },
        {
            "Config": {"Labels": {"com.docker.compose.service": "sonarr"}},
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
        },
    ]
    assert module.containers_ready(rows)
    rows[0]["State"]["ExitCode"] = 1
    assert not module.containers_ready(rows)
    rows[0]["State"]["ExitCode"] = 0
    rows[1]["State"]["Status"] = "exited"
    assert not module.containers_ready(rows)
