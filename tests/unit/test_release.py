import importlib.util
import tarfile
from pathlib import Path

import pytest


def _module():
    path = Path("scripts/validate-release.py").resolve()
    spec = importlib.util.spec_from_file_location("validate_release", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_artifact_rejects_parent_path(tmp_path: Path) -> None:
    artifact = tmp_path / "unsafe.tar"
    with tarfile.open(artifact, "w") as archive:
        source = tmp_path / "payload"
        source.write_text("blocked", encoding="utf-8")
        info = tarfile.TarInfo("../escaped")
        info.size = source.stat().st_size
        with source.open("rb") as handle:
            archive.addfile(info, handle)

    with pytest.raises(ValueError, match="unsafe archive path"):
        _module().extract_artifact(artifact, tmp_path / "out")


def test_extract_artifact_writes_regular_files_and_directories(tmp_path: Path) -> None:
    artifact = tmp_path / "release.tar"
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "deploy" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    with tarfile.open(artifact, "w") as archive:
        archive.add(source / "deploy", arcname="deploy")

    destination = tmp_path / "out"
    _module().extract_artifact(artifact, destination)

    assert (destination / "deploy" / "compose.yaml").read_text(encoding="utf-8") == (
        "services: {}\n"
    )
