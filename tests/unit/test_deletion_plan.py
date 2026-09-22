import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from homeserver_control.domain.deletion_plan import DeletionPlanError, DeletionPlanner


@pytest.fixture
def local_root() -> Path:
    root = Path(".runtime") / f"deletion-{uuid4().hex}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_preview_deduplicates_hardlinks_and_confirmation_is_versioned(local_root: Path) -> None:
    tmp_path = local_root
    media_root = tmp_path / "media"
    download_root = tmp_path / "torrents"
    media_root.mkdir()
    download_root.mkdir()
    original = download_root / "movie.mkv"
    library = media_root / "movie.mkv"
    original.write_bytes(b"payload")
    library.hardlink_to(original)
    planner = DeletionPlanner(roots=(media_root, download_root))
    preview = planner.preview("movie:tmdb:1", (library, original))
    assert preview.bytes_estimated == len(b"payload")
    assert len(preview.paths) == 2
    confirmed = planner.confirm(preview.token, preview.version, "operation-1")
    assert confirmed.operation_id == "operation-1"


def test_preview_rejects_escape_and_stale_confirmation(local_root: Path) -> None:
    tmp_path = local_root
    media_root = tmp_path / "media"
    media_root.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.write_bytes(b"x")
    planner = DeletionPlanner(roots=(media_root,))
    with pytest.raises(DeletionPlanError, match="root"):
        planner.preview("movie:tmdb:2", (outside,))
    item = media_root / "movie.mkv"
    item.write_bytes(b"x")
    preview = planner.preview("movie:tmdb:2", (item,))
    item.write_bytes(b"changed")
    with pytest.raises(DeletionPlanError, match="changed"):
        planner.confirm(preview.token, preview.version, "operation-2")
