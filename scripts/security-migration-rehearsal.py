"""Run official native migrations on private copies, with no network or media.

Only aggregate counts and preservation booleans leave the host. Native logs and
database fingerprints stay in the private fixture directory. This helper never
opens .env, changes production, or modifies the original checkpoint.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

BASE = Path('/var/lib/homeserver/security-hardening-20261018')
ROOT = BASE / 'migration-fixtures'
CHECKPOINT = BASE / 'checkpoints/20261019T021511Z'
IMAGES = {
    'jellyfin': 'jellyfin/jellyfin@sha256:'
                '357724bf0ae27a672c7cbaa899db2d9abeb13dbd8657ccce750258a4c059d037',
    'seerr': 'ghcr.io/seerr-team/seerr@sha256:'
             '27602401178d54f1964442287b9f23f67a3fa2252645ee8065839ea1c3f69e45',
}
CORE = {
    'users': ['Id', 'Username'],
    'user': ['id', 'username', 'jellyfinUserId', 'userType', 'permissions'],
    'media_request': ['id', 'requestedById', 'mediaId', 'status', 'type'],
    'media': ['id', 'tmdbId', 'tvdbId', 'mediaType', 'status'],
    'season_request': ['id', 'seasonNumber', 'status', 'requestId'],
}


def run(*command, timeout=60):
    result = subprocess.run(command, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('Fixed rehearsal command failed; output withheld')
    return result.stdout


def container_command(service, config, name):
    if (not config.is_relative_to(ROOT) or config.parent.parent != ROOT
            or config.name != service or config.resolve() != config):
        raise RuntimeError('Only exact private fixture mounts are allowed')
    destination = '/config' if service == 'jellyfin' else '/app/config'
    cache_size = '3g' if service == 'jellyfin' else '256m'
    command = [
        'docker', 'run', '--name', name, '--network', 'none', '--init', '--read-only',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--pids-limit', '512',
        '--memory', '4g', '--cpus', '3', '--user', '1000:1000', '--log-driver', 'none',
        '--tmpfs', '/tmp:rw,nosuid,nodev,uid=1000,gid=1000,size=256m',
        '--tmpfs', '/cache:rw,nosuid,nodev,uid=1000,gid=1000,size=' + cache_size,
        '--mount', f'type=bind,src={config},dst={destination}', IMAGES[service],
    ]
    if service == 'jellyfin':
        command += ['--mode', 'MigrateSystem']
    return command


def snapshot(root):
    result = {'databases': {}, 'core': {}}
    for path in sorted(root.rglob('*')):
        if (path.is_symlink() or not path.is_file()
                or path.suffix not in {'.db', '.sqlite', '.sqlite3'}):
            continue
        with path.open('rb') as stream:
            if stream.read(16) != b'SQLite format 3\0':
                continue
        with closing(sqlite3.connect(
            path.as_uri() + '?mode=ro', uri=True, timeout=10,
        )) as connection:
            if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise RuntimeError('Fixture SQLite integrity check failed')
            tables = [r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )]
            counts = {}
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                counts[table] = connection.execute('SELECT count(*) FROM ' + quoted).fetchone()[0]
                if table.lower() not in CORE:
                    continue
                columns = {r[1].lower(): r[1] for r in connection.execute(
                    'PRAGMA table_info(' + quoted + ')'
                )}
                selected = [columns[c.lower()] for c in CORE[table.lower()] if c.lower() in columns]
                projection = ','.join('"' + c.replace('"', '""') + '"' for c in selected)
                rows = [json.dumps(row, default=lambda x: x.hex(), separators=(',', ':'))
                        for row in connection.execute('SELECT ' + projection + ' FROM ' + quoted)]
                digest = hashlib.sha256('\n'.join(sorted(rows)).encode()).hexdigest()
                key = (path.relative_to(root).as_posix() + ':' + table.lower() + ':'
                       + ','.join(c.lower() for c in selected))
                result['core'][key] = {'rows': len(rows), 'sha256': digest}
            result['databases'][str(path.relative_to(root))] = counts
    return result


def verify_jellyfin_completion(path, exit_code):
    """Require positive v12.2 upgrade proof; an unproven no-op is not accepted."""
    if exit_code != 0:
        raise RuntimeError('Jellyfin migration completion unproven; private log retained')
    applied = shutdown = fatal = False
    fatal_pattern = re.compile(
        r'error while starting server|unhandled exception|failed to apply migrations|'
        r'\bmigration\b[^\r\n]*\bfailed\b|kestrel failed to start', re.IGNORECASE,
    )
    with path.open(encoding='utf-8', errors='replace') as stream:
        for line in stream:
            fatal = fatal or fatal_pattern.search(line) is not None
            if 'Migrations have been applied, optimizing the database...' in line:
                applied, shutdown = True, False
            if applied and 'Preparing the database for shutdown...' in line:
                shutdown = True
    if fatal or not applied or not shutdown:
        raise RuntimeError('Jellyfin migration completion unproven; private log retained')


def seerr_ready(name):
    result = subprocess.run(
        ['docker', 'exec', name, 'node', '-e',
         'fetch("http://127.0.0.1:5055/api/v1/status?checkUpdateAvailable=false",'
         '{signal:AbortSignal.timeout(4000)}).then(r=>process.exit(r.ok?0:1))'
         '.catch(()=>process.exit(1))'], capture_output=True, timeout=10,
    )
    return result.returncode == 0


def main(services=tuple(IMAGES)):
    if os.geteuid() != 0:
        raise RuntimeError('Root required for private fixture preparation')
    os.umask(0o077)
    if CHECKPOINT.resolve() != CHECKPOINT or ROOT.resolve() != ROOT:
        raise RuntimeError('Unsafe checkpoint or fixture root')
    original = json.loads((CHECKPOINT / 'manifest.private.json').read_text())
    if not original.get('complete') or not original.get('original_services_ready'):
        raise RuntimeError('Checkpoint is not complete and confirmed')
    ROOT.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    target = ROOT / stamp
    target.mkdir(mode=0o700)
    report = {'checkpoint': str(CHECKPOINT), 'images': IMAGES, 'completed': False}
    for service in services:
        source = CHECKPOINT / service
        if source.resolve() != source or not source.is_dir():
            raise RuntimeError('Unsafe checkpoint source for private fixture')
        run('docker', 'image', 'inspect', IMAGES[service])
        run('cp', '--archive', '--reflink=auto', '--', str(CHECKPOINT / service),
            str(target / service), timeout=600)
    before = {service: snapshot(target / service) for service in services}
    processes = {}
    try:
        for service in services:
            name = f'homeserver-migration-{service}-{stamp.lower()}'
            log = (target / f'{service}.private.log').open('wb')
            process = subprocess.Popen(
                container_command(service, target / service, name),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            )
            processes[service] = (name, process, log)
            if service == 'jellyfin':
                exit_code = process.wait(timeout=1200)
                log.flush()
                verify_jellyfin_completion(target / 'jellyfin.private.log', exit_code)
                report['jellyfin_completion_verified'] = True
            else:
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError('Seerr exited before readiness; private log retained')
                    if seerr_ready(name):
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError('Seerr migration readiness timed out')
                run('docker', 'stop', '--time', '60', name, timeout=90)
                process.wait(timeout=15)
        after = {service: snapshot(target / service) for service in services}
        preserved = {
            service: {key: after[service]['core'].get(key) == value
                      for key, value in before[service]['core'].items()}
            for service in services
        }
        report.update(before=before, after=after, preserved=preserved)
        if not preserved or any(not values or not all(values.values())
                                for values in preserved.values()):
            raise RuntimeError('Fixture core preservation failed; private report retained')
        report['completed'] = True
        (target / 'report.private.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'fixture': str(target), 'official_migrations_completed': True,
                          'sqlite_integrity': 'ok', 'core_preservation': preserved,
                          'core_counts': {s: {k: v['rows'] for k, v in before[s]['core'].items()}
                                          for s in services},
                          'network': 'none', 'media_mounted': False,
                          'jellyfin_started_for_playback': False}))
    finally:
        for name, process, log in processes.values():
            if process.poll() is None:
                subprocess.run(['docker', 'stop', '--time', '30', name], capture_output=True,
                               timeout=60)
                process.wait(timeout=15)
            log.close()
        (target / 'report.private.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--service', choices=tuple(IMAGES))
        args = parser.parse_args()
        main((args.service,) if args.service else tuple(IMAGES))
    except Exception as error:
        reason = str(error) if type(error) is RuntimeError else 'Unclassified rehearsal failure'
        print(json.dumps({'migration_failed': type(error).__name__, 'reason': reason}))
        raise SystemExit(1) from None
