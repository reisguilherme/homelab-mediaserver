"""Refuse expansion startup if the logical view is an ordinary/stale bind."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .storage import StorageUnavailable, _mounts, _no_symlinks, load_storage_registry


def verify_storage_startup(registry_path: Path, logical_root: Path) -> bool:
    registry = load_storage_registry(registry_path)
    if registry is None:
        return False
    try:
        _no_symlinks(logical_root)
        registry.inspect('ssd')  # SSD is mandatory; USB absence is independent.
        for pool in registry.pools.values():
            registry._guard_parent(pool)
        hosts = [m for m in _mounts(registry.host_mountinfo)
                 if m.target == Path('/srv/media-view')]
        if len(hosts) != 1 or hosts[0].filesystem != 'fuse.mergerfs' or hosts[0].root != Path('/'):
            raise StorageUnavailable('registered mergerfs view is absent')
        host = hosts[0]
        local = max((m for m in _mounts(registry.mountinfo)
                     if m.target == logical_root or m.target in logical_root.parents),
                    key=lambda m: len(m.target.parts))
        if (local.device != host.device or logical_root.stat().st_dev != host.device
                or local.filesystem != host.filesystem
                or local.root / logical_root.relative_to(local.target) != Path('/')):
            raise StorageUnavailable('logical storage bind does not map mergerfs root')
        control = logical_root / '.mergerfs'
        expected = '/srv/data=RW:/srv/external/homeserver=RW'
        if os.getxattr(control, 'user.mergerfs.branches').decode() != expected:
            raise StorageUnavailable('mergerfs branches differ from registered layout')
        for key, value in {'category.create': 'epff', 'ignorepponrename': 'true',
                           'moveonenospc': 'false', 'link_cow': 'false',
                           'symlinkify': 'false'}.items():
            if os.getxattr(control, 'user.mergerfs.' + key).decode() != value:
                raise StorageUnavailable('unsafe mergerfs option: ' + key)
        return True
    except (OSError, ValueError) as exc:
        raise StorageUnavailable('logical storage startup evidence unavailable') from exc


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        return 2
    try:
        if not verify_storage_startup(Path('/run/homeserver/storage.json'), Path('/data')):
            raise StorageUnavailable('expansion entrypoint requires a registry')
        os.execvp(arguments[0], arguments)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'Storage startup refused: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
