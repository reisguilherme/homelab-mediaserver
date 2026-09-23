from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


class ImportError(RuntimeError):
    """The verified hardlink import could not be completed safely."""


@dataclass(frozen=True)
class ImportResult:
    source: Path
    destination: Path
    bytes_imported: int


def probe_hardlink_as(source: Path, destination_dir: Path, *, uid: int, gid: int) -> bool:
    """Test a real hardlink with the Arr process identity, leaving no library file."""
    if not hasattr(os, "fork") or uid < 0 or gid < 0:
        return False
    try:
        source_stat = source.stat()
        directory_stat = destination_dir.stat()
    except OSError:
        return False
    if not destination_dir.is_dir() or source_stat.st_dev != directory_stat.st_dev:
        return False
    target = destination_dir / f".homeserver-link-probe-{uuid4().hex}"
    pid = os.fork()
    if pid == 0:
        linked = False
        success = False
        try:
            if os.geteuid() == 0:
                os.setgroups([])
            if os.getegid() != gid:
                os.setgid(gid)
            if os.geteuid() != uid:
                os.setuid(uid)
            os.link(source, target)
            linked = True
            linked_stat = target.stat()
            success = (
                linked_stat.st_dev == source_stat.st_dev
                and linked_stat.st_ino == source_stat.st_ino
                and linked_stat.st_size == source_stat.st_size
            )
        except OSError:
            pass
        finally:
            if linked:
                try:
                    target.unlink()
                except OSError:
                    success = False
        os._exit(0 if success else 1)
    _, status = os.waitpid(pid, 0)
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def _same_payload(left: Path, right: Path) -> bool:
    left_stat = left.stat()
    right_stat = right.stat()
    return (
        left_stat.st_dev == right_stat.st_dev
        and left_stat.st_ino == right_stat.st_ino
        and left_stat.st_size == right_stat.st_size
    )


def import_hardlink(source: str | Path, destination: str | Path) -> ImportResult:
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.is_symlink() or destination_path.is_symlink():
        raise ImportError("symlinks are not valid import paths")
    if not source_path.is_file():
        raise ImportError(f"source file does not exist: {source_path}")
    if destination_path.exists():
        if _same_payload(source_path, destination_path):
            return ImportResult(source_path, destination_path, source_path.stat().st_size)
        raise ImportError("destination contains a different payload") from None
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source_path, destination_path)
    except FileExistsError:
        if _same_payload(source_path, destination_path):
            return ImportResult(source_path, destination_path, source_path.stat().st_size)
        raise ImportError("destination contains a different payload") from None
    except OSError as error:
        raise ImportError(f"hardlink import failed: {error}") from error
    if not _same_payload(source_path, destination_path):
        try:
            destination_path.unlink()
        except OSError:
            pass
        raise ImportError("hardlink identity could not be verified")
    return ImportResult(source_path, destination_path, source_path.stat().st_size)
