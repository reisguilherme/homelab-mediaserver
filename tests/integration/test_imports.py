import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from homeserver_control.worker.imports import ImportError, import_hardlink
from homeserver_control.worker.validation import ValidationError, validate_size


@pytest.fixture
def fixture_root() -> Path:
    root = Path(".runtime") / f"import-{uuid4().hex}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_verified_import_uses_hardlink_and_is_idempotent(fixture_root: Path) -> None:
    source = fixture_root / "torrents" / "episode.mkv"
    destination = fixture_root / "media" / "episode.mkv"
    source.parent.mkdir()
    source.write_bytes(b"fixture")
    first = import_hardlink(source, destination)
    second = import_hardlink(source, destination)
    assert first == second
    assert source.stat().st_ino == destination.stat().st_ino
    assert source.stat().st_dev == destination.stat().st_dev


def test_import_rejects_existing_different_payload(fixture_root: Path) -> None:
    source = fixture_root / "source.mkv"
    destination = fixture_root / "destination.mkv"
    source.write_bytes(b"one")
    destination.write_bytes(b"two")
    with pytest.raises(ImportError, match="different"):
        import_hardlink(source, destination)


def test_size_validation_has_explicit_limit(fixture_root: Path) -> None:
    source = fixture_root / "episode.mkv"
    source.write_bytes(b"1234")
    assert validate_size(source, 4) == 4
    with pytest.raises(ValidationError, match="limit"):
        validate_size(source, 3)
