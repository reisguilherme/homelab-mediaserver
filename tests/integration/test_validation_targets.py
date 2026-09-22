import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="validation targets require Linux tools")


def test_make_smoke_propagates_first_shell_test_failure(tmp_path: Path) -> None:
    if shutil.which("make") is None:
        pytest.skip("make is unavailable")
    (tmp_path / "tests" / "system").mkdir(parents=True)
    (tmp_path / "tests" / "system" / "a.sh").write_text("exit 7\n", encoding="utf-8")
    (tmp_path / "tests" / "system" / "b.sh").write_text("exit 0\n", encoding="utf-8")
    shutil.copyfile("Makefile", tmp_path / "Makefile")

    result = subprocess.run(
        ["make", "smoke", "PYTHON=true"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode != 0


def test_compose_check_fails_when_docker_is_unavailable(tmp_path: Path) -> None:
    make = shutil.which("make")
    if make is None:
        pytest.skip("make is unavailable")
    shutil.copyfile("Makefile", tmp_path / "Makefile")
    result = subprocess.run(
        [make, "compose-check"],
        cwd=tmp_path,
        env={"PATH": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_smoke_requires_health_url(tmp_path: Path) -> None:
    config = tmp_path / "smoke.env"
    config.write_text("\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", "scripts/smoke.sh", "--config", str(config), "--environment", "dev"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_smoke_checks_readiness_not_only_liveness(tmp_path: Path) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl is unavailable")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/health/live" else 503)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = tmp_path / "smoke.env"
        config.write_text(
            f"HOMESERVER_HEALTH_URL=http://127.0.0.1:{server.server_port}\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            ["bash", "scripts/smoke.sh", "--config", str(config), "--environment", "dev"],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_capacity_collector_publishes_only_with_matching_mount(tmp_path: Path) -> None:
    media = tmp_path / "media"
    media.mkdir()
    output = tmp_path / "capacity.json"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    findmnt = fake_bin / "findmnt"
    findmnt.write_text(
        '#!/bin/sh\nprintf "%s\\t%s\\trw\\n" "$FAKE_TARGET" "$FAKE_UUID"\n',
        encoding="utf-8",
    )
    findmnt.chmod(0o755)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_TARGET": str(media),
        "FAKE_UUID": "uuid-test",
    }
    command = [
        "python3", "scripts/capacity-snapshot.py", "--output", str(output),
        "--media-path", str(media), "--media-uuid", "uuid-test",
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["filesystem_id"] == "uuid-test"
    assert 0 <= snapshot["free_bytes"] <= snapshot["total_bytes"]

    env["FAKE_UUID"] = "other-uuid"
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not output.exists()
