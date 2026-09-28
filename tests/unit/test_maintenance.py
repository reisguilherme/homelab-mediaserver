import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeserver_common import backup
from homeserver_common.env import load_settings
from homeserver_common.settings import Settings


def fixture(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("HOMESERVER_ENVIRONMENT=dev\n")
    dev = load_settings(env, mode="dev")
    settings = Settings(dict(dev.values) | {"environment": "prod"})
    compose = Path(settings.install_root) / "shared/compose.json"
    compose.parent.mkdir(parents=True)
    compose.write_text(json.dumps({"services": {"control-worker": {}}}))
    events = []
    state = {"running": True, "guard": True}

    def checked(command, **kwargs):
        events.append(command)
        if command[:2] == ["docker", "compose"]:
            if "stop" in command:
                state["running"] = False
            if "ps" in command:
                rows = (
                    [{"Service": "control-worker", "State": "running"}] if state["running"] else []
                )
                return SimpleNamespace(stdout=json.dumps(rows))
        return SimpleNamespace(stdout="")

    def guard(*args, **kwargs):
        events.append(["guard"])
        if not state["guard"]:
            raise ValueError("UUID unavailable")

    monkeypatch.setattr(backup, "run_checked", checked)
    monkeypatch.setattr(backup, "preflight", guard)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    return settings, events, state


def test_production_maintenance_stops_containers_before_capture(tmp_path, monkeypatch):
    settings, events, state = fixture(tmp_path, monkeypatch)
    with backup.maintenance(settings, "backup"):
        assert state["running"] is False
        events.append(["capture"])
    stopped = next(
        index
        for index, command in enumerate(events)
        if command[:2] == ["docker", "compose"] and "stop" in command
    )
    assert stopped < events.index(["capture"])
    assert ["systemctl", "start", "homeserver-stack.service"] in events


def test_lost_uuid_during_maintenance_does_not_restart_writers(tmp_path, monkeypatch):
    settings, events, state = fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="UUID"):
        with backup.maintenance(settings, "backup"):
            state["guard"] = False
    assert ["systemctl", "start", "homeserver-stack.service"] not in events
    assert (Path(settings.run_root) / "maintenance").exists()
    assert state["running"] is False


def test_native_configuration_can_keep_apis_alive_while_admission_is_blocked(tmp_path, monkeypatch):
    settings, events, state = fixture(tmp_path, monkeypatch)
    with backup.maintenance(settings, "configure", stop_stack=False):
        assert state["running"] is True
        assert (Path(settings.run_root) / "maintenance").exists()
    assert not any("stop" in command and command[:2] == ["docker", "compose"] for command in events)
