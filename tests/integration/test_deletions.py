import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from homeserver_control.domain.deletion_plan import DeletionPlanner
from homeserver_control.worker.deletions import DeletionExecutor


@pytest.fixture
def local_root() -> Path:
    root = Path(".runtime") / f"deletions-{uuid4().hex}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_selected_hardlinks_are_removed_and_bytes_are_measured(local_root: Path) -> None:
    root = local_root / "media"
    root.mkdir()
    original = root / "download.mkv"
    imported = root / "library.mkv"
    original.write_bytes(b"payload")
    imported.hardlink_to(original)
    planner = DeletionPlanner(roots=(root,))
    preview = planner.preview("season:tvdb:1:1", (original, imported))
    confirmation = planner.confirm(preview.token, preview.version, "delete-1")
    result = DeletionExecutor().execute(confirmation)
    assert set(result.paths_removed) == {original.resolve(), imported.resolve()}
    assert result.bytes_freed == len(b"payload")
