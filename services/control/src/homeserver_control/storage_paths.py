"""Map union names to verified physical branches without trusting FUSE st_dev."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from homeserver_common.storage import StorageRegistry, StorageUnavailable


def source_logical_path(permit, relative: str) -> str:
    if (not relative or relative.startswith('/') or '\\' in relative
            or any(part in {'', '.', '..'} for part in relative.split('/'))):
        raise StorageUnavailable('invalid selected torrent path')
    destination = permit.destination.rstrip('/')
    if not destination.startswith('/data/torrents') or (
        destination != '/data/torrents' and not destination.startswith('/data/torrents/')
    ) or '..' in destination.split('/'):
        raise StorageUnavailable('invalid admitted destination')
    return f'{destination}/{relative}'


def physical_paths(
    registry: StorageRegistry, logical_path: str | Path, *,
    union_path: Path | None = None, writable: bool = False,
) -> list[tuple[str, Path]]:
    """Use mergerfs allpaths/fullpath to reject foreign or hidden counterparts.

    Directories may span registered branches; files must have exactly one branch.
    See mergerfs runtime_interface.md (file / directory xattrs).
    """
    logical = PurePosixPath(logical_path)
    local = union_path if union_path is not None else Path(logical_path)
    try:
        fullpath = os.fsdecode(os.getxattr(local, 'user.mergerfs.fullpath'))
        allpaths = os.fsdecode(os.getxattr(local, 'user.mergerfs.allpaths')).split('\0')
    except OSError as exc:
        raise StorageUnavailable('mergerfs physical path evidence unavailable') from exc
    if (not fullpath or not allpaths or fullpath not in allpaths
            or len(set(allpaths)) != len(allpaths)):
        raise StorageUnavailable('ambiguous mergerfs physical path evidence')
    result = []
    for raw in allpaths:
        host = Path(raw)
        if not host.is_absolute() or '..' in host.parts:
            raise StorageUnavailable('invalid mergerfs physical path')
        matches = [(name, pool) for name, pool in registry.pools.items()
                   if host.is_relative_to(pool.host_root)]
        if len(matches) != 1:
            raise StorageUnavailable('unknown mergerfs physical branch')
        name, pool = matches[0]
        relative = host.relative_to(pool.host_root)
        if PurePosixPath('/data') / relative != logical:
            raise StorageUnavailable('mergerfs physical path differs from logical path')
        result.append((name, registry.resolve(name, str(logical), writable=writable)))
    return result


def physical_path(
    registry: StorageRegistry, logical_path: str | Path, *,
    union_path: Path | None = None, writable: bool = False,
) -> tuple[str, Path]:
    paths = physical_paths(registry, logical_path, union_path=union_path, writable=writable)
    if len(paths) != 1:
        raise StorageUnavailable('file exists on more than one physical branch')
    return paths[0]


def captured_identity(registry: StorageRegistry, logical_path: str | Path, *,
                      union_path: Path | None = None) -> dict:
    pool_id, physical = physical_path(registry, logical_path, union_path=union_path)
    status = physical.stat()
    return {'pool_id': pool_id, 'filesystem_id': registry.pools[pool_id].filesystem_id,
            'device': status.st_dev, 'inode': status.st_ino, 'size': status.st_size,
            'mtime_ns': status.st_mtime_ns, 'nlink': status.st_nlink}
