"""Update public source on the authenticated Python base without changing dependencies."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "docker/core-python/Dockerfile"


def instructions():
    assert RECIPE.is_file(), "core Python derived recipe is missing"
    text = RECIPE.read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def test_python_image_requires_verified_base_and_does_not_resolve_dependencies():
    rows = instructions()
    assert rows[:2] == ["ARG BASE_IMAGE", "FROM ${BASE_IMAGE}"]
    assert not any(row.startswith("ARG BASE_IMAGE=") for row in rows)
    for row in rows:
        assert not any(value in row for value in ("pip install", "uv sync", "apt-get", "apk add",
                                                 "curl ", "wget ", "--chmod", "printenv"))
    assert all(row.startswith("RUN /usr/bin/env -i ") for row in rows if row.startswith("RUN "))
    assert not any(row.startswith(("VOLUME ", "EXPOSE ", "ONBUILD ")) for row in rows)


def test_python_image_copies_only_current_public_code_and_required_helpers():
    copies = [row for row in instructions() if row.startswith(("COPY ", "ADD "))]
    assert copies == [
        "COPY --chown=0:0 services/common/src /app/services/common/src",
        "COPY --chown=0:0 services/control/src /app/services/control/src",
        "COPY --chown=0:0 scripts/core-storage-guard.py scripts/core-python-entrypoint.py "
        "scripts/core-worker-runtime.py scripts/capacity-snapshot.py /app/scripts/",
    ]


def test_python_entrypoint_clears_captured_environment_and_defaults_to_denial():
    rows = instructions()
    entrypoint = json.loads(next(row.removeprefix("ENTRYPOINT ") for row in rows
                                if row.startswith("ENTRYPOINT ")))
    assert entrypoint[:2] == ["/usr/bin/env", "-i"]
    assert "PYTHONPATH=/app/services/common/src:/app/services/control/src" in entrypoint
    assert entrypoint[-3:] == ["/app/.venv/bin/python", "-B",
                              "/app/scripts/core-python-entrypoint.py"]
    assert "CMD []" in rows and "USER 1000:1000" in rows and "HEALTHCHECK NONE" in rows
    assert "--run" not in entrypoint and "-I" not in entrypoint
