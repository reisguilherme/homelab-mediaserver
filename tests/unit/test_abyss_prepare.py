"""Exercise the offline packaging boundary; fixtures never use server data."""

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "deploy" / "abyss"


@pytest.fixture
def package(tmp_path):
    target = tmp_path / "abyss"
    shutil.copytree(PACKAGE, target, ignore=shutil.ignore_patterns("runtime", "__pycache__"))
    return target


def run_prepare(package, *args):
    return subprocess.run(
        [sys.executable, str(package / "prepare.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_prepare_generates_self_contained_assets_and_check_does_not_write(package):
    result = run_prepare(package)
    assert result.returncode == 0, result.stderr
    runtime = package / "runtime"
    paths = sorted(runtime.iterdir())
    assert {path.name for path in paths} == {
        "abyss.css",
        "abyss-defaults.js",
        "spotlight.css",
        "spotlight.html",
        "spotlight-loader.js",
        "LICENSE",
        "SHA256SUMS",
        "google-sans.css",
        "google-sans-normal-latin.woff2",
        "google-sans-normal-latin-ext.woff2",
        "google-sans-italic-latin.woff2",
        "google-sans-italic-latin-ext.woff2",
        "material-icons-round.woff2",
        "LICENSE-material-icons.txt",
        "OFL-google-sans.txt",
        "TRADEMARKS-google-sans.md",
    }
    original = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    checked = run_prepare(package, "--check")
    assert checked.returncode == 0, checked.stderr
    assert original == {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    for name in ("abyss.css", "spotlight.css", "spotlight.html", "spotlight-loader.js"):
        content = (runtime / name).read_text(encoding="utf-8")
        assert "fonts.googleapis.com" not in content
        assert "cdn.jsdelivr.net" not in content
        assert "api.github.com" not in content
    assert (runtime / "LICENSE").read_bytes() == (package / "vendor" / "LICENSE").read_bytes()


def test_prepared_css_resolves_original_font_families_to_bundled_woff2(package):
    assert run_prepare(package).returncode == 0
    runtime = package / "runtime"
    theme = (runtime / "abyss.css").read_text(encoding="utf-8")
    google = (runtime / "google-sans.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\s*\{([^}]+)\}", theme + google)
    assert len(faces) == 5
    for declaration in faces:
        url = re.search(r"src:\s*url\(['\"]?([^)'\"]+)", declaration)[1]
        assert ":" not in url and "/" not in url
        assert (runtime / url).read_bytes().startswith(b"wOF2")
        assert (runtime / url).read_bytes() == (package / "fonts" / url).read_bytes()
    google_faces = [face for face in faces if "'Google Sans'" in face]
    assert len(google_faces) == 4
    assert sum("font-style: italic" in face for face in google_faces) == 2
    assert sum("font-style: normal" in face for face in google_faces) == 2
    assert all("font-weight: 400 700" in face for face in google_faces)
    assert all("unicode-range:" in face for face in google_faces)
    spotlight = (runtime / "spotlight.html").read_text(encoding="utf-8")
    assert 'href="google-sans.css"' in spotlight
    assert "'Material Icons Round' !important" in theme
    for name in ("LICENSE-material-icons.txt", "OFL-google-sans.txt", "TRADEMARKS-google-sans.md"):
        assert (runtime / name).read_bytes() == (package / "fonts" / name).read_bytes()


@pytest.mark.parametrize("name", ["material-icons-round.woff2", "google-sans-normal-latin.woff2"])
def test_changed_font_fails_before_creating_runtime(package, name):
    with (package / "fonts" / name).open("ab") as stream:
        stream.write(b"unreviewed font bytes")
    result = run_prepare(package)
    assert result.returncode != 0
    assert "integrity" in result.stderr.lower()
    assert not (package / "runtime").exists()


@pytest.mark.parametrize("name", ["abyss.css", "spotlight.html", "spotlight-loader.js", "LICENSE"])
def test_changed_vendor_fails_before_creating_runtime(package, name):
    with (package / "vendor" / name).open("ab") as stream:
        stream.write(b"\n/* unexpected upstream change */\n")
    result = run_prepare(package)
    assert result.returncode != 0
    assert "integrity" in result.stderr.lower()
    assert not (package / "runtime").exists()


def test_missing_vendor_fails_before_creating_runtime(package):
    (package / "vendor" / "spotlight.css").unlink()
    result = run_prepare(package)
    assert result.returncode != 0
    assert not (package / "runtime").exists()


def test_check_rejects_modified_runtime_without_repair(package):
    assert run_prepare(package).returncode == 0
    target = package / "runtime" / "spotlight.html"
    target.write_text("unexpected bytes", encoding="utf-8")
    result = run_prepare(package, "--check")
    assert result.returncode != 0
    assert target.read_text(encoding="utf-8") == "unexpected bytes"


def test_check_rejects_missing_runtime_without_creating_it(package):
    result = run_prepare(package, "--check")
    assert result.returncode != 0
    assert not (package / "runtime").exists()


def test_check_rejects_unlisted_runtime_assets(package):
    assert run_prepare(package).returncode == 0
    extra = package / "runtime" / "unreviewed.js"
    extra.write_text("alert('unexpected')", encoding="utf-8")
    assert run_prepare(package, "--check").returncode != 0
    assert extra.is_file()


def test_runtime_javascript_parses_and_enforces_message_boundaries(package):
    assert run_prepare(package).returncode == 0
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required to execute the browser-independent JS checks")
    result = subprocess.run(
        [node, str(PACKAGE / "test-runtime.mjs"), str(package / "runtime")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
