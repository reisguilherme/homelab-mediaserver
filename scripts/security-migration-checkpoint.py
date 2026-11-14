"""One-off 2026-10-18 checkpoint. Operator runs this on homelab as root.

Stops only Jellyfin/Seerr after the worker enters maintenance, copies their
configuration, and restarts their original containers. Does not deploy images,
mount media, read .env, or delete checkpoints. Failures keep maintenance in place
unless both original applications are ready. Output never includes DB rows/logs.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path('/var/lib/homeserver/security-hardening-20261018/checkpoints')
CONTROL = Path('/srv/appdata/control')
SERVICES = {
    'jellyfin': ('homeserver-jellyfin-1', Path('/srv/appdata/jellyfin'), '/config'),
    'seerr': ('homeserver-seerr-1', Path('/srv/appdata/seerr'), '/app/config'),
}
WORKER = 'homeserver-control-worker-1'


def run(*args, timeout=60):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Command failed; no command output disclosed')
    return result.stdout


def inspect(name):
    return json.loads(run('docker', 'inspect', name))[0]


def require_mount(container, destination, source):
    mounts = [m for m in container['Mounts'] if m['Destination'] == destination]
    if len(mounts) != 1 or mounts[0]['Type'] != 'bind' or mounts[0]['Source'] != str(source):
        raise RuntimeError('Unexpected container mount')
    if source.resolve() != source or not source.is_dir():
        raise RuntimeError('Unsafe source mount path')


def worker_idle(database, now):
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
        row = connection.execute(
            'SELECT state,last_seen_at,cycle_deadline_at,initialized '
            'FROM worker_heartbeat WHERE id=1'
        ).fetchone()
    return bool(row and row[0] == 'maintenance' and 0 <= now - row[1] <= 30
                and row[2] is None and row[3] == 1)


def wait_for(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(2)
    raise RuntimeError(message)


def verify_sqlite_tree(root):
    report = {}
    for path in sorted(root.rglob('*')):
        if (path.is_symlink() or not path.is_file()
                or path.suffix not in {'.db', '.sqlite', '.sqlite3'}):
            continue
        with path.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\0':
                continue
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10) as connection:
            checks = connection.execute('PRAGMA integrity_check').fetchall()
            if checks != [('ok',)]:
                raise RuntimeError('Copied database integrity check failed')
            tables = [r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            counts = {}
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                counts[table] = connection.execute('SELECT count(*) FROM ' + quoted).fetchone()[0]
            report[str(path.relative_to(root))] = {'integrity': 'ok', 'tables': counts}
    return report


def ready(service):
    state = inspect(SERVICES[service][0])['State']
    if not state['Running']:
        return False
    if service == 'jellyfin':
        return state.get('Health', {}).get('Status') == 'healthy'
    result = subprocess.run(
        ['docker', 'exec', SERVICES[service][0], 'node', '-e',
         'fetch("http://127.0.0.1:5055/api/v1/status",'
         '{signal:AbortSignal.timeout(4000)}).then(r=>process.exit(r.ok?0:1))'
         '.catch(()=>process.exit(1))'], capture_output=True, timeout=8,
    )
    return result.returncode == 0


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Root required for private consistent checkpoint')
    os.umask(0o077)
    worker = inspect(WORKER)
    require_mount(worker, '/run/homeserver', CONTROL)
    require_mount(worker, '/var/lib/homeserver', CONTROL)
    if not worker['State']['Running']:
        raise RuntimeError('Worker must be running before checkpoint')
    original = {}
    for service, (name, source, destination) in SERVICES.items():
        container = inspect(name)
        require_mount(container, destination, source)
        if container['Config']['Labels'].get('com.docker.compose.service') != service:
            raise RuntimeError('Unexpected container identity')
        if not ready(service):
            raise RuntimeError('Original applications must be ready')
        original[service] = {
            'name': name, 'image': container['Config']['Image'], 'image_id': container['Image'],
            'source': str(source), 'destination': destination, 'user': container['Config']['User'],
        }
    if ROOT.resolve() != ROOT:
        raise RuntimeError('Unsafe checkpoint directory')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    # Reserve substantially more than current Jellyfin+Seerr configuration size.
    if shutil.disk_usage(ROOT).free < 20 * 1024**3:
        raise RuntimeError('Less than 20 GiB free for checkpoint')
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    target = ROOT / stamp
    target.mkdir(mode=0o700)
    maintenance = CONTROL / 'maintenance'
    marker = 'security-checkpoint-' + stamp
    with maintenance.open('x', encoding='utf-8') as stream:
        stream.write(marker)
    manifest = {'complete': False, 'original': original, 'created': stamp}
    interrupted = []
    try:
        wait_for(lambda: worker_idle(CONTROL / 'control.sqlite', time.time()), 960,
                 'Worker did not reach an idle maintenance boundary')
        for name, _, _ in SERVICES.values():
            interrupted.append(name)
            run('docker', 'kill', '--signal=SIGTERM', name)
        for name in interrupted:
            wait_for(lambda name=name: not inspect(name)['State']['Running'], 180,
                     'Application did not stop gracefully; no snapshot copied')
        for service, (_, source, _) in SERVICES.items():
            run('cp', '--archive', '--reflink=auto', '--', str(source), str(target / service),
                timeout=600)
        # Original apps can restart immediately; integrity checks use copies only.
        for name in interrupted:
            run('docker', 'start', name)
        manifest['databases'] = {
            service: verify_sqlite_tree(target / service) for service in SERVICES
        }
        manifest['complete'] = True
    finally:
        for name in interrupted:
            if not inspect(name)['State']['Running']:
                run('docker', 'start', name)
        try:
            for service in SERVICES:
                wait_for(lambda service=service: ready(service), 180,
                         'Original application readiness did not recover')
            if maintenance.read_text(encoding='utf-8') == marker:
                maintenance.unlink()
            manifest['original_services_ready'] = True
        finally:
            (target / 'manifest.private.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'checkpoint': str(target), 'complete': manifest['complete'],
                      'original_services_ready': manifest.get('original_services_ready', False),
                      'database_integrity': 'ok'}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Only RuntimeError messages are authored fixed strings in this helper;
        # arbitrary native exceptions may include private filesystem/log content.
        reason = str(error) if type(error) is RuntimeError else 'Unclassified helper failure'
        print(json.dumps({'checkpoint_failed': type(error).__name__, 'reason': reason,
                          'action': 'Inspect private checkpoint manifest and maintenance state'}))
        raise SystemExit(1) from None
