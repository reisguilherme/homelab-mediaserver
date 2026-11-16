"""Derived runtime recipe must keep startup denied and package verification intact."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RECIPE = ROOT / "docker/core-native/Dockerfile"


def instructions():
    assert RECIPE.is_file(), "derived native runtime recipe is missing"
    text = RECIPE.read_text().replace("\\\n", " ")
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def test_base_and_python_version_have_no_moving_defaults():
    rows = instructions()
    assert rows[0:2] == ["ARG BASE_IMAGE", "FROM ${BASE_IMAGE}"]
    assert "ARG PYTHON_APK_VERSION" in rows
    assert not any(row.startswith(("ARG BASE_IMAGE=", "ARG PYTHON_APK_VERSION=")) for row in rows)


def test_package_install_uses_clean_environment_exact_version_and_default_signature_checks():
    runs = [row for row in instructions() if row.startswith("RUN ")]
    assert len(runs) == 2
    assert all(row.startswith("RUN /usr/bin/env -i ") for row in runs)
    assert '"python3=$PYTHON_APK_VERSION"' in runs[0]
    assert "apk add --no-cache" in runs[0]
    assert "apk info -v" in runs[0] and "apk-installed.txt" in runs[0]
    assert not any(value in runs[0] for value in
                   ("--allow-untrusted", "--no-check-certificate", "curl ", "wget ",
                    "apk upgrade", "--keys-dir", "--repositories-file", "printenv", "env >"))


def test_final_entrypoint_clears_image_environment_and_has_no_native_command():
    rows = instructions()
    entrypoint = json.loads(next(row.removeprefix("ENTRYPOINT ") for row in rows
                                if row.startswith("ENTRYPOINT ")))
    assert entrypoint[:2] == ["/usr/bin/env", "-i"]
    assert entrypoint[-4:] == ["/usr/bin/python3", "-I", "-B",
                              "/usr/local/libexec/core-storage-guard.py"]
    assert "CMD []" in rows and "USER 1000:1000" in rows
    assert "HEALTHCHECK NONE" in rows
    assert not any(value in json.dumps(entrypoint) for value in ("--run", "/init", "-c"))


def test_only_guard_code_is_copied_and_never_an_interpreter_or_private_state():
    rows = instructions()
    copies = [row for row in rows if row.startswith(("COPY ", "ADD "))]
    assert copies == ["COPY --chown=0:0 scripts/core-storage-guard.py "
                      "/usr/local/libexec/core-storage-guard.py"]
    assert rows[rows.index(copies[0]) + 1] == (
        "RUN /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin "
        "/bin/chmod 0444 /usr/local/libexec/core-storage-guard.py")
    assert not any(row.startswith(("VOLUME ", "EXPOSE ", "ONBUILD ")) for row in rows)
