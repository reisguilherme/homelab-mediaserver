import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def local_tmp():
    path = Path(".runtime") / f"test-layout-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_hardlink_preserves_payload_after_unlink(local_tmp: Path) -> None:
    tmp_path = local_tmp
    original = tmp_path / "download.mkv"
    imported = tmp_path / "library.mkv"
    original.write_bytes(b"fixture")
    os.link(original, imported)
    assert original.stat().st_ino == imported.stat().st_ino
    assert original.stat().st_dev == imported.stat().st_dev
    original.unlink()
    assert imported.read_bytes() == b"fixture"


def test_dev_compose_does_not_reference_production_mounts() -> None:
    compose = Path("deploy/compose.dev.yaml").read_text(encoding="utf-8")
    assert "/srv/data" not in compose
    assert "127.0.0.1" in compose
    assert "HOMESERVER_RUNTIME" in compose
