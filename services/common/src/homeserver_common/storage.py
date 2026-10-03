"""Physical storage identity and exclusive torrent placement.

The host mount table and UUID links must be read-only host binds. A container
directory (including a Docker-created empty bind source) is never mount evidence.
Capability flags are installation probe results, not filesystem assumptions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

CAPABILITIES = frozenset({'hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement'})
POOL_IDS = ('ssd', 'hdd')


class StorageRegistryError(ValueError):
    """An installed registry is malformed; do not fall back to legacy mode."""


class StorageUnavailable(RuntimeError):
    """Current physical evidence does not authorize the requested operation."""


@dataclass(frozen=True)
class StoragePool:
    pool_id: str
    label: str
    filesystem_id: str
    root: Path
    host_mount: Path
    host_root: Path
    capabilities: Mapping[str, bool]


@dataclass(frozen=True)
class StorageSample:
    pool_id: str
    label: str
    filesystem_id: str
    measured_at: float
    total_bytes: int
    used_bytes: int
    free_bytes: int
    state: str = 'ready'
    reason: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Mount:
    device: int
    root: Path
    target: Path
    options: frozenset[str]
    filesystem: str
    source: str
    super_options: frozenset[str]


def _unescape(value: str) -> str:
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def _mounts(path: Path) -> list[_Mount]:
    result = []
    for line in path.read_text(encoding='utf-8').splitlines():
        before, after = line.split(' - ', 1)
        fields, extra = before.split(), after.split()
        major, minor = map(int, fields[2].split(':'))
        result.append(_Mount(
            os.makedev(major, minor), Path(_unescape(fields[3])),
            Path(_unescape(fields[4])), frozenset(fields[5].split(',')),
            extra[0], _unescape(extra[1]), frozenset(extra[2].split(',')),
        ))
    return result


def _no_symlinks(path: Path) -> None:
    """Reject even internal symlinks, including broken links and root ancestors."""
    for component in reversed((path, *path.parents)):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise StorageUnavailable('symlink in physical path')


def _permit_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', value):
        raise StorageUnavailable('invalid placement identifier')
    return value


class StorageRegistry:
    pool_ids = POOL_IDS

    def __init__(
        self, pools: Mapping[str, StoragePool], *,
        host_mountinfo: Path = Path('/run/host-mountinfo'),
        mountinfo: Path = Path('/proc/self/mountinfo'),
        uuid_dir: Path = Path('/run/host-uuids'),
        placement_claims: Path = Path('/var/lib/homeserver/storage-placements'),
        device_id: Callable = lambda path: path.stat().st_dev,
        usage: Callable = shutil.disk_usage,
        download_uid: int = 1000, download_gid: int = 1000,
        set_owner: Callable = os.chown,
    ):
        self.pools = dict(pools)
        self.host_mountinfo, self.mountinfo = host_mountinfo, mountinfo
        self.uuid_dir, self.placement_claims = uuid_dir, placement_claims
        self._device_id, self._usage, self._set_owner = device_id, usage, set_owner
        self.download_uid, self.download_gid = download_uid, download_gid

    def _pool(self, pool_id: str) -> StoragePool:
        try:
            return self.pools[pool_id]
        except KeyError as exc:
            raise StorageUnavailable('unknown storage pool') from exc

    def inspect(self, pool_id: str, *, writable: bool = False) -> StorageSample:
        pool = self._pool(pool_id)
        try:
            _no_symlinks(pool.root)
            if not pool.root.is_dir():
                raise StorageUnavailable('pool root absent')
            hosts = [m for m in _mounts(self.host_mountinfo) if m.target == pool.host_mount]
            if len(hosts) != 1:
                raise StorageUnavailable('physical host mount absent or ambiguous')
            host = hosts[0]
            if host.root != Path('/') or not host.source.startswith('/dev/'):
                raise StorageUnavailable('host path is not a physical filesystem root')
            if 'rw' not in host.options or 'rw' not in host.super_options:
                raise StorageUnavailable('physical host mount is read-only')
            uuid_link = self.uuid_dir / pool.filesystem_id
            if not uuid_link.is_symlink():
                raise StorageUnavailable('UUID evidence absent')
            device_name = Path(os.readlink(uuid_link)).name
            if device_name != Path(host.source).name:
                raise StorageUnavailable('UUID does not identify mounted device')
            if self._device_id(pool.root) != host.device:
                raise StorageUnavailable('bind device differs from physical host mount')
            locals_ = [m for m in _mounts(self.mountinfo)
                       if pool.root == m.target or m.target in pool.root.parents]
            if not locals_:
                raise StorageUnavailable('container mount evidence absent')
            local = max(locals_, key=lambda m: len(m.target.parts))
            expected_root = Path('/') / pool.host_root.relative_to(pool.host_mount)
            effective_root = local.root / pool.root.relative_to(local.target)
            if (local.device != host.device or effective_root != expected_root
                    or local.filesystem != host.filesystem):
                raise StorageUnavailable('container bind does not map the registered subtree')
            if writable:
                if 'rw' not in local.options or 'rw' not in local.super_options:
                    raise StorageUnavailable('container binding is read-only')
                if any(pool.capabilities.get(key) is not True for key in CAPABILITIES):
                    raise StorageUnavailable('filesystem capability probes incomplete')
            size = self._usage(pool.root)
            return StorageSample(
                pool.pool_id, pool.label, pool.filesystem_id, time.time(),
                size.total, size.used, size.free,
            )
        except (OSError, ValueError, IndexError) as exc:
            raise StorageUnavailable(f'physical evidence unavailable: {exc}') from exc

    def resolve(self, pool_id: str, logical_path: str | Path, *, writable: bool = False) -> Path:
        raw = str(logical_path)
        parts = raw.split('/')
        if ('\\' in raw or '\x00' in raw or '..' in parts or '.' in parts
                or len(parts) < 3 or parts[:2] != ['', 'data']
                or parts[2] not in ('torrents', 'media')):
            raise StorageUnavailable('path outside allowed logical storage roots')
        self.inspect(pool_id, writable=writable)
        pool = self._pool(pool_id)
        physical = pool.root.joinpath(*PurePosixPath(raw).parts[2:])
        try:
            _no_symlinks(physical)
            for mount in _mounts(self.mountinfo):
                if (pool.root in mount.target.parents
                        and (mount.target == physical or mount.target in physical.parents)):
                    raise StorageUnavailable('nested mount in physical storage path')
        except OSError as exc:
            raise StorageUnavailable('cannot inspect physical path') from exc
        return physical

    def _guard_parent(self, pool: StoragePool) -> Path:
        parent = pool.root / 'torrents' / '.placements'
        _no_symlinks(parent)
        # Preserve legacy torrent-root ownership. The guard prevents ordinary
        # qBit mkdir fallback; it is not a sandbox against a malicious UID 1000.
        info = parent.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid == self.download_uid
                or info.st_mode & 0o022):
            raise StorageUnavailable('placement parent is writable by downloader')
        return parent

    def _claim(self, pool: StoragePool, permit_id: str, *, create: bool) -> None:
        _no_symlinks(self.placement_claims)
        if create:
            self.placement_claims.mkdir(mode=0o755, parents=True, exist_ok=True)
        expected = {'pool_id': pool.pool_id, 'filesystem_id': pool.filesystem_id}
        claim = self.placement_claims / f'{permit_id}.json'
        if create:
            try:
                fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(expected, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
        fd = os.open(claim, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding='utf-8') as stream:
            if json.load(stream) != expected:
                raise StorageUnavailable('placement already belongs to another physical pool')

    def _destination(self, pool_id: str, permit_id: str, *, create: bool) -> str:
        permit_id = _permit_id(permit_id)
        pool = self._pool(pool_id)
        logical = f'/data/torrents/.placements/{permit_id}'
        self.inspect(pool_id, writable=create)
        try:
            parent = self._guard_parent(pool)
            for other_id, other in self.pools.items():
                if other_id == pool_id:
                    continue
                # Even an offline bind exposes a fallback directory: if visible,
                # it must not let qBit recreate the missing placement there.
                if other.root.exists():
                    other_parent = self._guard_parent(other)
                    counterpart = other_parent / permit_id
                    if counterpart.exists() or counterpart.is_symlink():
                        raise StorageUnavailable('placement exists on counterpart pool')
            destination = self.resolve(pool_id, logical, writable=create)
            claim = self.placement_claims / f'{permit_id}.json'
            if create and destination.exists() and not claim.exists():
                raise StorageUnavailable('preexisting destination has no placement claim')
            self._claim(pool, permit_id, create=create)
            if create:
                fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    try:
                        os.mkdir(permit_id, mode=0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                    else:
                        self._set_owner(destination, self.download_uid, self.download_gid)
                finally:
                    os.close(fd)
            if not destination.is_dir() or destination.is_symlink():
                raise StorageUnavailable('authorized destination absent or unsafe')
            self.inspect(pool_id, writable=create)
            return logical
        except (OSError, ValueError) as exc:
            raise StorageUnavailable(f'placement unavailable: {exc}') from exc

    def prepare_destination(self, pool_id: str, permit_id: str) -> str:
        return self._destination(pool_id, permit_id, create=True)

    def validate_destination(self, pool_id: str, permit_id: str) -> str:
        """Validate a persisted placement without requiring a writable bind."""
        return self._destination(pool_id, permit_id, create=False)


def load_storage_registry(
    path: Path = Path('/run/homeserver/storage.json'),
) -> StorageRegistry | None:
    try:
        raw = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        if Path(path).is_symlink():
            raise StorageRegistryError('installed registry is a broken symlink') from None
        return None
    except OSError as exc:
        raise StorageRegistryError('cannot read installed storage registry') from exc
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {'version', 'pools'} or data['version'] != 1:
            raise ValueError('unsupported storage registry schema')
        if not isinstance(data['pools'], list) or len(data['pools']) != 2:
            raise ValueError('both fixed storage pools are required')
        pools = {}
        for entry in data['pools']:
            if set(entry) != {'pool_id', 'filesystem_id', 'capabilities'}:
                raise ValueError('unsupported pool fields')
            name, uuid, caps = entry['pool_id'], entry['filesystem_id'], entry['capabilities']
            if name not in POOL_IDS or name in pools:
                raise ValueError('unknown or repeated pool')
            if (not isinstance(uuid, str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', uuid)):
                raise ValueError('invalid filesystem UUID')
            if (not isinstance(caps, dict) or set(caps) - CAPABILITIES
                    or any(type(value) is not bool for value in caps.values())):
                raise ValueError('invalid capability evidence')
            ssd = name == 'ssd'
            pools[name] = StoragePool(
                name, 'SSD' if ssd else 'HD USB', uuid,
                Path('/storage/ssd' if ssd else '/storage/hdd/homeserver'),
                Path('/srv/data' if ssd else '/srv/external'),
                Path('/srv/data' if ssd else '/srv/external/homeserver'), caps,
            )
        if len({p.filesystem_id for p in pools.values()}) != 2:
            raise ValueError('pools must have distinct physical identities')
        return StorageRegistry({key: pools[key] for key in POOL_IDS})
    except (ValueError, TypeError, KeyError) as exc:
        raise StorageRegistryError(f'invalid storage registry: {exc}') from exc
