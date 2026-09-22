from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from homeserver_control.domain.deletion_plan import DeletionConfirmation, DeletionPlanError


@dataclass(frozen=True)
class DeletionResult:
    operation_id: str
    paths_removed: tuple[Path, ...]
    bytes_freed: int


class DeletionExecutor:
    """Apply an already confirmed plan, one path at a time, with a tombstone hook."""

    def __init__(self, *, tombstone: Callable[[str, str], None] | None = None) -> None:
        self.tombstone = tombstone

    def execute(self, confirmation: DeletionConfirmation) -> DeletionResult:
        if len(confirmation.paths) != len(confirmation.identities):
            raise DeletionPlanError("deletion confirmation is incomplete")
        snapshots: list[tuple[Path, int, int, int, int]] = []
        for path, expected in zip(confirmation.paths, confirmation.identities, strict=True):
            if path.is_symlink() or not path.is_file():
                raise DeletionPlanError(f"deletion target changed: {path}")
            stat = path.stat()
            if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != expected:
                raise DeletionPlanError(f"deletion target changed: {path}")
            snapshots.append((path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_nlink))

        if self.tombstone is not None:
            self.tombstone(confirmation.media_key, confirmation.operation_id)
        removed: list[Path] = []
        for (path, _device, _inode, _size, _nlink), expected in zip(
            snapshots, confirmation.identities, strict=True
        ):
            try:
                stat = path.lstat()
                if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != expected:
                    raise DeletionPlanError(f"deletion target changed: {path}")
                path.unlink()
            except FileNotFoundError as error:
                raise DeletionPlanError(f"deletion target changed: {path}") from error
            removed.append(path)

        # ``st_nlink`` is captured before mutation.  If another hardlink was
        # not in the confirmed set, deleting these names cannot free payload
        # blocks even though every selected path disappeared.
        grouped: dict[tuple[int, int], tuple[int, int, int]] = {}
        for _path, device, inode, size, nlink in snapshots:
            previous = grouped.get((device, inode))
            if previous is None:
                grouped[(device, inode)] = (size, nlink, 1)
            else:
                grouped[(device, inode)] = (previous[0], previous[1], previous[2] + 1)
        bytes_freed = sum(
            size for size, nlink, selected_count in grouped.values() if nlink <= selected_count
        )
        return DeletionResult(confirmation.operation_id, tuple(removed), bytes_freed)
