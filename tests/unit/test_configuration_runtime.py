import os
import subprocess
import sys
from pathlib import Path

import pytest

from homeserver_common.env import serialize_env


def _preferences(tmp_path, **changes):
    path = tmp_path / ".env"
    values = {
        "HOMESERVER_QBIT_PASSWORD": "a $HOME # ' \" password",
        "HOMESERVER_DOWNLOAD_MAX_ACTIVE": "3",
        "HOMESERVER_SUBTITLE_LANGUAGES": "en-US,pt-BR",
        "HOMESERVER_WORKER_INTERVAL_SECONDS": "12",
        "HOMESERVER_LOG_LEVEL": "WARNING",
        **changes,
    }
    path.write_text(serialize_env(values))
    return path


def _process_environment(path):
    environment = dict(os.environ)
    environment.update(
        HOMESERVER_ENV_PATH=str(path),
        HOMESERVER_ENV_MODE="dev",
        PYTHONPATH=str(Path(__file__).parents[2] / "services/common/src"),
    )
    return environment


def test_runtime_preserves_literal_preferences_and_container_wiring(tmp_path):
    from homeserver_common.runtime import apply_runtime_configuration

    path = _preferences(tmp_path, HOMESERVER_MEDIA_ROOT="/legacy/host-only")
    original = {
        "PATH": "/bin",
        "HOMESERVER_MEDIA_ROOT": "/data",
        "HOMESERVER_DB_PATH": "/var/lib/homeserver/control.sqlite",
        "HOMESERVER_QBIT_URL": "http://qbittorrent:8080",
        "HOMESERVER_QBIT_PASSWORD": '"incorrect interpolated value"',
    }
    result = apply_runtime_configuration(path, "dev", original)
    assert result["HOMESERVER_QBIT_PASSWORD"] == "a $HOME # ' \" password"
    assert result["HOMESERVER_DOWNLOAD_MAX_ACTIVE"] == "3"
    assert result["HOMESERVER_SUBTITLE_LANGUAGES"] == "en-US,pt-BR"
    assert result["HOMESERVER_WORKER_INTERVAL"] == "12"
    assert result["HOMESERVER_MEDIA_ROOT"] == "/data"
    assert result["HOMESERVER_DB_PATH"] == original["HOMESERVER_DB_PATH"]
    assert result["HOMESERVER_QBIT_URL"] == original["HOMESERVER_QBIT_URL"]
    assert result["PATH"] == "/bin"
    assert original["HOMESERVER_QBIT_PASSWORD"] == '"incorrect interpolated value"'
    assert "HOMESERVER_ENVIRONMENT" not in result
    assert "HOMESERVER_INSTALL_ROOT" not in result


def test_runtime_executes_child_with_typed_values_without_shell_expansion(tmp_path):
    path = _preferences(tmp_path)
    environment = _process_environment(path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "homeserver_common.runtime",
            sys.executable,
            "-c",
            "import os; print(os.environ['HOMESERVER_QBIT_PASSWORD']); "
            "print(os.environ['HOMESERVER_SUBTITLE_LANGUAGES'])",
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["a $HOME # ' \" password", "en-US,pt-BR"]


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (
            ["uvicorn", "app:app", "--log-level", "info"],
            ["uvicorn", "app:app", "--log-level", "warning"],
        ),
        (
            ["uvicorn", "app:app", "--log-level=info"],
            ["uvicorn", "app:app", "--log-level=warning"],
        ),
    ],
)
def test_runtime_log_level_uses_preference_for_child_command(
    tmp_path, monkeypatch, arguments, expected
):
    from homeserver_common import runtime

    path = _preferences(tmp_path)
    monkeypatch.setenv("HOMESERVER_ENV_PATH", str(path))
    monkeypatch.setenv("HOMESERVER_ENV_MODE", "dev")
    called = []
    monkeypatch.setattr(runtime.os, "execvpe", lambda *args: called.append(args))
    assert runtime.main(arguments) == 0
    assert called[0][1] == expected
    assert called[0][2]["HOMESERVER_LOG_LEVEL"] == "WARNING"


def test_runtime_validation_failure_does_not_expose_value_or_traceback(tmp_path):
    path = _preferences(tmp_path, HOMESERVER_DOWNLOAD_MAX_ACTIVE="credential-should-not-leak")
    environment = _process_environment(path)
    result = subprocess.run(
        [sys.executable, "-m", "homeserver_common.runtime", "a-command-that-must-not-run"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "HOMESERVER_DOWNLOAD_MAX_ACTIVE" in result.stderr
    assert "credential-should-not-leak" not in result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout == ""


def test_missing_env_stops_startup_without_file_path_or_traceback(tmp_path):
    path = tmp_path / "not-mounted.env"
    result = subprocess.run(
        [sys.executable, "-m", "homeserver_common.runtime", "a-command-that-must-not-run"],
        env=_process_environment(path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stderr == "Runtime configuration error: .env unavailable\n"
    assert result.stdout == ""
