import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def local_tmp():
    path = Path(".runtime") / f"test-backup-{uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _bash() -> str:
    candidates = [r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    pytest.skip("bash is required for backup integration tests")


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "the managed Windows sandbox blocks Git Bash writes to the mounted "
        "workspace; run in WSL/Linux"
    ),
)


def _bash_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if len(value) > 1 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def test_capture_and_restore_uses_isolated_target(local_tmp: Path) -> None:
    tmp_path = local_tmp
    source = tmp_path / "appdata"
    source.mkdir()
    (source / "control.sqlite3").write_text("control", encoding="utf-8")
    staging = tmp_path / "staging"
    repository = tmp_path / "repository"
    restore_root = tmp_path / "restore-root"
    config = tmp_path / "backup.env"
    config.write_text(
        "\n".join(
            [
                f"BACKUP_STAGING_ROOT={_bash_path(staging)}",
                f"BACKUP_REPOSITORY={_bash_path(repository)}",
                f"BACKUP_RESTORE_ROOT={_bash_path(restore_root)}",
                f"BACKUP_ITEMS={_bash_path(source)}",
                "BACKUP_MAX_BYTES=20000000",
            ]
        ),
        encoding="utf-8",
    )
    script = _bash_path(Path("scripts/backup.sh"))
    restore_script = _bash_path(Path("scripts/restore.sh"))

    result = subprocess.run(
        [_bash(), script, "--config", _bash_path(config), "--capture-and-send"],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    snapshots = list(repository.glob("*/manifest.sha256"))
    assert len(snapshots) == 1
    snapshot_id = snapshots[0].parent.name

    target = restore_root / "trial"
    result = subprocess.run(
        [
            _bash(),
            restore_script,
            "--config",
            _bash_path(config),
            "--snapshot",
            snapshot_id,
            "--target",
            _bash_path(target),
            "--isolated",
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert (target / source.name / "control.sqlite3").read_text(encoding="utf-8") == "control"


def test_sent_snapshot_is_not_copied_again_from_staging(local_tmp: Path) -> None:
    tmp_path = local_tmp
    source = tmp_path / "appdata"
    source.mkdir()
    (source / "control.sqlite3").write_text("control", encoding="utf-8")
    staging = tmp_path / "staging"
    repository = tmp_path / "repository"
    restore_root = tmp_path / "restore-root"
    config = tmp_path / "backup.env"
    config.write_text(
        "\n".join(
            [
                f"BACKUP_STAGING_ROOT={_bash_path(staging)}",
                f"BACKUP_REPOSITORY={_bash_path(repository)}",
                f"BACKUP_RESTORE_ROOT={_bash_path(restore_root)}",
                f"BACKUP_ITEMS={_bash_path(source)}",
                "BACKUP_MAX_BYTES=20000000",
            ]
        ),
        encoding="utf-8",
    )
    script = _bash_path(Path("scripts/backup.sh"))
    first = subprocess.run(
        [_bash(), script, "--config", _bash_path(config), "--capture-and-send"],
        text=True,
        capture_output=True,
    )
    assert first.returncode == 0, first.stderr
    snapshots = list(repository.glob("*/manifest.sha256"))
    assert len(snapshots) == 1
    snapshot_id = snapshots[0].parent.name
    assert (staging / snapshot_id / ".sent").exists()

    second = subprocess.run(
        [_bash(), script, "--config", _bash_path(config), "--send-pending"],
        text=True,
        capture_output=True,
    )
    assert second.returncode == 0, second.stderr
    assert len(list(repository.glob("*/manifest.sha256"))) == 1


def test_restore_rejects_production_roots(local_tmp: Path) -> None:
    tmp_path = local_tmp
    config = tmp_path / "backup.env"
    config.write_text(
        (
            f"BACKUP_REPOSITORY={_bash_path(tmp_path / 'repository')}\n"
            f"BACKUP_RESTORE_ROOT={_bash_path(tmp_path)}\n"
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            _bash(),
            _bash_path(Path("scripts/restore.sh")),
            "--config",
            _bash_path(config),
            "--snapshot",
            "missing",
            "--target",
            "/srv/data",
            "--isolated",
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0


def test_restore_rejects_paths_that_escape_restore_root(local_tmp: Path) -> None:
    tmp_path = local_tmp
    repository = tmp_path / "repository"
    snapshot = repository / "snapshot-1"
    snapshot.mkdir(parents=True)
    (snapshot / "manifest.sha256").write_text(
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855  empty\n",
        encoding="utf-8",
    )
    (snapshot / "empty").write_bytes(b"")
    restore_root = tmp_path / "restore-root"
    restore_root.mkdir()
    config = tmp_path / "backup.env"
    config.write_text(
        f"BACKUP_REPOSITORY={_bash_path(repository)}\n"
        f"BACKUP_RESTORE_ROOT={_bash_path(restore_root)}\n",
        encoding="utf-8",
    )
    escaped = restore_root / ".." / "escaped"
    result = subprocess.run(
        [
            _bash(),
            _bash_path(Path("scripts/restore.sh")),
            "--config",
            _bash_path(config),
            "--snapshot",
            "snapshot-1",
            "--target",
            _bash_path(escaped),
            "--isolated",
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert not (tmp_path / "escaped" / "empty").exists()
