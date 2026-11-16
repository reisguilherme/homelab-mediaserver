"""Capacity producer stays in the admitted worker process group."""

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/core-worker-runtime.py'
UUID = 'b2256ce1-b29c-4608-a227-c78a50ea169e'


def load():
    assert SCRIPT.is_file(), 'supervised worker capacity runtime is missing'
    spec = importlib.util.spec_from_file_location('core_worker_fixture', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_worker_and_collector_commands():
    module = load()
    assert module.WORKER_COMMAND == ['/app/.venv/bin/python', '-m', 'homeserver_control.worker']
    assert module.COLLECTOR_COMMAND == ['/app/.venv/bin/python',
                                         '/app/scripts/core-worker-runtime.py', '--collector']
    assert module.INTERVAL == 5 and module.MAX_SAMPLE_WAIT == 10


def test_collector_environment_contains_no_application_credentials():
    module = load()
    env = module.collector_environment({'HOMESERVER_MEDIA_UUID': UUID,
                                       'HOMESERVER_QBIT_PASSWORD': 'private-fixture',
                                       'PYTHONPATH': '/untrusted', 'LD_PRELOAD': '/untrusted'})
    assert env['HOMESERVER_MEDIA_UUID'] == UUID
    assert 'private-fixture' not in str(env)
    assert 'LD_PRELOAD' not in env and '/untrusted' not in str(env)


def test_importing_runtime_does_not_import_control_application():
    before = set(sys.modules)
    load()
    assert not any(name.startswith('homeserver_control.') for name in set(sys.modules) - before)


def sample(module, monkeypatch, **changes):
    data = dict(filesystem_id=UUID, pool_id='hdd3', state='ready', measured_at=100,
                total_bytes=1000, free_bytes=500)
    data.update(changes)
    snapshot = dict(token=('mount',), sequence=1, lease_sha256='a' * 64,
                    expires_at=module.guard.uptime_clock() + 20)
    calls = []
    monkeypatch.setattr(module.guard, 'check_runtime',
                        lambda *a: calls.append(('lease', a)) or snapshot)
    def registry_loader(path, **kwargs):
        calls.append(('registry', path, kwargs))
        return SimpleNamespace(primary_pool=SimpleNamespace(filesystem_id=UUID))
    monkeypatch.setattr(module, 'load_storage_registry', registry_loader)
    monkeypatch.setattr(module.capacity, 'collect_pools',
                        lambda registry: calls.append(('sample',)) or {**data, 'pools': [data]})
    return calls, snapshot


def test_collection_publishes_atomic_file_after_two_lease_checks(tmp_path, monkeypatch):
    module = load()
    calls, snapshot = sample(module, monkeypatch)
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        observed = module.collect_once(fd, UUID)
        assert observed == snapshot
        assert calls[0] == ('lease', (UUID, 'rw')) and calls[-1] == calls[0]
        assert ('registry', module.REGISTRY_PATH, {'required_backend': 'ext4-single'}) in calls
        output = tmp_path / 'capacity.json'
        assert json.loads(output.read_text())['pools'][0]['pool_id'] == 'hdd3'
        assert output.stat().st_mode & 0o777 == 0o600
        assert list(tmp_path.iterdir()) == [output]
    finally:
        os.close(fd)


@pytest.mark.parametrize('change', [dict(state='unavailable'), dict(filesystem_id='other'),
                                    dict(free_bytes=1001), dict(total_bytes=0)])
def test_bad_sample_removes_old_ready_file(tmp_path, monkeypatch, change):
    module = load()
    sample(module, monkeypatch, **change)
    output = tmp_path / 'capacity.json'
    output.write_text('stale')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((module.guard.GuardError, ValueError, OSError)):
            module.collect_once(fd, UUID)
        assert not output.exists()
    finally:
        os.close(fd)


@pytest.mark.parametrize('failure', ['missing_registry', 'hold_after_sample', 'fsync'])
def test_failed_collection_cannot_leave_ready_snapshot(tmp_path, monkeypatch, failure):
    module = load()
    sample(module, monkeypatch)
    if failure == 'missing_registry':
        monkeypatch.setattr(module, 'load_storage_registry', lambda *a, **kw: None)
    elif failure == 'hold_after_sample':
        def fail_after_sample(_registry):
            def hold(*a):
                raise module.guard.GuardError('HOLD')
            monkeypatch.setattr(module.guard, 'check_runtime', hold)
            return dict(filesystem_id=UUID, state='ready', total_bytes=1000, free_bytes=500,
                        pools=[dict(pool_id='hdd3', filesystem_id=UUID, state='ready',
                                    total_bytes=1000, free_bytes=500)])
        monkeypatch.setattr(module.capacity, 'collect_pools', fail_after_sample)
    else:
        original = os.fsync
        failed = False
        def fail_once(fd):
            nonlocal failed
            if not failed:
                failed = True
                raise OSError('synthetic fsync failure')
            original(fd)
        monkeypatch.setattr(os, 'fsync', fail_once)
    (tmp_path / 'capacity.json').write_text('stale')
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises((module.guard.GuardError, OSError)):
            module.collect_once(fd, UUID)
        assert not list(tmp_path.iterdir())
    finally:
        os.close(fd)


def test_renewal_rejects_changed_physical_generation(tmp_path, monkeypatch):
    module = load()
    _calls, snapshot = sample(module, monkeypatch)
    previous = {**snapshot, 'token': ('old-physical-generation',)}
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.guard.GuardError, match='storage_identity_changed'):
            module.collect_once(fd, UUID, previous)
        assert not list(tmp_path.iterdir())
    finally:
        os.close(fd)


def test_collector_publishes_every_five_seconds_without_worker_dependency(tmp_path, monkeypatch):
    module = load()
    now = [0.0]
    measurements = []
    acknowledgements = []
    class Done(Exception):
        pass
    monkeypatch.setattr(os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(os, 'getegid', lambda: 1000)
    monkeypatch.setattr(os, 'getgroups', lambda: [])
    monkeypatch.setattr(module, 'open_output_directory',
                        lambda: os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY))
    monkeypatch.setattr(module.guard, 'uptime_clock', lambda: now[0])
    def collect(fd, expected_uuid, previous):
        assert expected_uuid == UUID
        measurements.append(now[0])
        now[0] += 0.2
        return {'sequence': len(measurements)}
    monkeypatch.setattr(module, 'collect_once', collect)
    monkeypatch.setattr(os, 'write', lambda fd, value: acknowledgements.append(now[0]))
    def sleep(duration):
        now[0] += duration
        if len(measurements) == 3:
            raise Done
    monkeypatch.setattr(module.time, 'sleep', sleep)
    with pytest.raises(Done):
        module.collector_loop(UUID)
    assert measurements == [0, 5, 10]
    assert acknowledgements == [0.2, 5.2, 10.2]


@pytest.mark.parametrize('unsafe', ['owner', 'permissions', 'symlink'])
def test_output_directory_requires_dedicated_identity(tmp_path, monkeypatch, unsafe):
    if os.geteuid() != 0:
        pytest.skip('fixture needs root to verify directory ownership')
    module = load()
    output = tmp_path / 'capacity'
    output.mkdir(mode=0o750)
    os.chown(output, 1000, 1000)
    if unsafe == 'owner':
        os.chown(output, 0, 0)
    elif unsafe == 'permissions':
        output.chmod(0o777)
    else:
        link = tmp_path / 'link'
        link.symlink_to(output)
        output = link
    monkeypatch.setattr(module, 'OUTPUT_DIRECTORY', output)
    with pytest.raises((module.guard.GuardError, OSError)):
        module.open_output_directory()


@pytest.mark.parametrize('mode', ['first_failure', 'collector_failure', 'worker_failure',
                                 'collector_stall', 'hold'])
def test_real_worker_group_ends_both_children_and_invalidates_capacity(mode, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip('fixture needs root to verify collector UID/GID 1000')
    load()
    with tempfile.TemporaryDirectory(prefix='core-worker-group-') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        output = root / 'capacity'
        output.mkdir(mode=0o750)
        os.chown(output, 1000, 1000)
        collector = root / 'collector.py'
        worker = root / 'worker.py'
        common = ('import json, os, signal, time\nfrom pathlib import Path\n'
                  'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                  f'output = Path({str(output)!r})\n')
        collector.write_text(common +
            '(output / "collector.pid").write_text(json.dumps([os.getpid(), os.geteuid(), '
            'os.getegid(), os.getgroups(), os.getpgrp(), os.getsid(0)]))\n'
            f'mode = {mode!r}\n'
            'if mode == "first_failure": raise SystemExit(17)\n'
            '(output / "capacity.json").write_text("fresh-fixture")\n'
            'os.write(1, b".")\n'
            'if mode == "collector_failure":\n    time.sleep(0.3)\n    raise SystemExit(17)\n'
            'while True:\n    time.sleep(0.05)\n'
            '    if mode != "collector_stall": os.write(1, b".")\n')
        worker.write_text(common +
            '(output / "worker.pid").write_text(json.dumps([os.getpid(), os.geteuid(), '
            'os.getegid(), os.getgroups(), os.getpgrp(), os.getsid(0)]))\n'
            'assert (output / "capacity.json").read_text() == "fresh-fixture"\n'
            f'if {mode!r} == "worker_failure":\n    time.sleep(0.3)\n    raise SystemExit(17)\n'
            'while True: time.sleep(0.05)\n')
        harness = root / 'harness.py'
        harness.write_text(
            'import importlib.util, os, sys\nfrom pathlib import Path\n'
            f'spec=importlib.util.spec_from_file_location("runtime", {str(SCRIPT)!r})\n'
            'm=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(m)\n'
            f'm.OUTPUT_DIRECTORY=Path({str(output)!r})\nm.MAX_SAMPLE_WAIT=0.7\n'
            f'm.WORKER_COMMAND=[sys.executable, {str(worker)!r}]\n'
            f'm.COLLECTOR_COMMAND=[sys.executable, {str(collector)!r}]\n'
            'original=m.subprocess.Popen\n'
            'def launch(*a, **kw):\n'
            f'    kw["cwd"]={str(root)!r}\n    return original(*a, **kw)\n'
            'm.subprocess.Popen=launch\n'
            f'os.environ["HOMESERVER_MEDIA_UUID"]={UUID!r}\n'
            'raise SystemExit(m.main([]))\n')
        entry_path = SCRIPT.with_name('core-python-entrypoint.py')
        spec = importlib.util.spec_from_file_location('capacity_entry_fixture', entry_path)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        monkeypatch.setattr(entry, 'CAPACITY_DIRECTORY', output)
        deadline = entry.guard.uptime_clock() + 20
        snapshot = dict(token=('fixture',), sequence=1, lease_sha256='a' * 64,
                        expires_at=deadline)
        def checker():
            if mode == 'hold' and (output / 'worker.pid').exists():
                raise entry.guard.GuardError('fixture HOLD')
            return snapshot
        env = dict(os.environ)
        env['PYTHONPATH'] = str(SCRIPT.parent.parent / 'services/common/src')
        # Private test interpreter path needs no application credentials.
        plan = dict(argv=[sys.executable, str(harness)], cwd=str(root))
        result = entry.run_supervised('control-worker', plan, checker, env)
        assert result == 78
        assert not (output / 'capacity.json').exists()
        identities = [json.loads((output / 'collector.pid').read_text())]
        assert identities[0][1:4] == [1000, 1000, []]
        if mode == 'first_failure':
            assert not (output / 'worker.pid').exists()
        else:
            identities.append(json.loads((output / 'worker.pid').read_text()))
            assert identities[1][1:3] == [0, 0]
            assert identities[0][4:] == identities[1][4:]
        for pid, *_ in identities:
            status = Path(f'/proc/{pid}/stat')
            assert not status.exists() or status.read_bytes().rsplit(b')', 1)[1].split()[0] == b'Z'
