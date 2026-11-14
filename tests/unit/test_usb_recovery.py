"""Recovery effects follow durable intent and fresh host evidence."""

import copy
import errno
import json
import os
import stat
import urllib.request
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from homeserver_common.storage import _parse_mounts
from homeserver_common.usb_recovery import (
    SERVICES,
    HostBackend,
    RecoveryController,
    RecoveryHold,
    _boot_restart,
    atomic_json,
    attest_fstab,
    identify_devices,
    validate_config,
)


def test_refresh_jellyfin_uses_modern_auth_without_exposing_a_key(monkeypatch):
    backend = HostBackend(config())
    monkeypatch.setattr(backend, '_current', lambda _: {
        'control-api': {'Id': 'fixture-controller', 'State': {'Running': True}},
    })
    captured = []

    def native(request, **kwargs):
        assert request.full_url == 'http://jellyfin:8096/jellyfin/Library/Refresh'
        assert request.data == b''
        assert 'Token="synthetic-key"' in request.get_header('Authorization', '')
        assert request.get_header('X-emby-token') is None
        captured.append(request)
        return SimpleNamespace(close=lambda: None)

    def command(argv, **kwargs):
        assert 'synthetic-key' not in repr(argv)
        assert argv[:5] == ['docker', 'exec', '-u', '0', 'fixture-controller']
        exec(compile(argv[-1], '<refresh fixture>', 'exec'), {})

    monkeypatch.setenv('HOMESERVER_JELLYFIN_URL', 'http://jellyfin:8096/jellyfin')
    monkeypatch.setenv('HOMESERVER_JELLYFIN_API_KEY', 'synthetic-key')
    monkeypatch.setattr(urllib.request, 'urlopen', native)
    monkeypatch.setattr(backend, '_run', command)
    backend.refresh_jellyfin({'desired': {'jellyfin': {}}})
    assert len(captured) == 1


def config():
    return {
        'version': 1, 'project_dir': '/srv/HomeServer', 'compose_project': 'homeserver',
        'actor_name': 'homeserver-new-batch-io', 'minimum_free_bytes': 10,
        'installed_dir': '/usr/local/lib/homeserver-usb-recovery',
        'pools': [dict(pool_id=name, filesystem_id=name + '-uuid', serial=name + '-bridge',
                       size_bytes=1100 if name == 'ssd' else 1000, disk_size_bytes=1100,
                       partition_number={'ssd': 0, 'hdd': 3, 'hdd2': 1}[name])
                  for name in ('ssd', 'hdd', 'hdd2')],
    }


class FixtureBackend:
    def __init__(self, state_path):
        self.state_path = state_path
        self.effects = []
        self.pools = {name: dict(status='ready', generation=name + '-1', device='/dev/mock')
                      for name in ('ssd', 'hdd', 'hdd2')}
        self.containers = {
            name: dict(id=name + '-id', fingerprint=name + '-fp', running=True, bindings_ok=True)
            for name in ('qbittorrent', 'jellyfin', 'control-worker', 'actor')
        }
        self.containers['jellyfin']['running'] = False
        self.maintenance = False
        self.union_ok = True
        self.snapshot_unavailable = False
        self.capacity_ok = True
        self.crash = None
        self.disconnect_after_mount = False
        self.boot_id = 'boot-one'
        self.rows = [{'hash': 'a' * 40, 'state': 'error', 'progress': 0.25}]

    def observe(self):
        return copy.deepcopy(dict(pools=self.pools, containers=self.containers,
                                  maintenance=self.maintenance, union_ok=self.union_ok,
                                  boot_id=self.boot_id))

    def snapshot(self):
        if self.snapshot_unavailable:
            raise RecoveryHold('native-unavailable')
        return copy.deepcopy(self.rows)

    def effect(self, name):
        durable = json.loads(self.state_path.read_text())
        assert durable['event']['desired'], 'stop/start requires durable desired identities'
        self.effects.append(name)
        if self.crash == name:
            self.crash = None
            raise RuntimeError('simulated process loss after effect')

    def quiesce(self, event):
        for name, item in event['desired'].items():
            current = self.containers[name]
            if current['id'] != item['id'] or current['fingerprint'] != item['fingerprint']:
                raise RecoveryHold('container-replaced')
            if current['running']:
                current['running'] = False
                self.effect('stop:' + name)

    def restore_mounts(self, event):
        self.effect('mount')
        for name in event['affected']:
            self.pools[name]['status'] = 'ready'
        self.union_ok = True
        if self.disconnect_after_mount:
            self.pools['hdd2']['status'] = 'absent'

    def preflight(self, rows):
        if not self.capacity_ok:
            raise RecoveryHold('capacity')
        return {row['hash']: {'pool_id': 'hdd'} for row in rows}

    def start_consumers(self, event):
        for name, item in event['desired'].items():
            current = self.containers[name]
            if current['id'] != item['id'] or current['fingerprint'] != item['fingerprint']:
                raise RecoveryHold('container-replaced')
        for name, item in event['desired'].items():
            if name == 'actor':
                continue
            current = self.containers[name]
            if current['id'] != item['id'] or current['fingerprint'] != item['fingerprint']:
                raise RecoveryHold('container-replaced')
            if not current['running']:
                current['running'] = True
                self.effect('start:' + name)

    def verify_consumers(self, event):
        if any(not self.containers[name]['bindings_ok'] for name in event['desired']
               if name != 'actor'):
            raise RecoveryHold('stale-bind')

    def resume_sources(self, event):
        self.effect('sources')

    def start_actor(self, event):
        if 'actor' in event['desired'] and not self.containers['actor']['running']:
            self.containers['actor']['running'] = True
            self.effect('start:actor')

    def refresh_jellyfin(self, event):
        self.effect('refresh')


def setup(tmp_path):
    path = tmp_path / 'state.json'
    backend = FixtureBackend(path)
    now = [100.0]
    controller = RecoveryController(backend, path, clock=lambda: now[0])
    return controller, backend, now, path


def recover(controller, now, limit=20):
    for _ in range(limit):
        result = controller.tick()
        now[0] += 30
        if result['phase'] == 'complete':
            return result
    return result


def test_healthy_tick_has_no_effects_and_private_atomic_state(tmp_path):
    controller, backend, _, path = setup(tmp_path)
    assert controller.tick()['phase'] == 'ready'
    assert backend.effects == []
    assert path.stat().st_mode & 0o777 == 0o600
    assert 'uuid' not in json.dumps(controller.tick())


def test_event_is_durable_before_stops_and_both_usb_required(tmp_path):
    controller, backend, now, path = setup(tmp_path)
    controller.tick()
    backend.pools['hdd']['status'] = 'absent'
    result = controller.tick()
    assert result['phase'] == 'waiting'
    assert backend.effects[0] == 'stop:actor'
    assert not any(effect.startswith('start:') for effect in backend.effects)
    backend.pools['hdd']['status'] = 'stale'
    backend.pools['hdd2']['status'] = 'absent'
    for _ in range(3):
        now[0] += 30
        assert controller.tick()['phase'] == 'waiting'
    assert 'mount' not in backend.effects
    assert json.loads(path.read_text())['event']['snapshot']


def test_recovery_preserves_manual_stop_and_capacity_precedes_native_start(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick(force_pools=('hdd', 'hdd2'))
    backend.capacity_ok = False
    recover(controller, now, 5)
    assert not any(effect.startswith('start:') for effect in backend.effects)
    backend.capacity_ok = True
    assert recover(controller, now)['phase'] == 'complete'
    assert not backend.containers['jellyfin']['running']
    assert backend.effects.index('sources') < backend.effects.index('start:actor')
    assert backend.effects.count('refresh') == 1
    controller.tick()
    assert backend.effects.count('refresh') == 1


@pytest.mark.parametrize('pool,status', [
    ('ssd', 'invalid'), ('hdd', 'invalid'), ('hdd2', 'absent')])
def test_wrong_or_readonly_device_holds_without_mount(tmp_path, pool, status):
    controller, backend, now, _ = setup(tmp_path)
    backend.pools[pool]['status'] = status
    recover(controller, now, 4)
    assert 'mount' not in backend.effects
    assert not backend.containers['actor']['running']


def test_generation_change_with_reused_device_rebuilds_mounts(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick()
    backend.pools['hdd']['generation'] = 'hdd-2'
    assert recover(controller, now)['phase'] == 'complete'
    assert 'mount' in backend.effects


def test_midpoint_disconnect_stops_recovery_before_consumers_start(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick(force_pools=('hdd',))
    backend.disconnect_after_mount = True
    recover(controller, now, 4)
    assert not any(effect.startswith('start:') for effect in backend.effects)


@pytest.mark.parametrize('crash', ['stop:actor', 'mount', 'start:qbittorrent', 'sources'])
def test_crash_reconciles_current_effects_and_desired_ids(tmp_path, crash):
    controller, backend, now, path = setup(tmp_path)
    backend.crash = crash
    controller.tick(force_pools=('hdd',))
    while backend.crash is not None:
        controller.tick()
        now[0] += 30
    controller = RecoveryController(backend, path, clock=lambda: now[0])
    assert recover(controller, now)['phase'] == 'complete'
    assert backend.effects.count('stop:actor') == 1
    expected_starts = 2 if crash in ('start:qbittorrent', 'sources') else 1
    assert backend.effects.count('start:qbittorrent') == expected_starts


def test_api_failure_without_healthy_cache_quiesces_and_holds(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    backend.snapshot_unavailable = True
    backend.pools['hdd']['status'] = 'stale'
    recover(controller, now)
    assert not backend.containers['actor']['running']
    assert not any(effect.startswith('start:') for effect in backend.effects)


def test_api_failure_uses_healthy_snapshot_with_fresh_capacity_proof(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick()
    backend.snapshot_unavailable = True
    backend.pools['hdd']['status'] = 'stale'
    assert recover(controller, now)['phase'] == 'complete'


def test_external_maintenance_and_replaced_container_remain_held(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick(force_pools=('hdd',))
    backend.maintenance = True
    recover(controller, now, 5)
    assert not any(effect.startswith('start:') for effect in backend.effects)
    backend.maintenance = False
    backend.containers['qbittorrent']['id'] = 'replacement-id'
    recover(controller, now, 5)
    assert not any(effect.startswith('start:') for effect in backend.effects)


def test_force_scope_rejects_ssd_and_unknown_pool(tmp_path):
    controller, backend, _, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        controller.tick(force_pools=('ssd',))
    assert backend.effects == []


def test_stale_bind_blocks_actor_after_native_start(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick(force_pools=('hdd',))
    backend.containers['qbittorrent']['bindings_ok'] = False
    result = recover(controller, now)
    assert result['phase'] != 'complete'
    assert 'start:actor' not in backend.effects


def test_complete_does_not_repeat_recovery_for_old_generation(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick()
    backend.pools['hdd']['generation'] = 'hdd-two'
    assert recover(controller, now)['phase'] == 'complete'
    effects = list(backend.effects)
    assert controller.tick()['phase'] == 'ready'
    assert backend.effects == effects


def test_coldboot_restores_cached_active_ids_but_preserves_manually_stopped(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    for item in backend.containers.values():
        item['restart_policy'] = 'unless-stopped'
    controller.tick()
    backend.boot_id = 'boot-two'
    for item in backend.containers.values():
        item['running'] = False
        item['boot_restart'] = True
    backend.snapshot_unavailable = True
    backend.pools['hdd']['status'] = 'absent'
    controller.tick()
    backend.pools['hdd']['status'] = 'stale'
    assert recover(controller, now)['phase'] == 'complete'
    assert backend.containers['qbittorrent']['running']
    assert backend.containers['actor']['running']
    assert not backend.containers['jellyfin']['running']


def test_backend_registered_readonly_subtree_is_healthy(monkeypatch):
    item = {'State': {'Running': True, 'Pid': 321}, 'Mounts': [
        {'Source': '/srv/media-view/media', 'Destination': '/media', 'RW': False}]}
    host = _parse_mounts('10 1 0:77 / /srv/media-view rw - fuse.mergerfs mergerfs rw')
    local = _parse_mounts('20 1 0:77 /media /media ro - fuse.mergerfs mergerfs rw')
    monkeypatch.setattr('homeserver_common.usb_recovery._mounts', lambda path: local)
    assert HostBackend._binding_ok(item, host)
    local[0] = _parse_mounts(
        '20 1 0:78 /media /media ro - fuse.mergerfs mergerfs rw')[0]
    assert not HostBackend._binding_ok(item, host)


def test_error_after_consumers_start_quiesces_and_retries_capacity(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick(force_pools=('hdd',))
    backend.crash = 'sources'
    while backend.crash:
        controller.tick()
        now[0] += 30
    assert not backend.containers['qbittorrent']['running']
    assert not backend.containers['actor']['running']
    backend.capacity_ok = False
    result = controller.tick()
    assert result['phase'] == 'starting'
    assert not backend.containers['qbittorrent']['running']


def test_mount_scope_rejects_changed_fstab_source_before_effect():
    text = (
        'UUID=ssd-uuid /srv/data ext4 defaults 0 2\n'
        'UUID=hdd-uuid /srv/external ext4 defaults 0 2\n'
        'UUID=hdd2-uuid /srv/external2 ext4 defaults 0 2\n'
        '/srv/data:/srv/external/homeserver:/srv/external2/homeserver '
        '/srv/media-view fuse.mergerfs defaults 0 0\n')
    assert attest_fstab(text, config()['pools'])['hdd']['source'] == 'UUID=hdd-uuid'
    with pytest.raises(RecoveryHold):
        attest_fstab(text.replace('UUID=hdd-uuid', 'UUID=foreign'), config()['pools'])
    alternate = text.replace('UUID=ssd-uuid', '/dev/disk/by-uuid/ssd-uuid')
    assert attest_fstab(alternate, config()['pools'])['ssd']['filesystem'] == 'ext4'


def test_snapshot_file_failure_preserves_trusted_cache_and_has_no_partial_vectors(monkeypatch):
    backend = HostBackend(config())
    calls = []

    def native(endpoint, **kwargs):
        calls.append(endpoint)
        if endpoint == 'torrents/info':
            return [{'hash': 'a' * 40}, {'hash': 'b' * 40}]
        raise RecoveryHold('native-unavailable')

    monkeypatch.setattr(backend, '_native', native)
    with pytest.raises(RecoveryHold):
        backend.snapshot()
    assert calls == ['torrents/info', 'torrents/files?hash=' + 'a' * 40]


def test_failed_coldboot_with_healthy_mounts_still_restores_previous_active(tmp_path):
    controller, backend, now, _ = setup(tmp_path)
    controller.tick()
    backend.boot_id = 'boot-new'
    backend.containers['actor'].update(running=False, boot_restart=True)
    assert recover(controller, now)['phase'] == 'complete'
    assert backend.containers['actor']['running']


def test_backend_stops_verified_originals_despite_one_replacement(monkeypatch):
    backend = HostBackend(config())
    current = {name: {'Id': name + '-id', 'fingerprint': name + '-fp',
                      'State': {'Running': True}} for name in ('actor', 'qbittorrent')}
    event = {'desired': {name: {'id': item['Id'], 'fingerprint': item['fingerprint']}
                         for name, item in current.items()}}
    current['qbittorrent']['Id'] = 'replacement-id'
    effects = []
    monkeypatch.setattr(backend, '_inspect_service', lambda name: current.get(name))
    monkeypatch.setattr(backend, '_fingerprint', lambda item: item['fingerprint'])

    def command(argv, **kwargs):
        effects.append(argv)
        current['actor']['State']['Running'] = False
        return ''

    monkeypatch.setattr(backend, '_run', command)
    with pytest.raises(RecoveryHold):
        backend.quiesce(event)
    assert effects == [['docker', 'stop', '--time', '30', 'actor-id']]
    assert current['qbittorrent']['State']['Running']


def test_coldboot_proof_requires_failed_start_after_current_boot():
    item = {'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}},
            'State': {'StartedAt': '2026-10-11T01:00:00Z',
                      'FinishedAt': '2026-10-11T01:00:01Z', 'ExitCode': 2, 'Error': ''}}
    boot = datetime.fromisoformat('2026-10-11T00:59:00+00:00').timestamp()
    assert _boot_restart(item, boot)
    assert not _boot_restart(item, boot + 120)
    item['State']['ExitCode'] = 0
    assert not _boot_restart(item, boot)


def test_flat_lsblk_uses_unique_pkname_parent(tmp_path):
    nested = blocks()['blockdevices']
    flat = []
    for disk in nested:
        children = disk.pop('children', [])
        flat.append(disk)
        for child in children:
            child['pkname'] = disk['path'].split('/')[-1]
            flat.append(child)
    result = identify_devices({'blockdevices': flat}, config()['pools'], sysfs(tmp_path))
    assert result['hdd']['device'] == '/dev/hdd1'
    assert result['hdd2']['status'] == 'stale'


def test_manual_stop_during_healthy_capture_has_no_mount_effect(tmp_path):
    controller, backend, _, _ = setup(tmp_path)
    original = backend.snapshot

    def snapshot():
        backend.containers['qbittorrent']['running'] = False
        return original()

    backend.snapshot = snapshot
    assert controller.tick()['phase'] == 'ready'
    assert backend.effects == []


def test_effective_unit_what_is_attested_and_allows_current_kernel_device(monkeypatch):
    backend = HostBackend(config())
    fstab = (
        'UUID=ssd-uuid /srv/data ext4 defaults 0 2\n'
        'UUID=hdd-uuid /srv/external ext4 defaults 0 2\n'
        'UUID=hdd2-uuid /srv/external2 ext4 defaults 0 2\n'
        '/srv/data:/srv/external/homeserver:/srv/external2/homeserver '
        '/srv/media-view fuse.mergerfs defaults 0 0\n')
    roots = {'srv-external.mount': ('/srv/external', 'UUID=hdd-uuid', 'ext4'),
             'srv-external2.mount': ('/srv/external2', 'UUID=hdd2-uuid', 'ext4'),
             'srv-media\\x2dview.mount': (
                 '/srv/media-view', '/srv/data:/srv/external/homeserver:/srv/external2/homeserver',
                 'fuse.mergerfs')}
    what = ['unregistered-source']

    def trusted(path):
        if path == Path('/etc/fstab'):
            return fstab
        target, source, filesystem = roots[path.name]
        return f'What={source}\nWhere={target}\nType={filesystem}\n'

    def command(argv, **kwargs):
        unit = argv[2]
        target, source, filesystem = roots[unit]
        effective = what[0] if unit == 'srv-external.mount' else source
        return (f'FragmentPath=/run/systemd/generator/{unit}\nSourcePath=/etc/fstab\n'
                f'DropInPaths=\nWhere={target}\nType={filesystem}\nWhat={effective}\n'
                'ActiveState=active\n')

    monkeypatch.setattr('homeserver_common.usb_recovery._trusted_text', trusted)
    monkeypatch.setattr(backend, '_run', command)
    monkeypatch.setattr('homeserver_common.usb_recovery._mounts', lambda path: _parse_mounts(
        '10 1 8:17 / /srv/external rw - ext4 /dev/currentUSB3 rw'))
    with pytest.raises(RecoveryHold):
        backend._attest_mount_units()
    what[0] = '/dev/currentUSB3'
    backend._attest_mount_units()


def test_finished_batch_actor_is_optional_without_stale_restart_intent(monkeypatch):
    backend = HostBackend(config())
    monkeypatch.setattr(backend, '_inspect_service',
                        lambda name: None if name == 'actor' else {'Id': name + '-id'})
    assert set(backend._containers()) == set(SERVICES)


def test_source_hold_reason_is_sanitized_for_status(monkeypatch):
    from homeserver_common.usb_recovery_sources import SourceHold

    backend = HostBackend(config())
    monkeypatch.setattr(backend, '_eligible', lambda: None)
    monkeypatch.setattr(backend, '_registry', lambda: None)

    def fail(*args):
        raise SourceHold('physical_storage_proof_unavailable')

    monkeypatch.setattr('homeserver_common.usb_recovery_sources.preflight_sources', fail)
    with pytest.raises(RecoveryHold, match='^physical-storage-proof-unavailable$'):
        backend.preflight([])


def test_natural_queue_index_change_does_not_block_unchanged_file_vectors(monkeypatch, tmp_path):
    (tmp_path / '.env').write_text('HOMESERVER_DOWNLOAD_MAX_ACTIVE="5"\n', encoding='utf-8')
    backend = HostBackend(dict(config(), project_dir=str(tmp_path)))
    native_id = 'a' * 40
    old = dict(native_id=native_id, binding_fingerprint='sealed', priority=1,
               state='uploading', complete=True, file_priorities=[])
    current = dict(old, priority=-1)
    event = {'confirmed': {native_id: old}, 'snapshot': [
        {'hash': native_id, '_recovery_files': []}]}
    monkeypatch.setattr(backend, '_native', lambda *args, **kwargs: dict(
        queueing_enabled=True, max_active_downloads=5,
        dont_count_slow_torrents=False, enable_piece_extent_affinity=True))
    monkeypatch.setattr(backend, 'snapshot', lambda: event['snapshot'])
    monkeypatch.setattr(backend, '_registry', lambda: None)
    monkeypatch.setattr('homeserver_common.usb_recovery_sources.validate_sources',
                        lambda *args: [current])
    backend.resume_sources(event)


@pytest.fixture
def native_resume(monkeypatch, tmp_path):
    """Native HTTP and proven sources are boundaries; resume/snapshot stay real."""
    def configure(*, complete=False, old_state='error', state='error',
                  stop_readback='stoppedDL', selection_drift=False,
                  max_active_downloads=50, configured_limit='50',
                  ignore_slow=False, configured_ignore_slow='false'):
        (tmp_path / '.env').write_text(
            ('' if configured_limit is None else
             f'HOMESERVER_DOWNLOAD_MAX_ACTIVE="{configured_limit}"\n')
            + f'HOMESERVER_QUEUE_IGNORE_SLOW_TORRENTS="{configured_ignore_slow}"\n',
            encoding='utf-8')
        backend = HostBackend(dict(config(), project_dir=str(tmp_path)))
        native_id = 'a' * 40
        files = [{'index': 0, 'name': 'Movie/video.mkv', 'size': 100, 'priority': 1}]
        native = dict(hash=native_id, infohash_v1=native_id, infohash_v2='',
                      save_path='/data/torrents/.placements/permit-one',
                      content_path='/data/torrents/.placements/permit-one/Movie',
                      category='movie', state=state, total_size=100,
                      amount_left=0 if complete else 50, progress=1 if complete else 0.5,
                      priority=-1 if complete else 1)
        source = dict(
            native_id=native_id, canonical_infohash=native_id, permit_id='permit-one',
            reservation_id='reservation-one', scope_key='movie:one', pool_id='hdd',
            filesystem_id='hdd-uuid', category='movie', destination=native['save_path'],
            total_size=100, amount_left=native['amount_left'], state=state,
            progress=native['progress'], complete=complete, metadata_sha256='b' * 64,
            budget_bytes=100, selected_files=['Movie/video.mkv'], descriptor_files=[
                {'name': 'Movie/video.mkv', 'size': 100}], padding_files=[],
            priority=native['priority'],
            infohash_v1=native_id, infohash_v2='', content_path=native['content_path'],
            binding_fingerprint='c' * 64, file_priorities=copy.deepcopy(files))
        original = dict(source, state=old_state)
        event = {'confirmed': {native_id: original}, 'snapshot': [
            dict(native, state=old_state, _recovery_files=copy.deepcopy(files))]}
        if selection_drift:
            files[0]['priority'] = 7
            source['file_priorities'][0]['priority'] = 7
        rpc = []

        def native_api(endpoint, data=None, **kwargs):
            rpc.append((endpoint, copy.deepcopy(data)))
            if endpoint == 'app/preferences' and data is None:
                return dict(queueing_enabled=True, max_active_downloads=max_active_downloads,
                            dont_count_slow_torrents=ignore_slow,
                            enable_piece_extent_affinity=True)
            if endpoint == 'torrents/info' and data is None:
                return [copy.deepcopy(native)]
            if endpoint == 'torrents/files?hash=' + native_id and data is None:
                return copy.deepcopy(files)
            if data == {'hashes': native_id} and endpoint == 'torrents/stop':
                native['state'] = stop_readback
                return None
            if data == {'hashes': native_id} and endpoint == 'torrents/start':
                native['state'] = 'uploading' if complete else 'downloading'
                return None
            raise AssertionError('RPC outside the confirmed event source')

        source_gate = backend._source_gate

        def proven_gate(action, *args):
            if action.__name__ == 'validate_sources':
                return [copy.deepcopy(source)]
            return source_gate(action, *args)

        monkeypatch.setattr(backend, '_native', native_api)
        monkeypatch.setattr(backend, '_source_gate', proven_gate)
        # Host mounts/namespaces and descriptor gates have independent fixtures.
        monkeypatch.setattr(backend, '_registry', lambda: None)
        monkeypatch.setattr(backend, 'verify_consumers', lambda event: None)
        return backend, event, rpc

    return configure


def test_resume_uses_configured_download_limit_with_real_preferences_guard(native_resume):
    backend, event, rpc = native_resume(max_active_downloads=5, configured_limit='5')
    backend.resume_sources(event)
    assert ('torrents/stop', {'hashes': 'a' * 40}) in rpc


def test_resume_uses_canonical_default_when_limit_is_not_overridden(native_resume):
    backend, event, rpc = native_resume(max_active_downloads=10, configured_limit=None)
    backend.resume_sources(event)
    assert ('torrents/stop', {'hashes': 'a' * 40}) in rpc


def test_resume_uses_configured_ignore_slow_with_real_preferences_guard(native_resume):
    backend, event, rpc = native_resume(
        max_active_downloads=5, configured_limit='5',
        ignore_slow=True, configured_ignore_slow='true')
    backend.resume_sources(event)
    assert ('torrents/stop', {'hashes': 'a' * 40}) in rpc


def test_resume_ignore_slow_drift_holds_before_native_control(native_resume):
    backend, event, rpc = native_resume(
        max_active_downloads=5, configured_limit='5',
        ignore_slow=False, configured_ignore_slow='true')
    with pytest.raises(RecoveryHold, match='^protected-queue-preferences-drift$'):
        backend.resume_sources(event)
    assert not any(data is not None for _, data in rpc)


def test_resume_invalid_configured_ignore_slow_holds_before_http(native_resume):
    backend, event, rpc = native_resume(configured_ignore_slow='invalid')
    with pytest.raises(RecoveryHold, match='^queue-configuration-unavailable$'):
        backend.resume_sources(event)
    assert rpc == []


@pytest.mark.parametrize('native_limit', [4, 50])
def test_resume_configured_download_drift_holds_before_native_control(native_resume, native_limit):
    backend, event, rpc = native_resume(max_active_downloads=native_limit, configured_limit='5')
    with pytest.raises(RecoveryHold, match='^protected-queue-preferences-drift$'):
        backend.resume_sources(event)
    assert not any(data is not None for _, data in rpc)


@pytest.mark.parametrize('configured_limit', ['0', 'true', 'invalid'])
def test_resume_invalid_queue_configuration_holds_before_native_control(
    native_resume, configured_limit,
):
    backend, event, rpc = native_resume(configured_limit=configured_limit)
    with pytest.raises(RecoveryHold, match='^queue-configuration-unavailable$'):
        backend.resume_sources(event)
    assert not any(data is not None for _, data in rpc)


def test_resume_incomplete_error_stops_exact_source_and_leaves_start_to_actor(native_resume):
    backend, event, rpc = native_resume()
    backend.resume_sources(event)
    assert rpc == [
        ('app/preferences', None), ('torrents/info', None),
        ('torrents/files?hash=' + 'a' * 40, None),
        ('torrents/stop', {'hashes': 'a' * 40}), ('torrents/info', None)]


def test_resume_completed_error_seed_stops_reads_back_then_starts(native_resume):
    backend, event, rpc = native_resume(complete=True, stop_readback='stoppedUP')
    backend.resume_sources(event)
    assert rpc == [
        ('app/preferences', None), ('torrents/info', None),
        ('torrents/files?hash=' + 'a' * 40, None),
        ('torrents/stop', {'hashes': 'a' * 40}), ('torrents/info', None),
        ('torrents/start', {'hashes': 'a' * 40})]


def test_resume_manually_stopped_seed_has_no_native_control_rpc(native_resume):
    backend, event, rpc = native_resume(complete=True, old_state='stoppedUP', state='stoppedUP')
    backend.resume_sources(event)
    assert rpc == [
        ('app/preferences', None), ('torrents/info', None),
        ('torrents/files?hash=' + 'a' * 40, None)]


def test_resume_failed_stop_readback_holds_without_direct_start(native_resume):
    backend, event, rpc = native_resume(complete=True, stop_readback='error')
    with pytest.raises(RecoveryHold, match='^native-stop-readback$'):
        backend.resume_sources(event)
    assert rpc == [
        ('app/preferences', None), ('torrents/info', None),
        ('torrents/files?hash=' + 'a' * 40, None),
        ('torrents/stop', {'hashes': 'a' * 40}), ('torrents/info', None)]


def test_resume_file_selection_drift_holds_before_any_native_control_rpc(native_resume):
    backend, event, rpc = native_resume(selection_drift=True)
    with pytest.raises(RecoveryHold, match='^source-selection-changed$'):
        backend.resume_sources(event)
    assert rpc == [
        ('app/preferences', None), ('torrents/info', None),
        ('torrents/files?hash=' + 'a' * 40, None)]


def test_inactive_unit_is_reattested_before_start(monkeypatch):
    backend = HostBackend(config())
    targets = {'/srv/media-view', '/srv/external', '/srv/external2'}
    effects = []

    def evidence():
        return {name: {'status': 'ready' if name == 'ssd' or target in targets else 'stale',
                       'generation': name + '-known'}
                for name, target in [('ssd', '/srv/data'), ('hdd', '/srv/external'),
                                     ('hdd2', '/srv/external2')]}

    def attest():
        effects.append('attest')
        if '/srv/external' not in targets:
            raise RecoveryHold('unit-effective-source')

    def command(argv, **kwargs):
        if argv[:2] == ['systemctl', 'stop']:
            target = {'srv-media\\x2dview.mount': '/srv/media-view',
                      'srv-external.mount': '/srv/external',
                      'srv-external2.mount': '/srv/external2'}[argv[2]]
            targets.discard(target)
            effects.append('stop:' + target)
        elif argv[:2] == ['systemctl', 'start']:
            target = {'srv-media\\x2dview.mount': '/srv/media-view',
                      'srv-external.mount': '/srv/external',
                      'srv-external2.mount': '/srv/external2'}[argv[2]]
            targets.add(target)
            effects.append('start:' + target)
        return ''

    monkeypatch.setattr(backend, '_eligible', evidence)
    monkeypatch.setattr(backend, '_current', lambda _: {})
    monkeypatch.setattr(backend, '_attest_mount_units', attest)
    monkeypatch.setattr(backend, '_run', command)
    monkeypatch.setattr(backend, '_union_ok', lambda: '/srv/media-view' in targets)
    monkeypatch.setattr('homeserver_common.usb_recovery._mounts', lambda path: _parse_mounts(
        '\n'.join(f'10 1 8:17 / {target} rw - ext4 /dev/fixture rw' for target in targets)))
    with pytest.raises(RecoveryHold, match='unit-effective-source'):
        backend.restore_mounts({'desired': {}, 'affected': ['hdd']})
    assert 'stop:/srv/external' in effects
    assert 'start:/srv/external' not in effects


def blocks():
    return {'blockdevices': [
        {'path': '/dev/' + name, 'type': 'disk', 'serial': name + '-bridge',
         'size': 1100,
         **(dict(uuid='ssd-uuid', fstype='ext4', ro=False, **{'maj:min': '8:1'})
            if name == 'ssd' else
            {'children': [dict(path='/dev/' + name + '1', type='part',
                              uuid=name + '-uuid', fstype='ext4', ro=False,
                              size=1000, **{'maj:min': f'8:{number}'})]})}
        for name, number in [('ssd', 1), ('hdd', 17), ('hdd2', 33)]]}


def sysfs(tmp_path):
    for name in ('ssd', 'hdd', 'hdd2'):
        path = tmp_path / (name if name == 'ssd' else name + '1')
        path.mkdir()
        if name != 'ssd':
            (path / 'partition').write_text('3' if name == 'hdd' else '1')
    return tmp_path


def test_device_identity_uses_uuid_bridge_sizes_partition_and_rejects_clone(tmp_path):
    data = blocks()
    expected = config()['pools']
    devices = identify_devices(data, expected, sysfs(tmp_path))
    assert devices['hdd']['device'] == '/dev/hdd1'
    assert devices['ssd']['status'] == 'stale'
    data['blockdevices'][1]['children'].append(copy.deepcopy(data['blockdevices'][1]['children'][0]))
    assert identify_devices(data, expected, tmp_path)['hdd']['status'] == 'invalid'


@pytest.mark.parametrize('field,value', [('serial', 'wrong'), ('size', 999)])
def test_device_wrong_bridge_or_disk_size_rejected(tmp_path, field, value):
    data = blocks()
    data['blockdevices'][1][field] = value
    assert identify_devices(data, config()['pools'], sysfs(tmp_path))['hdd']['status'] == 'invalid'


def test_device_readonly_or_wrong_partition_rejected(tmp_path):
    data = blocks()
    data['blockdevices'][1]['children'][0]['ro'] = True
    assert identify_devices(data, config()['pools'], sysfs(tmp_path))['hdd']['status'] == 'invalid'
    data['blockdevices'][1]['children'][0]['ro'] = False
    (tmp_path / 'hdd1' / 'partition').write_text('2')
    assert identify_devices(data, config()['pools'], tmp_path)['hdd']['status'] == 'invalid'


@pytest.fixture
def devices_with_reused_major_minor(monkeypatch):
    backend = HostBackend(config())
    roots = {'ssd': '/srv/data', 'hdd': '/srv/external', 'hdd2': '/srv/external2'}
    numbers = {'ssd': 1, 'hdd': 17, 'hdd2': 33}
    identified = {name: dict(status='stale', generation=name + '-reconnected',
                             device='/dev/fixture-' + name, rdev=os.makedev(8, numbers[name]))
                  for name in roots}
    mounts = _parse_mounts('\n'.join(
        f'10 1 8:{numbers[name]} / {target} rw - ext4 /dev/fixture-{name} rw'
        for name, target in roots.items()))
    original_stat = Path.stat
    fault = {'mount': False, 'block': False}

    def current_stat(path, *args, **kwargs):
        for name, row in identified.items():
            if str(path) == row['device']:
                if name == 'hdd' and fault['block']:
                    raise OSError(errno.EIO, 'fixture block identity unavailable')
                return SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=row['rdev'])
            if str(path) == roots[name]:
                if name == 'hdd' and fault['mount']:
                    raise OSError(errno.EIO, 'fixture stale mount')
                return SimpleNamespace(st_dev=row['rdev'])
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(backend, '_run', lambda *args, **kwargs: '{"blockdevices": []}')
    monkeypatch.setattr('homeserver_common.usb_recovery.identify_devices',
                        lambda *args: copy.deepcopy(identified))
    monkeypatch.setattr('homeserver_common.usb_recovery._mounts', lambda path: mounts)
    monkeypatch.setattr(Path, 'stat', current_stat)
    return backend, fault


def test_valid_reconnected_block_with_same_major_minor_and_mount_eio_is_stale(
        devices_with_reused_major_minor):
    backend, fault = devices_with_reused_major_minor
    assert backend._devices()['hdd']['status'] == 'ready'
    fault['mount'] = True
    result = backend._devices()
    assert result['hdd']['status'] == 'stale'
    assert result['hdd']['generation'] == 'hdd-reconnected'
    assert result['ssd']['status'] == result['hdd2']['status'] == 'ready'


def test_block_identity_eio_still_invalid_even_when_old_mount_is_stale(
        devices_with_reused_major_minor):
    backend, fault = devices_with_reused_major_minor
    fault.update(block=True, mount=True)
    result = backend._devices()
    assert result['hdd']['status'] == 'invalid'
    assert result['ssd']['status'] == result['hdd2']['status'] == 'ready'


def test_backend_rejects_arbitrary_config_and_native_mutation(tmp_path):
    configured = config()
    configured['actor_name'] = 'unrelated'
    with pytest.raises(ValueError):
        validate_config(configured)
    backend = HostBackend(config())
    with pytest.raises(RecoveryHold):
        backend._native('torrents/delete', {'hashes': 'a' * 40})


def test_atomic_state_replaces_without_leftovers(tmp_path):
    path = tmp_path / 'state.json'
    atomic_json(path, {'phase': 'waiting'})
    atomic_json(path, {'phase': 'complete'})
    assert json.loads(path.read_text()) == {'phase': 'complete'}
    assert list(tmp_path.iterdir()) == [path]
    assert os.stat(path).st_mode & 0o777 == 0o600


def fourth_config():
    result = config()
    result['pools'].append(dict(pool_id='hdd3', filesystem_id='hdd3-uuid',
                                serial='hdd3-bridge', size_bytes=1000,
                                disk_size_bytes=1100, partition_number=1))
    return result


def test_fourth_pool_is_optional_but_its_identity_and_partition_are_pinned():
    assert validate_config(config())['version'] == 1
    expanded = fourth_config()
    assert validate_config(expanded) is expanded
    expanded['pools'][-1]['partition_number'] = 2
    with pytest.raises(ValueError, match='dimensions'):
        validate_config(expanded)
    expanded['pools'][-1]['partition_number'] = 1
    expanded['pools'][-1]['filesystem_id'] = 'hdd2-uuid'
    with pytest.raises(ValueError, match='ambiguous'):
        validate_config(expanded)


def test_fourth_pool_requires_base_pools_and_exact_union_branches():
    expanded = fourth_config()
    text = (
        'UUID=ssd-uuid /srv/data ext4 defaults 0 2\n'
        'UUID=hdd-uuid /srv/external ext4 defaults 0 2\n'
        'UUID=hdd2-uuid /srv/external2 ext4 defaults 0 2\n'
        'UUID=hdd3-uuid /srv/external3 ext4 defaults 0 2\n'
        '/srv/data:/srv/external/homeserver:/srv/external2/homeserver:'
        '/srv/external3/homeserver /srv/media-view fuse.mergerfs defaults 0 0\n')
    assert attest_fstab(text, expanded['pools'])['hdd3']['source'] == 'UUID=hdd3-uuid'
    with pytest.raises(RecoveryHold, match='fstab-union'):
        attest_fstab(text.replace(':/srv/external3/homeserver', ''), expanded['pools'])
    expanded['pools'].pop(1)
    with pytest.raises(ValueError):
        validate_config(expanded)


def test_fourth_usb_disconnect_waits_then_recovers_without_source_changes(tmp_path):
    controller, backend, now, path = setup(tmp_path)
    backend.pools['hdd3'] = dict(status='ready', generation='hdd3-1', device='/dev/mock')
    assert controller.tick()['phase'] == 'ready'
    backend.pools['hdd3']['status'] = 'absent'
    assert controller.tick()['phase'] == 'waiting'
    assert json.loads(path.read_text())['event']['affected'] == ['hdd3']
    assert 'mount' not in backend.effects
    backend.pools['hdd3']['status'] = 'stale'
    assert recover(controller, now)['phase'] == 'complete'
    assert backend.effects.count('sources') == 1
    assert not backend.containers['jellyfin']['running']


def test_fourth_usb_force_and_host_mount_unit_are_scoped(tmp_path):
    controller, backend, _, path = setup(tmp_path)
    backend.pools['hdd3'] = dict(status='ready', generation='hdd3-1', device='/dev/mock')
    assert controller.tick(force_pools=('hdd3',))['phase'] == 'waiting'
    assert json.loads(path.read_text())['event']['affected'] == ['hdd3']
    assert HostBackend._unit(Path('/srv/external3')) == 'srv-external3.mount'


def test_fourth_usb_absence_blocks_host_mount_recovery(monkeypatch):
    backend = HostBackend(fourth_config())
    devices = {name: dict(status='ready') for name in ('ssd', 'hdd', 'hdd2', 'hdd3')}
    devices['hdd3']['status'] = 'absent'
    monkeypatch.setattr(backend, '_devices', lambda: devices)
    with pytest.raises(RecoveryHold, match='devices-unavailable'):
        backend._eligible()


def test_unregistered_third_usb_is_not_recovered(tmp_path):
    controller, backend, _, _ = setup(tmp_path)
    assert controller.tick(force_pools=('hdd3',))['reason'] == 'unregistered-usb-pool'
    assert backend.effects == []


def test_fourth_usb_restore_preserves_others_and_checks_all_registered_uuids(monkeypatch):
    backend = HostBackend(fourth_config())
    targets = {'/srv/media-view', '/srv/external', '/srv/external2', '/srv/external3'}
    units = {'srv-media\\x2dview.mount': '/srv/media-view',
             'srv-external.mount': '/srv/external',
             'srv-external2.mount': '/srv/external2',
             'srv-external3.mount': '/srv/external3'}
    calls = []

    def evidence():
        return {name: dict(status='ready', generation=name + '-stable')
                for name in ('ssd', 'hdd', 'hdd2', 'hdd3')}

    def command(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ['systemctl', 'stop']:
            targets.discard(units[argv[2]])
        elif argv[:2] == ['systemctl', 'start']:
            targets.add(units[argv[2]])
        return ''

    monkeypatch.setattr(backend, '_eligible', evidence)
    monkeypatch.setattr(backend, '_current', lambda _: {})
    monkeypatch.setattr(backend, '_attest_mount_units', lambda: None)
    monkeypatch.setattr(backend, '_run', command)
    monkeypatch.setattr(backend, '_union_ok', lambda: '/srv/media-view' in targets)
    monkeypatch.setattr('homeserver_common.usb_recovery._mounts', lambda _: _parse_mounts(
        '\n'.join(f'10 1 8:17 / {target} rw - ext4 /dev/fixture rw' for target in targets)))
    backend.restore_mounts({'desired': {}, 'affected': ['hdd3']})
    assert calls[:4] == [
        ['systemctl', 'stop', 'srv-media\\x2dview.mount'],
        ['systemctl', 'stop', 'srv-external3.mount'],
        ['systemctl', 'start', 'srv-external3.mount'],
        ['systemctl', 'start', 'srv-media\\x2dview.mount'],
    ]
    assert calls[-1] == [
        'python3', '/usr/local/lib/homeserver-usb-recovery/prepare_storage.py', '--check',
        '--ssd-uuid', 'ssd-uuid', '--hdd-uuid', 'hdd-uuid', '--hdd2-uuid', 'hdd2-uuid',
        '--hdd3-uuid', 'hdd3-uuid',
    ]
