from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Allocation:
    path: Path
    device: int
    inode: int
    allocated_bytes: int


def allocated_bytes(path: str | Path) -> int:
    """Return filesystem blocks used by a file, not its logical length."""

    stat = Path(path).stat()
    return int(stat.st_blocks) * 512


def reconcile_files(paths: list[str | Path]) -> tuple[Allocation, ...]:
    seen: set[tuple[int, int]] = set()
    result: list[Allocation] = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(
            Allocation(
                path=path,
                device=stat.st_dev,
                inode=stat.st_ino,
                allocated_bytes=allocated_bytes(path),
            )
        )
    return tuple(result)
