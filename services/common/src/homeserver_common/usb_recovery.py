"""Durable, fail-closed recovery of already registered USB media volumes.

Persisted state never supplies commands, paths or container names. A healthy
tick creates no Docker helpers. Only existing fstab mount units are controlled.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .env import convert, parse_env
from .settings import FIELDS
from .storage import StorageRegistry, _mounts, _unescape, load_storage_registry

SERVICES = ('control-worker', 'download-gateway', 'telemetry', 'host-metrics',
            'control-api', 'jellyfin', 'bazarr', 'qbittorrent', 'radarr', 'sonarr')
MOUNTS = {'ssd': Path('/srv/data'), 'hdd': Path('/srv/external'),
          'hdd2': Path('/srv/external2'), 'hdd3': Path('/srv/external3')}
BASE_POOLS = frozenset(('ssd', 'hdd', 'hdd2'))
SUPPORTED_POOLS = (frozenset(('hdd3',)), BASE_POOLS, frozenset(MOUNTS))
PARTITIONS = {'ssd': 0, 'hdd': 3, 'hdd2': 1, 'hdd3': 1}
ROOTS = {key: value if key == 'ssd' else value / 'homeserver'
         for key, value in MOUNTS.items()}
UNION = Path('/srv/media-view')
CONTROL = Path('/srv/appdata/control')
PHASES = {'ready', 'quiescing', 'waiting', 'mounting', 'starting', 'resuming', 'complete'}


def attest_fstab(text, pools):
    rows = {}
    for line in text.splitlines():
        fields = line.split('#', 1)[0].split()
        if len(fields) < 4:
            continue
        source, target, filesystem, options = map(_unescape, fields[:4])
        if target not in {str(value) for value in (*MOUNTS.values(), UNION)}:
            continue
        if target in rows:
            raise RecoveryHold('fstab-ambiguous')
        rows[target] = dict(source=source, filesystem=filesystem, options=options)
    result = {}
    for pool in pools:
        name = pool['pool_id']
        row = rows.get(str(MOUNTS[name]))
        allowed = {'UUID=' + pool['filesystem_id'], '/dev/disk/by-uuid/' + pool['filesystem_id']}
        if (not row or row['source'] not in allowed
                or row['filesystem'] != 'ext4'):
            raise RecoveryHold('fstab-identity')
        result[name] = row
    row = rows.get(str(UNION))
    registered = {pool['pool_id'] for pool in pools}
    expected = [str(ROOTS[name]) for name in MOUNTS if name in registered]
    if (not row or row['filesystem'] != 'fuse.mergerfs'
            or [part.removesuffix('=RW') for part in row['source'].split(':')] != expected):
        raise RecoveryHold('fstab-union')
    result['union'] = row
    return result


def _trusted_text(path):
    if any(component.is_symlink() for component in (path, *path.parents)):
        raise RecoveryHold('unit-trust')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RecoveryHold('unit-trust')
    return path.read_text(encoding='utf-8')


def _boot_restart(item, boot_time):
    if item['HostConfig']['RestartPolicy']['Name'] not in (
            'always', 'unless-stopped', 'on-failure'):
        return False
    state = item['State']
    timestamps = []
    for key in ('StartedAt', 'FinishedAt'):
        try:
            timestamps.append(datetime.fromisoformat(state[key].replace('Z', '+00:00')).timestamp())
        except (KeyError, ValueError):
            continue
    after_boot = any(value >= boot_time for value in timestamps)
    failed_start = bool(state.get('Error') or state.get('ExitCode') or state.get('Restarting'))
    return after_boot and failed_start


class RecoveryHold(RuntimeError):
    """A sanitized reason code; caller must keep consumers quiesced."""


def atomic_json(path, value):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.recovery-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_config(config):
    expected = {'version', 'project_dir', 'compose_project', 'pools', 'actor_name',
                'minimum_free_bytes', 'installed_dir'}
    if (not isinstance(config, dict) or set(config) != expected or config['version'] != 1
            or config['compose_project'] != 'homeserver'
            or config['actor_name'] != 'homeserver-new-batch-io'
            or type(config['minimum_free_bytes']) is not int or config['minimum_free_bytes'] < 0):
        raise ValueError('invalid recovery configuration')
    for key in ('project_dir', 'installed_dir'):
        value = config[key]
        if (not isinstance(value, str) or not value.startswith('/')
                or '..' in value.split('/') or '\x00' in value):
            raise ValueError('invalid installed path')
    pools = config['pools']
    if not isinstance(pools, list) or len(pools) not in (1, 3, 4):
        raise ValueError('unsupported registered pools')
    fields = {'pool_id', 'filesystem_id', 'serial', 'size_bytes',
              'disk_size_bytes', 'partition_number'}
    for row in pools:
        if not isinstance(row, dict) or set(row) != fields or row['pool_id'] not in MOUNTS:
            raise ValueError('invalid pool identity')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', row['filesystem_id']):
            raise ValueError('invalid filesystem identity')
        if not isinstance(row['serial'], str) or not row['serial'] or '\x00' in row['serial']:
            raise ValueError('bridge serial required')
        if (any(type(row[key]) is not int or row[key] < 1
                for key in ('size_bytes', 'disk_size_bytes'))
                or type(row['partition_number']) is not int
                or row['partition_number'] != PARTITIONS[row['pool_id']]):
            raise ValueError('invalid device dimensions')
    names = {row['pool_id'] for row in pools}
    if (names not in SUPPORTED_POOLS or len(names) != len(pools)
            or len({row['filesystem_id'] for row in pools}) != len(pools)):
        raise ValueError('ambiguous pool identity')
    return config


def _generation(sys_path):
    """SCSI path and USB devnum distinguish reconnects with reused major/minor."""
    resolved = sys_path.resolve(strict=True)
    tokens = [str(resolved)]
    for parent in (resolved, *resolved.parents):
        if (parent / 'devnum').is_file():
            tokens += [(parent / 'devnum').read_text().strip(),
                       (parent / 'busnum').read_text().strip()]
            break
    device = sys_path / 'device'
    if device.exists():
        tokens.append(str(device.resolve(strict=True)))
    return hashlib.sha256('|'.join(tokens).encode()).hexdigest()


def identify_devices(data, pools, sys_block=Path('/sys/class/block')):
    flat = []

    def walk(rows, disk=None):
        for row in rows:
            parent = row if row.get('type') == 'disk' else disk
            flat.append((row, parent))
            walk(row.get('children', []), parent)

    walk(data['blockdevices'])
    result = {}
    for expected in pools:
        matches = [(row, disk) for row, disk in flat
                   if row.get('uuid') == expected['filesystem_id']]
        item = {'status': 'absent', 'generation': None, 'device': None}
        result[expected['pool_id']] = item
        if not matches:
            continue
        item['status'] = 'invalid'
        if len(matches) != 1:
            continue
        row, disk = matches[0]
        if disk is None:
            parents = [candidate for candidate, _ in flat
                       if candidate.get('type') == 'disk'
                       and Path(candidate.get('path', '')).name == Path(row.get('pkname', '')).name]
            disk = parents[0] if len(parents) == 1 else None
        try:
            node = Path(row['path'])
            whole_disk = expected['pool_id'] == 'ssd' and expected['partition_number'] == 0
            number = 0 if whole_disk else int((sys_block / node.name / 'partition').read_text())
            if (not str(node).startswith('/dev/') or disk is None
                    or row['type'] != ('disk' if whole_disk else 'part')
                    or row.get('fstype') != 'ext4' or row.get('ro') not in (False, 0, '0')
                    or disk.get('ro', False) not in (False, 0, '0')
                    or disk.get('serial') != expected['serial']
                    or int(row['size']) != expected['size_bytes']
                    or int(disk['size']) != expected['disk_size_bytes']
                    or number != expected['partition_number']):
                continue
            major, minor = map(int, row['maj:min'].split(':'))
            item.update(status='stale', device=str(node), rdev=os.makedev(major, minor),
                        generation=_generation(sys_block / node.name))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


class RecoveryController:
    def __init__(self, backend, state_path, clock=time.time, persist=atomic_json):
        self.backend, self.path = backend, Path(state_path)
        self.clock, self.persist = clock, persist

    def _save(self, state, phase=None, reason=None):
        if phase:
            state['phase'] = phase
        state['reason'] = reason
        state['updated_at'] = self.clock()
        self.persist(self.path, state)

    @staticmethod
    def _summary(state):
        event = state.get('event') or {}
        return dict(version=1, phase=state['phase'], reason=state.get('reason'),
                    event=event.get('started_at'), affected_count=len(event.get('affected', [])),
                    desired_count=len(event.get('desired', {})))

    @staticmethod
    def _available(observation):
        pools = observation['pools']
        return (set(pools) in SUPPORTED_POOLS
                and ('ssd' not in pools or pools['ssd']['status'] == 'ready')
                and all(row['status'] in ('ready', 'stale')
                        for name, row in pools.items() if name != 'ssd'))

    def tick(self, force_pools=()):
        if set(force_pools) - (set(MOUNTS) - {'ssd'}):
            raise ValueError('only registered USB pools may be recovered')
        try:
            state = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            state = dict(version=1, phase='ready', reason=None, event=None, healthy=None)
        if (not isinstance(state, dict) or state.get('version') != 1
                or state.get('phase') not in PHASES):
            raise ValueError('invalid recovery state')
        try:
            observation = self.backend.observe()
            usb_pools = tuple(name for name in MOUNTS
                              if name != 'ssd' and name in observation['pools'])
            if set(force_pools) - set(usb_pools):
                raise RecoveryHold('unregistered-usb-pool')
            if state['phase'] in ('ready', 'complete'):
                previous = state.get('healthy') or {}
                boot_recovery = {}
                if (previous.get('boot_id') and observation.get('boot_id')
                        and previous['boot_id'] != observation['boot_id']):
                    for name, cached in previous.get('active', {}).items():
                        current = observation['containers'].get(name) or {}
                        if (current.get('id') == cached['id']
                                and current.get('fingerprint') == cached['fingerprint']
                                and current.get('boot_restart') is True
                                and not current.get('running')):
                            boot_recovery[name] = cached
                affected = set(force_pools)
                if boot_recovery:
                    affected.update(usb_pools)
                for name in usb_pools:
                    current = observation['pools'][name]
                    if (current['status'] != 'ready'
                            or (previous.get('generations', {}).get(name) not in
                                (None, current['generation']))):
                        affected.add(name)
                bad_bind = any(item['running'] and not item['bindings_ok']
                               for item in observation['containers'].values())
                if (not affected and not bad_bind and observation['union_ok']
                        and self._available(observation)):
                    try:
                        rows = self.backend.snapshot()
                    except (OSError, RuntimeError, ValueError):
                        rows = previous.get('snapshot')
                    fresh = self.backend.observe()
                    storage_unchanged = (fresh['pools'] == observation['pools']
                                         and fresh['union_ok']
                                         and not any(item['running'] and not item['bindings_ok']
                                                     for item in fresh['containers'].values()))
                    if storage_unchanged:
                        observation = fresh
                        state['healthy'] = dict(generations={name: item['generation']
                                                for name, item in observation['pools'].items()},
                                                snapshot=rows, boot_id=observation.get('boot_id'),
                                                active={name: item for name, item in
                                                        observation['containers'].items()
                                                        if item['running']})
                        self._save(state, 'ready')
                        return self._summary(state)
                    observation = fresh
                    affected.update(usb_pools)
                if not affected:
                    affected = set(usb_pools)
                try:
                    snapshot = self.backend.snapshot()
                except (OSError, RuntimeError, ValueError):
                    snapshot = previous.get('snapshot')
                desired = {name: {'id': item['id'], 'fingerprint': item['fingerprint']}
                           for name, item in observation['containers'].items() if item['running']}
                for name, cached in boot_recovery.items():
                    desired[name] = {'id': cached['id'], 'fingerprint': cached['fingerprint']}
                desired = dict(sorted(desired.items(), key=lambda pair: pair[0] != 'actor'))
                state['event'] = dict(started_at=self.clock(), affected=sorted(affected),
                                      desired=desired, snapshot=snapshot, stable=None,
                                      mounted=False, confirmed=None, refresh_attempted=False)
                self._save(state, 'quiescing')
            event = state['event']
            if state['phase'] == 'quiescing':
                self.backend.quiesce(event)
                self._save(state, 'waiting')
                return self._summary(state)
            if observation['maintenance']:
                raise RecoveryHold('external-maintenance')
            if not self._available(observation):
                event['stable'] = None
                if state['phase'] in ('starting', 'resuming'):
                    self.backend.quiesce(event)
                self._save(state, 'waiting', 'devices-unavailable')
                return self._summary(state)
            if state['phase'] == 'waiting':
                generations = {name: value['generation']
                               for name, value in observation['pools'].items()}
                stable = event.get('stable')
                if not stable or stable['generations'] != generations:
                    event['stable'] = dict(generations=generations, since=self.clock())
                    self._save(state, reason='debouncing')
                    return self._summary(state)
                if self.clock() - stable['since'] < 15:
                    return self._summary(state)
                self._save(state, 'mounting')
            if state['phase'] == 'mounting':
                self.backend.restore_mounts(event)
                fresh = self.backend.observe()
                if (not self._available(fresh) or not fresh['union_ok']
                        or any(row['status'] != 'ready' for row in fresh['pools'].values())):
                    raise RecoveryHold('mount-readback')
                event['mounted'] = True
                self._save(state, 'starting')
            if state['phase'] == 'starting':
                if (any(row['status'] != 'ready' for row in observation['pools'].values())
                        and not event['mounted']):
                    raise RecoveryHold('mount-readback')
                if event['snapshot'] is None:
                    raise RecoveryHold('snapshot-unavailable')
                event['confirmed'] = self.backend.preflight(event['snapshot'])
                self._save(state)
                self.backend.start_consumers(event)
                self.backend.verify_consumers(event)
                self._save(state, 'resuming')
            if state['phase'] == 'resuming':
                self.backend.verify_consumers(event)
                self.backend.resume_sources(event)
                self.backend.start_actor(event)
                if not event['refresh_attempted']:
                    event['refresh_attempted'] = True
                    self._save(state)
                    self.backend.refresh_jellyfin(event)
                fresh = self.backend.observe()
                state['healthy'] = dict(
                    generations={name: item['generation'] for name, item in fresh['pools'].items()},
                    snapshot=event['snapshot'], boot_id=fresh.get('boot_id'),
                    active={name: item for name, item in fresh['containers'].items()
                            if item['running']})
                self._save(state, 'complete')
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
            if state['phase'] in ('starting', 'resuming'):
                try:
                    self.backend.quiesce(state['event'])
                except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                    pass
                state['phase'] = 'starting'
            code = str(exc) if isinstance(exc, RecoveryHold) else 'operation-failed'
            reason = code if re.fullmatch(r'[a-z0-9-]{1,64}', code) else 'source-hold'
            self._save(state, reason=reason)
        return self._summary(state)


class HostBackend:
    def __init__(self, config):
        self.config = validate_config(config)
        self.pools = {row['pool_id']: row for row in config['pools']}
        self.pool_ids = tuple(name for name in MOUNTS if name in self.pools)
        self.usb_pool_ids = tuple(name for name in self.pool_ids if name != 'ssd')
        self._opener = None

    @staticmethod
    def _run(argv, *, timeout=60):
        try:
            result = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RecoveryHold('command-unavailable') from exc
        if result.returncode:
            raise RecoveryHold('command-failed')
        return result.stdout

    def _devices(self):
        data = json.loads(self._run(['lsblk', '-b', '-J', '-o',
                                    'PATH,TYPE,FSTYPE,UUID,SERIAL,SIZE,RO,PKNAME,MAJ:MIN']))
        devices = identify_devices(data, self.config['pools'])
        mounts = _mounts(Path('/proc/self/mountinfo'))
        for name, item in devices.items():
            if item['status'] != 'stale':
                continue
            try:
                info = Path(item['device']).stat()
            except OSError:
                item['status'] = 'invalid'
                continue
            if not stat.S_ISBLK(info.st_mode) or info.st_rdev != item['rdev']:
                item['status'] = 'invalid'
                continue
            try:
                matching = [mount for mount in mounts if mount.target == MOUNTS[name]]
                if len(matching) == 1:
                    mount = matching[0]
                    if (mount.device == info.st_rdev and mount.root == Path('/')
                            and mount.filesystem == 'ext4' and 'rw' in mount.options
                            and 'rw' in mount.super_options
                            and MOUNTS[name].stat().st_dev == info.st_rdev):
                        item['status'] = 'ready'
            except OSError:
                # Current block identity is proven; the old mount can return EIO
                # after USB reconnect even with the same major/minor number.
                continue
        return devices

    @staticmethod
    def _fingerprint(item):
        guarded = {key: item.get(key) for key in ('Image', 'Path', 'Args', 'Mounts')}
        guarded['config'] = item.get('Config')
        guarded['host_config'] = item.get('HostConfig')
        return hashlib.sha256(json.dumps(guarded, sort_keys=True).encode()).hexdigest()

    def _inspect_service(self, name):
        if name not in (*SERVICES, 'actor'):
            raise RecoveryHold('container-scope')
        filters = (['name=^/homeserver-new-batch-io$'] if name == 'actor' else
                   ['label=com.docker.compose.project=homeserver',
                    'label=com.docker.compose.service=' + name])
        argv = ['docker', 'ps', '-aq']
        for value in filters:
            argv += ['--filter', value]
        identifiers = self._run(argv).split()
        if not identifiers:
            return None
        items = json.loads(self._run(['docker', 'inspect', '--type', 'container', *identifiers]))
        items = [item for item in items if name == 'actor' or
                 (item['Config'].get('Labels') or {}).get(
                     'com.docker.compose.oneoff', '').lower() != 'true']
        if len(items) != 1:
            raise RecoveryHold('ambiguous-container')
        item = items[0]
        if name == 'actor' and (item.get('Name') != '/homeserver-new-batch-io'
                                or '--execute' not in item['Config']['Cmd']
                                or '--fill-download-slots' not in item['Config']['Cmd']):
            raise RecoveryHold('actor-identity')
        return item

    def _containers(self, *, require_all=True):
        result = {}
        for name in (*SERVICES, 'actor'):
            item = self._inspect_service(name)
            if item is not None:
                result[name] = item
        if require_all and not set(SERVICES).issubset(result):
            raise RecoveryHold('missing-container')
        return result

    @staticmethod
    def _binding_ok(item, host_mounts):
        if not item['State']['Running']:
            return True
        try:
            local = _mounts(Path('/proc') / str(int(item['State']['Pid'])) / 'mountinfo')
            checked = 0
            for binding in item['Mounts']:
                source, destination = Path(binding['Source']), Path(binding['Destination'])
                root = next((mount for mount in (*MOUNTS.values(), UNION)
                             if source == mount or mount in source.parents), None)
                if root is None:
                    continue
                allowed = {*MOUNTS.values(), *ROOTS.values(), UNION}
                allowed.update(base / leaf for base in (*ROOTS.values(), UNION)
                               for leaf in ('media', 'torrents'))
                if source not in allowed:
                    return False
                hosts = [mount for mount in host_mounts if mount.target == root]
                locals_ = [mount for mount in local if mount.target == destination]
                if len(hosts) != 1 or len(locals_) != 1 or type(binding.get('RW')) is not bool:
                    return False
                host, mount = hosts[0], locals_[0]
                option = 'rw' if binding['RW'] else 'ro'
                if (host.device != mount.device
                        or mount.root != Path('/') / source.relative_to(root)
                        or host.filesystem != mount.filesystem or option not in mount.options
                        or (binding['RW'] and 'rw' not in mount.super_options)):
                    return False
                checked += 1
            return checked > 0
        except (OSError, ValueError, KeyError):
            return False

    def _registry(self):
        registry = load_storage_registry(CONTROL / 'storage.json')
        if registry is None or set(registry.pools) != set(self.pools):
            raise RecoveryHold('registry-unavailable')
        if any(registry.pools[name].filesystem_id != self.pools[name]['filesystem_id']
               for name in self.pool_ids):
            raise RecoveryHold('registry-changed')
        return StorageRegistry({name: replace(pool, root=pool.host_root)
                                for name, pool in registry.pools.items()},
                               host_mountinfo=Path('/proc/self/mountinfo'),
                               mountinfo=Path('/proc/self/mountinfo'),
                               uuid_dir=Path('/dev/disk/by-uuid'),
                               placement_claims=CONTROL / 'storage-placements')

    def _union_ok(self):
        try:
            mounts = [mount for mount in _mounts(Path('/proc/self/mountinfo'))
                      if mount.target == UNION]
            if (len(mounts) != 1 or mounts[0].filesystem != 'fuse.mergerfs'
                    or mounts[0].root != Path('/') or 'rw' not in mounts[0].options
                    or UNION.stat().st_dev != mounts[0].device):
                return False
            expected = ':'.join(f'{ROOTS[name]}=RW' for name in self.pool_ids)
            if os.getxattr(UNION / '.mergerfs', 'user.mergerfs.branches').decode() != expected:
                return False
            for key, value in {'category.create': 'epff', 'ignorepponrename': 'true',
                               'moveonenospc': 'false', 'link_cow': 'false', 'symlinkify': 'false',
                               'cache.files': 'auto-full', 'dropcacheonclose': 'true'}.items():
                if os.getxattr(UNION / '.mergerfs', 'user.mergerfs.' + key).decode() != value:
                    return False
            return True
        except (OSError, ValueError):
            return False

    def observe(self):
        pools = self._devices()
        hosts = _mounts(Path('/proc/self/mountinfo'))
        boot_time = time.time() - float(Path('/proc/uptime').read_text().split()[0])
        containers = {name: dict(id=item['Id'], fingerprint=self._fingerprint(item),
                                 running=item['State']['Running'],
                                 restart_policy=item['HostConfig']['RestartPolicy']['Name'],
                                 boot_restart=_boot_restart(item, boot_time),
                                 bindings_ok=self._binding_ok(item, hosts))
                      for name, item in self._containers(require_all=False).items()}
        return dict(pools=pools, containers=containers, union_ok=self._union_ok(),
                    maintenance=(CONTROL / 'RECOVERY_MODE').exists(),
                    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip())

    def _native(self, endpoint, data=None, *, timeout=15):
        allowed = {'torrents/info', 'app/preferences', 'torrents/files'}
        if ((data is None and endpoint.split('?')[0] not in allowed)
                or (data is not None and endpoint not in {'torrents/stop', 'torrents/start'})):
            raise RecoveryHold('native-action-forbidden')
        if data is not None and (set(data) != {'hashes'}
                                 or not re.fullmatch(r'[a-fA-F0-9]{40}', data['hashes'])):
            raise RecoveryHold('native-hash-invalid')
        try:
            deadline = time.monotonic() + timeout
            headers = {'Referer': 'http://127.0.0.1:18080/', 'Origin': 'http://127.0.0.1:18080'}
            if self._opener is None:
                values = parse_env((Path(self.config['project_dir']) / '.env').read_text())
                username = values['HOMESERVER_QBIT_USERNAME']
                password = values['HOMESERVER_QBIT_PASSWORD']
                self._opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}),
                    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                body = urllib.parse.urlencode({'username': username, 'password': password}).encode()
                login = urllib.request.Request('http://127.0.0.1:18080/api/v2/auth/login',
                                               data=body, headers=headers)
                with self._opener.open(login, timeout=min(10, timeout)) as response:
                    if response.read() != b'Ok.':
                        raise RecoveryHold('native-auth')
            body = urllib.parse.urlencode(data).encode() if data is not None else None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecoveryHold('native-timeout')
            request = urllib.request.Request('http://127.0.0.1:18080/api/v2/' + endpoint,
                                             data=body, headers=headers)
            with self._opener.open(request, timeout=remaining) as response:
                payload = response.read()
                return json.loads(payload) if data is None else None
        except (OSError, ValueError, KeyError) as exc:
            self._opener = None
            raise RecoveryHold('native-unavailable') from exc

    def snapshot(self):
        deadline = time.monotonic() + 30
        rows = self._native('torrents/info')
        if not isinstance(rows, list):
            raise RecoveryHold('native-shape')
        for row in rows:
            native_id = row.get('hash', '')
            if not re.fullmatch('[a-fA-F0-9]{40}', native_id):
                raise RecoveryHold('native-hash-invalid')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RecoveryHold('snapshot-timeout')
            row['_recovery_files'] = self._native('torrents/files?hash=' + native_id,
                                                  timeout=min(15, remaining))
        return rows

    def _current(self, event):
        current = self._containers()
        for name, intended in event['desired'].items():
            if (name not in current or current[name]['Id'] != intended['id']
                    or self._fingerprint(current[name]) != intended['fingerprint']):
                raise RecoveryHold('container-replaced')
        return current

    def quiesce(self, event):
        replaced = False
        for name in ('actor', *SERVICES):
            if name not in event['desired']:
                continue
            try:
                item = self._inspect_service(name)
            except RecoveryHold:
                replaced = True
                continue
            desired = event['desired'][name]
            if (item is None or item['Id'] != desired['id']
                    or self._fingerprint(item) != desired['fingerprint']):
                replaced = True
                continue
            if item['State']['Running']:
                self._run(['docker', 'stop', '--time', '30', item['Id']], timeout=45)
                readback = self._inspect_service(name)
                if readback is not None and readback['State']['Running']:
                    raise RecoveryHold('stop-readback')
        if replaced:
            raise RecoveryHold('container-replaced')

    @staticmethod
    def _unit(target):
        return {'/srv/external': 'srv-external.mount', '/srv/external2': 'srv-external2.mount',
                '/srv/external3': 'srv-external3.mount',
                '/srv/media-view': 'srv-media\\x2dview.mount'}[str(target)]

    def _eligible(self):
        devices = self._devices()
        if (('ssd' in self.pools and devices['ssd']['status'] != 'ready')
                or any(devices[name]['status'] not in ('ready', 'stale')
                       for name in self.usb_pool_ids)):
            raise RecoveryHold('devices-unavailable')
        return devices

    def restore_mounts(self, event):
        self._attest_mount_units()
        before = self._eligible()
        if any(item['State']['Running'] for name, item in self._current(event).items()
               if name in event['desired']):
            raise RecoveryHold('consumers-running')
        self._run(['systemctl', 'stop', self._unit(UNION)])
        if any(mount.target == UNION for mount in _mounts(Path('/proc/self/mountinfo'))):
            raise RecoveryHold('union-busy')
        self._attest_mount_units()
        for name in self.usb_pool_ids:
            if name not in event['affected'] and before[name]['status'] == 'ready':
                continue
            fresh = self._eligible()
            if any(fresh[key]['generation'] != before[key]['generation'] for key in self.pool_ids):
                raise RecoveryHold('device-generation-changed')
            target = MOUNTS[name]
            self._run(['systemctl', 'stop', self._unit(target)])
            if any(mount.target == target for mount in _mounts(Path('/proc/self/mountinfo'))):
                raise RecoveryHold('mount-busy')
            self._attest_mount_units()
            self._eligible()
            self._run(['systemctl', 'start', self._unit(target)])
            readback = self._eligible()
            if (readback[name]['status'] != 'ready'
                    or readback[name]['generation'] != before[name]['generation']):
                raise RecoveryHold('mount-readback')
        self._attest_mount_units()
        self._run(['systemctl', 'start', self._unit(UNION)])
        if not self._union_ok():
            raise RecoveryHold('union-readback')
        argv = ['python3', str(Path(self.config['installed_dir']) / 'prepare_storage.py'),
                '--check']
        for name in self.pool_ids:
            argv += ['--' + name + '-uuid', self.pools[name]['filesystem_id']]
        self._run(argv)
        after = self._eligible()
        if any(after[name]['generation'] != before[name]['generation'] for name in self.pool_ids):
            raise RecoveryHold('device-generation-changed')

    def _attest_mount_units(self):
        registered = attest_fstab(_trusted_text(Path('/etc/fstab')), self.config['pools'])
        targets = [(name, MOUNTS[name]) for name in self.usb_pool_ids] + [('union', UNION)]
        for name, target in targets:
            shown = self._run(['systemctl', 'show', self._unit(target),
                               '--property=FragmentPath', '--property=SourcePath',
                               '--property=DropInPaths', '--property=Where', '--property=Type',
                               '--property=What', '--property=ActiveState'])
            properties = dict(line.split('=', 1) for line in shown.splitlines() if '=' in line)
            if (properties.get('SourcePath') != '/etc/fstab' or properties.get('DropInPaths')
                    or properties.get('Where') != str(target)
                    or properties.get('Type') != registered[name]['filesystem']):
                raise RecoveryHold('unit-source')
            fragment = Path(properties.get('FragmentPath', ''))
            if fragment.parent not in (Path('/run/systemd/generator'),
                                        Path('/run/systemd/generator.early'),
                                        Path('/run/systemd/generator.late')):
                raise RecoveryHold('unit-source')
            fields = dict(line.split('=', 1) for line in _trusted_text(fragment).splitlines()
                          if '=' in line and not line.startswith('#'))
            source = registered[name]['source']
            allowed = {source}
            if name != 'union':
                allowed.add('/dev/disk/by-uuid/' + self.pools[name]['filesystem_id'])
                allowed.add('UUID=' + self.pools[name]['filesystem_id'])
            if (fields.get('Where') != str(target) or fields.get('What') not in allowed
                    or fields.get('Type') != registered[name]['filesystem']):
                raise RecoveryHold('unit-identity')
            effective = set(allowed)
            mounts = [mount for mount in _mounts(Path('/proc/self/mountinfo'))
                      if mount.target == target]
            if properties.get('ActiveState') == 'active' and len(mounts) == 1:
                effective.add(mounts[0].source)
            if properties.get('What') not in effective:
                raise RecoveryHold('unit-effective-source')

    def preflight(self, rows):
        from .usb_recovery_sources import preflight_sources

        self._eligible()
        result = self._source_gate(preflight_sources, rows, CONTROL / 'control.sqlite',
                                   self._registry(), self.config['minimum_free_bytes'])
        return {row['native_id']: row for row in result}

    @staticmethod
    def _source_gate(action, *args):
        from .usb_recovery_sources import SourceHold

        try:
            return action(*args)
        except SourceHold as exc:
            code = str(exc).replace('_', '-')
            if not re.fullmatch(r'[a-z0-9-]{1,64}', code):
                code = 'source-hold'
            raise RecoveryHold(code) from None

    def start_consumers(self, event):
        for name in SERVICES:
            if name not in event['desired']:
                continue
            self._eligible()
            if (CONTROL / 'RECOVERY_MODE').exists():
                raise RecoveryHold('external-maintenance')
            item = self._current(event)[name]
            if not item['State']['Running']:
                self._run(['docker', 'start', item['Id']])
            hosts = _mounts(Path('/proc/self/mountinfo'))
            if not self._binding_ok(self._current(event)[name], hosts):
                self.quiesce(event)
                raise RecoveryHold('stale-bind')

    def verify_consumers(self, event):
        self._eligible()
        if not self._union_ok() or (CONTROL / 'RECOVERY_MODE').exists():
            raise RecoveryHold('storage-maintenance')
        hosts = _mounts(Path('/proc/self/mountinfo'))
        for name, item in self._current(event).items():
            if name == 'actor' or name not in event['desired']:
                continue
            if not item['State']['Running'] or not self._binding_ok(item, hosts):
                raise RecoveryHold('consumer-readback')

    def resume_sources(self, event):
        from .usb_recovery_sources import check_preferences, validate_sources

        try:
            values = parse_env((Path(self.config['project_dir']) / '.env').read_text(
                encoding='utf-8'))
            field = FIELDS['download_max_active']
            key = 'HOMESERVER_DOWNLOAD_MAX_ACTIVE'
            expected = convert(key, values.get(key, str(field.default)), field)
            field = FIELDS['queue_ignore_slow_torrents']
            key = 'HOMESERVER_QUEUE_IGNORE_SLOW_TORRENTS'
            expected_ignore_slow = convert(
                key, values.get(key, str(field.default).lower()), field)
        except (OSError, ValueError):
            raise RecoveryHold('queue-configuration-unavailable') from None
        self._source_gate(check_preferences, self._native('app/preferences'),
                          expected, expected_ignore_slow)
        rows = self.snapshot()
        files = {row['hash']: row['_recovery_files'] for row in rows}
        proven = self._source_gate(validate_sources, rows, files, CONTROL / 'control.sqlite',
                                   self._registry(), self.config['minimum_free_bytes'])
        current = {row['native_id']: row for row in proven}
        if set(current) != set(event['confirmed']):
            raise RecoveryHold('source-set-changed')
        original = {row['hash']: row for row in event['snapshot']}
        for native_id, row in current.items():
            old = event['confirmed'][native_id]
            if row['binding_fingerprint'] != old['binding_fingerprint']:
                raise RecoveryHold('source-vector-changed')
            snapshot_files = original[native_id].get('_recovery_files')
            if snapshot_files is not None:
                vector = [{key: file[key] for key in ('index', 'name', 'size', 'priority')}
                          for file in sorted(snapshot_files, key=lambda item: item['index'])]
                if vector != row['file_priorities']:
                    raise RecoveryHold('source-selection-changed')
        for native_id, row in current.items():
            if row['state'] in ('error', 'missingFiles'):
                self.verify_consumers(event)
                self._native('torrents/stop', {'hashes': native_id})
                fresh = {item['hash']: item for item in self._native('torrents/info')}
                if fresh[native_id]['state'] not in (
                        'stoppedDL', 'stoppedUP', 'pausedDL', 'pausedUP'):
                    raise RecoveryHold('native-stop-readback')
            old = event['confirmed'][native_id]
            if (old['complete'] and old['state'] not in
                    ('stoppedUP', 'stoppedDL', 'pausedUP', 'pausedDL')
                    and (row['state'] in ('stoppedUP', 'pausedUP', 'error', 'missingFiles'))):
                self.verify_consumers(event)
                self._native('torrents/start', {'hashes': native_id})

    def start_actor(self, event):
        if 'actor' not in event['desired']:
            return
        self.verify_consumers(event)
        item = self._current(event)['actor']
        if not item['State']['Running']:
            self._run(['docker', 'start', item['Id']])
        item = self._current(event)['actor']
        if not item['State']['Running'] or not self._binding_ok(
                item, _mounts(Path('/proc/self/mountinfo'))):
            self.quiesce(event)
            raise RecoveryHold('actor-readback')

    def refresh_jellyfin(self, event):
        if 'jellyfin' not in event['desired']:
            return
        item = self._current(event).get('control-api')
        if not item or not item['State']['Running']:
            raise RecoveryHold('refresh-unavailable')
        script = (
            "import os,urllib.request;"
            "from homeserver_common.jellyfin_auth import jellyfin_headers;"
            "u=os.environ['HOMESERVER_JELLYFIN_URL'].rstrip('/')+'/Library/Refresh';"
            "r=urllib.request.Request(u,data=b'',headers="
            "jellyfin_headers(os.environ['HOMESERVER_JELLYFIN_API_KEY']));"
            "urllib.request.urlopen(r,timeout=15).close()"
        )
        self._run(['docker', 'exec', '-u', '0', item['Id'], 'python', '-m',
                   'homeserver_common.runtime', 'python', '-c', script], timeout=20)
