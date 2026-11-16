"""No command, environment flag or playback lease can enable the prepared core."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/core-entrypoint.sh"


@pytest.mark.parametrize("arguments", [[], ["--check"], ["--resume"], ["--force"], ["/init"]])
def test_prepared_guard_always_denies_without_running_arguments(tmp_path, arguments):
    assert SCRIPT.is_file(), "core preparation guard is missing"
    marker = tmp_path / "native-started"
    receipt = tmp_path / "receipt.json"
    receipt.write_text('{"startup_allowed":true,"native_containment_verified":true}')
    lease = tmp_path / "lease"
    lease.write_text("version=1\nstate=ready\nexpires_uptime_seconds=999999999\n")
    environment = dict(
        os.environ,
        CORE_ENABLED="1",
        CORE_FORCE="true",
        CORE_ADMISSION_PATH=str(lease),
        CORE_RESTORE_RECEIPT=str(receipt),
    )
    command = ["/bin/sh", str(SCRIPT), *arguments, "/bin/sh", "-c", 'touch "$1"', "--", str(marker)]
    result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 78
    assert not marker.exists()
    assert (
        result.stderr.strip()
        == "core startup denied: writer lease and restored containment pending"
    )
    assert result.stdout == ""
