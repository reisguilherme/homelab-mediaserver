"""Opt-in sandbox regression on disposable Linux/WSL, never on the Proxmox host."""

import configparser
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    os.environ.get("HOMESERVER_TEST_SYSTEMD_SANDBOX") != "1"
    or os.geteuid() != 0
    or shutil.which("systemd-run") is None,
    reason="explicit disposable Linux root/systemd sandbox fixture opt-in required",
)
def test_playback_namespace_hides_runtime_and_limits_writes_to_textfile_directory():
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    config.read(ROOT / "deploy/playback/homeserver-playback-health.service")
    properties = dict(config["Service"])
    # Exercise every actual sandbox property. Substitute only the workload and
    # its private environment/output for synthetic paths in this disposable test.
    properties.pop("ExecStart")
    properties.pop("EnvironmentFile")
    with (
        tempfile.TemporaryDirectory(prefix="playback-health-sandbox-", dir="/var/lib") as directory,
        tempfile.NamedTemporaryFile(prefix="playback-health-hidden-", dir="/run") as sentinel,
    ):
        root = Path(directory)
        output = root / "textfile"
        output.mkdir()
        outside = root / "outside"
        properties["ReadWritePaths"] = str(output)
        probe = """
import errno, json, os, pathlib, sys
sentinel, output, outside = map(pathlib.Path, sys.argv[1:])
assert not sentinel.exists(), 'original /run is visible'
for path in (pathlib.Path('/run') / sentinel.name, outside):
    try:
        path.write_text('forbidden')
    except OSError as error:
        assert error.errno in (errno.EROFS, errno.EACCES, errno.EPERM), error
    else:
        raise AssertionError('write escaped textfile directory')
(output / 'fixture.prom').write_text('fixture_up 1\\n')
print(json.dumps({'runtime_hidden': True, 'runtime_readonly': True, 'write_scope': True}))
"""
        command = [
            "systemd-run",
            "--unit=playback-health-fixture-" + uuid.uuid4().hex,
            "--wait",
            "--pipe",
            "--collect",
            *[f"--property={key}={value}" for key, value in properties.items()],
            "/usr/bin/python3",
            "-I",
            "-B",
            "-c",
            probe,
            sentinel.name,
            str(output),
            str(outside),
        ]
        result = subprocess.run(command, text=True, capture_output=True, timeout=20, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {
            "runtime_hidden": True,
            "runtime_readonly": True,
            "write_scope": True,
        }
        assert (output / "fixture.prom").read_text() == "fixture_up 1\n"
        assert not outside.exists()
        assert Path(sentinel.name).exists(), "the host namespace must remain unchanged"
