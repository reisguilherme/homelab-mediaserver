import json
from pathlib import Path
from types import SimpleNamespace

from homeserver_common.supervision import StackSupervisor


def _supervisor(tmp_path, *, status="exited", guard_ok=True):
    settings = SimpleNamespace(
        install_root=str(tmp_path),
        run_root=str(tmp_path / "run"),
        appdata_root=str(tmp_path / "data"),
        instance_name="fixture",
        supervisor_restart_window_seconds=300,
        supervisor_max_restarts=2,
    )
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "compose.json").write_text(
        json.dumps({"services": {"worker": {}, "operator": {"profiles": ["operator"]}}})
    )
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            stdout=json.dumps({"Service": "worker", "State": status, "Health": ""})
        )

    def guard(*args, **kwargs):
        if not guard_ok:
            raise ValueError("UUID unavailable")

    return StackSupervisor(settings, runner=runner, guard=guard, clock=lambda: 1000), calls


def test_maintenance_never_restarts_or_checks_docker(tmp_path):
    supervisor, calls = _supervisor(tmp_path)
    marker = Path(supervisor.settings.run_root) / "maintenance"
    marker.parent.mkdir()
    marker.touch()
    assert supervisor.run_once()["status"] == "maintenance"
    assert calls == []


def test_restart_budget_survives_new_supervisor_and_excludes_operator(tmp_path):
    supervisor, calls = _supervisor(tmp_path)
    assert supervisor.run_once()["services"] == {"worker": "restart_requested"}
    assert supervisor.run_once()["services"] == {"worker": "restart_requested"}
    another = StackSupervisor(
        supervisor.settings, runner=supervisor.runner, guard=supervisor.guard, clock=lambda: 1001
    )
    assert another.run_once()["services"] == {"worker": "restart_limit"}
    assert not any("operator" in command for command in calls)
    assert sum("up" in command for command in calls) == 2


def test_lost_storage_stops_stack_and_blocks_automatic_restart(tmp_path):
    supervisor, calls = _supervisor(tmp_path, guard_ok=False)
    assert supervisor.run_once()["status"] == "blocked"
    assert calls[-1][-1] == "stop"
    assert supervisor.run_once()["status"] == "blocked"
    assert len(calls) == 1


def test_existing_operation_lock_prevents_restarts(tmp_path):
    import fcntl

    supervisor, calls = _supervisor(tmp_path)
    run = Path(supervisor.settings.run_root)
    run.mkdir()
    with (run / "operation.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert supervisor.run_once()["status"] == "maintenance"
    assert not calls


def test_failed_storage_stop_is_retried_on_next_run(tmp_path):
    import pytest

    supervisor, calls = _supervisor(tmp_path, guard_ok=False)
    runner = supervisor.runner
    attempts = 0

    def fail_once(command, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("stop unavailable")
        return runner(command, **kwargs)

    supervisor.runner = fail_once
    with pytest.raises(RuntimeError):
        supervisor.run_once()
    assert supervisor.run_once()["writers_stopped"]
    assert attempts == 2
