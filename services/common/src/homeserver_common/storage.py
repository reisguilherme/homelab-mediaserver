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
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

CAPABILITIES = frozenset({'hardlink', 'rename', 'unlink', 'ownership', 'exclusive_placement'})
POOL_IDS = ('ssd', 'hdd', 'hdd2', 'hdd3')
REQUIRED_POOL_IDS = ('ssd', 'hdd')
POOL_LABELS = {'ssd': 'SSD', 'hdd': 'HD USB', 'hdd2': 'HD USB 2', 'hdd3': 'HD USB 3'}


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


def _parse_mounts(text: str) -> list[_Mount]:
    result = []
    for line in text.splitlines():
        before, after = line.split(' - ', 1)
        fields, extra = before.split(), after.split()
        major, minor = map(int, fields[2].split(':'))
        result.append(_Mount(
            os.makedev(major, minor), Path(_unescape(fields[3])),
            Path(_unescape(fields[4])), frozenset(fields[5].split(',')),
            extra[0], _unescape(extra[1]), frozenset(extra[2].split(',')),
        ))
    return result


def _mounts(path: Path) -> list[_Mount]:
    return _parse_mounts(path.read_text(encoding='utf-8'))


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
    def __init__(
        self, pools: Mapping[str, StoragePool], *,
        backend: str = 'mergerfs',
        host_mountinfo: Path = Path('/run/host-mountinfo'),
        mountinfo: Path = Path('/proc/self/mountinfo'),
        uuid_dir: Path = Path('/run/host-uuids'),
        placement_claims: Path = Path('/var/lib/homeserver/storage-placements'),
        device_id: Callable = lambda path: path.stat().st_dev,
        usage: Callable = shutil.disk_usage,
        download_uid: int = 1000, download_gid: int = 1000,
        set_owner: Callable = os.chown,
    ):
        if backend not in ('mergerfs', 'ext4-single'):
            raise StorageRegistryError('unknown storage backend')
        if backend == 'ext4-single' and set(pools) != {'hdd3'}:
            raise StorageRegistryError('direct ext4 requires singleton hdd3')
        if set(pools) - set(POOL_IDS):
            raise StorageRegistryError('unknown storage pool')
        if len({pool.root for pool in pools.values()}) != len(pools):
            raise StorageRegistryError('pools must have distinct physical roots')
        if len({pool.filesystem_id for pool in pools.values()}) != len(pools):
            raise StorageRegistryError('pools must have distinct physical identities')
        self.pool_ids = tuple(name for name in POOL_IDS if name in pools)
        self.pools = {name: pools[name] for name in self.pool_ids}
        self.backend = backend
        self.host_mountinfo, self.mountinfo = host_mountinfo, mountinfo
        self.uuid_dir, self.placement_claims = uuid_dir, placement_claims
        self._device_id, self._usage, self._set_owner = device_id, usage, set_owner
        self.download_uid, self.download_gid = download_uid, download_gid

    @property
    def primary_pool_id(self) -> str:
        return 'ssd' if 'ssd' in self.pools else 'hdd3'

    @property
    def primary_pool(self) -> StoragePool:
        return self._pool(self.primary_pool_id)

    def _pool(self, pool_id: str) -> StoragePool:
        try:
            return self.pools[pool_id]
        except KeyError as exc:
            raise StorageUnavailable('unknown storage pool') from exc

    def _mounts(self, path: Path) -> list[_Mount]:
        return _mounts(path)

    @contextmanager
    def read_snapshot(self):
        """Reuse pool evidence for one read, validating all identities before return."""
        snapshot = _StorageReadSnapshot(self)
        try:
            yield snapshot
            snapshot.verify_unchanged()
        finally:
            snapshot._active = False

    def inspect(self, pool_id: str, *, writable: bool = False) -> StorageSample:
        pool = self._pool(pool_id)
        try:
            _no_symlinks(pool.root)
            if not pool.root.is_dir():
                raise StorageUnavailable('pool root absent')
            host_mounts = self._mounts(self.host_mountinfo)
            hosts = [m for m in host_mounts if m.target == pool.host_mount]
            if len(hosts) != 1:
                raise StorageUnavailable('physical host mount absent or ambiguous')
            host = hosts[0]
            if (host.root != Path('/') or not host.source.startswith('/dev/')
                    or host.filesystem != 'ext4'):
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
            local_mounts = self._mounts(self.mountinfo)
            locals_ = [m for m in local_mounts
                       if pool.root == m.target or m.target in pool.root.parents]
            if not locals_:
                raise StorageUnavailable('container mount evidence absent')
            local = max(locals_, key=lambda m: len(m.target.parts))
            expected_root = Path('/') / pool.host_root.relative_to(pool.host_mount)
            effective_root = local.root / pool.root.relative_to(local.target)
            if (local.device != host.device or effective_root != expected_root
                    or local.filesystem != host.filesystem):
                raise StorageUnavailable('container bind does not map the registered subtree')
            if self.backend == 'ext4-single':
                roots = [m for m in host_mounts if m.target == Path('/')]
                direct = [m for m in local_mounts if m.target == pool.root]
                restrictions = {'nosuid', 'nodev', 'noexec'}
                if (len(roots) != 1 or roots[0].device == host.device
                        or len(direct) != 1 or local != direct[0]
                        or any(pool.host_mount in m.target.parents for m in host_mounts)
                        or any(pool.root in m.target.parents for m in local_mounts)
                        or not restrictions.issubset(host.options)
                        or not restrictions.issubset(local.options)):
                    raise StorageUnavailable('direct ext4 mount is absent, shadowed or unsafe')
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
        if self.backend == 'ext4-single' and '' in parts[1:]:
            raise StorageUnavailable('direct ext4 requires a canonical logical path')
        self.inspect(pool_id, writable=writable)
        pool = self._pool(pool_id)
        physical = pool.root.joinpath(*PurePosixPath(raw).parts[2:])
        try:
            _no_symlinks(physical)
            for mount in self._mounts(self.mountinfo):
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


class _StorageReadSnapshot(StorageRegistry):
    """Private view; never installed on the shared admission registry."""

    def __init__(self, registry: StorageRegistry):
        self.__dict__.update(registry.__dict__)
        self._original = registry
        self._active = True
        self._samples: dict[str, StorageSample] = {}
        self._pool_identities: dict[str, tuple] = {}
        self._placements: dict[tuple[str, str], tuple[int, int]] = {}
        self._resolved_paths: dict[Path, tuple[int, int]] = {}
        try:
            self._tables = {
                path: path.read_text(encoding='utf-8')
                for path in {self.host_mountinfo, self.mountinfo}
            }
            self._parsed_tables = {path: _parse_mounts(text)
                                   for path, text in self._tables.items()}
        except (OSError, ValueError, IndexError) as exc:
            raise StorageUnavailable('read snapshot mount evidence unavailable') from exc

    def _mounts(self, path: Path) -> list[_Mount]:
        if not self._active:
            raise StorageUnavailable('read snapshot has expired')
        return self._parsed_tables[path]

    def _pool_identity(self, pool_id: str) -> tuple:
        pool = self._original._pool(pool_id)
        info = pool.root.stat()
        return (pool, info.st_dev, info.st_ino,
                os.readlink(self.uuid_dir / pool.filesystem_id))

    def inspect(self, pool_id: str, *, writable: bool = False) -> StorageSample:
        if not self._active or writable:
            raise StorageUnavailable('snapshot only permits an active read')
        if pool_id not in self._samples:
            sample = super().inspect(pool_id)
            identity = self._pool_identity(pool_id)
            self._samples[pool_id] = sample
            self._pool_identities[pool_id] = identity
        return self._samples[pool_id]

    def resolve(self, pool_id: str, logical_path: str | Path, *, writable: bool = False) -> Path:
        physical = super().resolve(pool_id, logical_path, writable=writable)
        if physical.exists():
            info = physical.stat()
            identity = (info.st_dev, info.st_ino)
            if physical in self._resolved_paths and self._resolved_paths[physical] != identity:
                raise StorageUnavailable('physical path changed during read snapshot')
            self._resolved_paths[physical] = identity
        return physical

    def prepare_destination(self, pool_id: str, permit_id: str) -> str:
        raise StorageUnavailable('read snapshot cannot prepare a destination')

    def validate_destination(self, pool_id: str, permit_id: str) -> str:
        logical = super().validate_destination(pool_id, permit_id)
        physical = self._pool(pool_id).root / 'torrents' / '.placements' / permit_id
        info = physical.stat()
        identity = (info.st_dev, info.st_ino)
        key = (pool_id, permit_id)
        if key in self._placements and self._placements[key] != identity:
            raise StorageUnavailable('placement changed during read snapshot')
        self._placements[key] = identity
        return logical

    def _verify_tables(self) -> None:
        if any(path.read_text(encoding='utf-8') != text
               for path, text in self._tables.items()):
            raise StorageUnavailable('mount table changed during read snapshot')

    def verify_unchanged(self) -> None:
        try:
            self._verify_tables()
            # Claims, counterparts, path components and destination inode remain
            # independently checked for every source, including after the batch.
            for pool_id, permit_id in tuple(self._placements):
                self.validate_destination(pool_id, permit_id)
            for physical, identity in self._resolved_paths.items():
                _no_symlinks(physical)
                info = physical.stat()
                if (info.st_dev, info.st_ino) != identity:
                    raise StorageUnavailable('physical path changed during read snapshot')
            for pool_id, identity in self._pool_identities.items():
                self._original.inspect(pool_id)
                if self._pool_identity(pool_id) != identity:
                    raise StorageUnavailable('physical pool changed during read snapshot')
            self._verify_tables()
        except (OSError, ValueError, IndexError) as exc:
            raise StorageUnavailable('read snapshot identity unavailable') from exc


def load_storage_registry(
    path: Path = Path('/run/homeserver/storage.json'),
    *, required_backend: str | None = None,
) -> StorageRegistry | None:
    if required_backend not in (None, 'mergerfs', 'ext4-single'):
        raise StorageRegistryError('unknown required storage backend')
    try:
        raw = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        if Path(path).is_symlink():
            raise StorageRegistryError('installed registry is a broken symlink') from None
        if required_backend is not None:
            raise StorageRegistryError('required storage registry is absent') from None
        return None
    except OSError as exc:
        raise StorageRegistryError('cannot read installed storage registry') from exc
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or type(data.get('version')) is not int:
            raise ValueError('unsupported storage registry schema')
        if data['version'] == 1 and set(data) == {'version', 'pools'}:
            backend = 'mergerfs'
        elif (data['version'] == 2 and set(data) == {'version', 'backend', 'pools'}
              and data['backend'] == 'ext4-single'):
            backend = 'ext4-single'
        else:
            raise ValueError('unsupported storage registry schema')
        if required_backend is not None and backend != required_backend:
            raise ValueError('storage backend differs from required layout')
        if not isinstance(data['pools'], list) or len(data['pools']) not in (1, 2, 3, 4):
            raise ValueError('legacy storage pools or singleton hdd3 are required')
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
            if backend == 'ext4-single' and not re.fullmatch(
                r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', uuid,
            ):
                raise ValueError('direct ext4 requires a canonical filesystem UUID')
            if (not isinstance(caps, dict) or set(caps) - CAPABILITIES
                    or any(type(value) is not bool for value in caps.values())):
                raise ValueError('invalid capability evidence')
            ssd = name == 'ssd'
            suffix = {'hdd2': '2', 'hdd3': '3'}.get(name, '')
            external = Path('/srv/external' + suffix)
            pools[name] = StoragePool(
                name, POOL_LABELS[name], uuid,
                (Path('/data') if backend == 'ext4-single'
                 else Path('/storage/ssd' if ssd else f'/storage/{name}/homeserver')),
                Path('/srv/data') if ssd else external,
                Path('/srv/data') if ssd else external / 'homeserver', caps,
            )
        if set(pools) != {'hdd3'} and not set(REQUIRED_POOL_IDS).issubset(pools):
            raise ValueError('SSD and first HD, or singleton hdd3, are required')
        if len({p.filesystem_id for p in pools.values()}) != len(pools):
            raise ValueError('pools must have distinct physical identities')
        return StorageRegistry(pools, backend=backend)
    except (ValueError, TypeError, KeyError) as exc:
        raise StorageRegistryError(f'invalid storage registry: {exc}') from exc
