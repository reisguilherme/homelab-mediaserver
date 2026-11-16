"""Execute the recipe's byte copy on real Linux files with file capabilities."""

import errno
import hashlib
import json
import os
import shlex
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "docker/core-caddy/Dockerfile"


def instructions():
    assert RECIPE.is_file(), "core Caddy derived recipe is missing"
    text = RECIPE.read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def copy_fixture(source, destination):
    run = next(row.removeprefix("RUN ") for row in instructions() if row.startswith("RUN "))
    arguments = shlex.split(run)
    assert arguments[:2] == ["/usr/bin/env", "-i"]
    assert arguments[-4:-1] == ["/bin/sh", "-eu", "-c"]
    environment = {"PATH": "/usr/bin:/bin", "SOURCE": str(source),
                   "DESTINATION": str(destination)}
    return subprocess.run(["/bin/sh", "-eu", "-c", arguments[-1]], env=environment,
                          capture_output=True, timeout=3)


def test_recipe_removes_original_before_copying_verified_bytes_without_dependencies():
    rows = instructions()
    assert rows[0:2] == ["ARG BASE_IMAGE", "FROM ${BASE_IMAGE} AS clean-binary"]
    final = rows[rows.index("FROM ${BASE_IMAGE}"):]
    copies = [row for row in rows if row.startswith(("COPY ", "ADD "))]
    assert copies == ["COPY --from=clean-binary --chown=0:0 "
                      "/.core-caddy-binary/caddy /usr/bin/caddy"]
    remove = "RUN /usr/bin/env -i PATH=/usr/bin:/bin /bin/rm -- /usr/bin/caddy"
    assert final.index(remove) < final.index(copies[0])
    assert not any(item in "\n".join(rows) for item in
                   ("apk ", "curl ", "wget ", "--chmod", "setcap", "printenv"))
    entrypoint = json.loads(next(row.removeprefix("ENTRYPOINT ") for row in final
                                if row.startswith("ENTRYPOINT ")))
    assert entrypoint[:2] == ["/usr/bin/env", "-i"]
    assert entrypoint[-1] == "/usr/bin/caddy"
    assert 'CMD ["version"]' in final and "USER 1000:1000" in final
    assert "HEALTHCHECK NONE" in final


def test_actual_copy_preserves_bytes_and_source_but_not_security_capability(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("real security.capability fixture requires root")
    source = tmp_path / "original"
    source.write_bytes(b"synthetic Caddy binary\0" * 300)
    source.chmod(0o755)
    # Linux v2 capability xattr: effective + CAP_NET_BIND_SERVICE (bit 10).
    capability = struct.pack("<IIIII", 0x02000001, 1 << 10, 0, 0, 0)
    try:
        os.setxattr(source, "security.capability", capability)
    except OSError as exc:
        if exc.errno in {errno.EPERM, errno.ENOTSUP}:
            pytest.skip("filesystem or process does not support file capabilities")
        raise
    os.setxattr(source, "user.fixture", b"do not inherit")
    original = source.stat()
    destination = tmp_path / "artifact"
    completed = copy_fixture(source, destination)
    assert completed.returncode == 0, completed.stderr.decode()
    target = destination / "caddy"
    assert (hashlib.sha256(target.read_bytes()).digest()
            == hashlib.sha256(source.read_bytes()).digest())
    assert target.stat().st_ino != source.stat().st_ino
    assert target.stat().st_mode & 0o7777 == 0o755
    assert os.listxattr(target) == []
    assert os.getxattr(source, "security.capability") == capability
    assert source.stat().st_ino == original.st_ino


def test_copy_fails_without_overwriting_existing_artifact(tmp_path):
    source = tmp_path / "original"
    source.write_bytes(b"source")
    destination = tmp_path / "existing"
    destination.mkdir()
    retained = destination / "caddy"
    retained.write_bytes(b"must preserve")
    assert copy_fixture(source, destination).returncode != 0
    assert retained.read_bytes() == b"must preserve"


def test_copy_refuses_symlink_source(tmp_path):
    source = tmp_path / "original"
    source.write_bytes(b"source")
    link = tmp_path / "link"
    link.symlink_to(source.name)
    destination = tmp_path / "artifact"
    assert copy_fixture(link, destination).returncode != 0
    assert not destination.exists()
