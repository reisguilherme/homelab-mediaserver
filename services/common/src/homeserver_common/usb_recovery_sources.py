"""Read-only source and physical-capacity proofs for USB recovery.

This gate grants no new admission. Every native source must retain a unique
confirmed gateway permit, its sealed descriptor and its exclusive placement.
Results are private JSON-safe evidence, never suitable for public logs.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import stat
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from homeserver_common.storage import StorageRegistry, StorageUnavailable
from homeserver_control.domain.torrent_bytes import _Decoder, inspect_torrent
from homeserver_control.gateway.probe_permits import ProbePermits

_HASH = re.compile(r'[0-9a-f]{40}')
_LIVE = {'authorized', 'dispatching', 'unknown', 'confirmed'}
_STATES = {
    'downloading', 'stalledDL', 'queuedDL', 'stoppedDL', 'pausedDL', 'checkingDL',
    'checkingResumeData', 'allocating', 'moving', 'error', 'missingFiles',
    'uploading', 'stalledUP', 'queuedUP', 'stoppedUP', 'pausedUP', 'checkingUP',
}


class SourceHold(RuntimeError):
    """A static sanitized reason, without native names, paths or credentials."""


def _require(condition, code):
    if not condition:
        raise SourceHold(code)


def check_preferences(prefs, expected_max_active_downloads, expected_ignore_slow):
    _require(isinstance(prefs, dict) and prefs.get('queueing_enabled') is True
             and type(expected_max_active_downloads) is int
             and expected_max_active_downloads > 0
             and type(prefs.get('max_active_downloads')) is int
             and prefs['max_active_downloads'] == expected_max_active_downloads
             and type(expected_ignore_slow) is bool
             and prefs.get('dont_count_slow_torrents') is expected_ignore_slow
             and prefs.get('enable_piece_extent_affinity') is True,
             'protected_queue_preferences_drift')


def _info(content):
    decoder = _Decoder(content)
    decoder.offset = 1
    while content[decoder.offset:decoder.offset + 1] != b'e':
        key = decoder.parse(1)
        start = decoder.offset
        value = decoder.parse(1)
        if key == b'info':
            return value, sha256(content[start:decoder.offset]).hexdigest()
    raise SourceHold('sealed_descriptor_info_absent')


def _selected(binding, parsed):
    selected = json.loads(binding['selected_files_json'])
    _require(isinstance(selected, list) and bool(selected)
             and all(isinstance(item, str) for item in selected)
             and len(set(selected)) == len(selected)
             and set(selected).issubset({file.path for file in parsed.files}),
             'sealed_content_selection_mismatch')
    return selected


def _reservation(connection, binding, complete):
    permit = SimpleNamespace(reservation_id=binding['reservation_id'],
                             scope_key=binding['scope_key'])
    if not complete:
        try:
            ProbePermits._require_probe_reservation(connection, permit)
        except PermissionError:
            raise SourceHold('source_request_reservation_cancelled_or_tombstoned') from None
        return
    # Completed seeds can outlive an imported/released reservation, but a
    # cancellation or deletion still revokes recovery authority.
    row = connection.execute(
        'SELECT r.media_key,q.state FROM reservations r JOIN requests q '
        'ON q.id=r.request_id WHERE r.id=?', (permit.reservation_id,),
    ).fetchone()
    if row is None:
        return
    _require(row['state'] not in {'cancel_requested', 'cancelled'}, 'source_request_cancelled')
    keys = [row['media_key']]
    season = re.fullmatch(r'season:tmdb:(\d+):(\d+)', row['media_key'])
    if season and re.fullmatch(r'S\d+E\d+', permit.scope_key or ''):
        keys.append(f'episode:tmdb:{season[1]}:{permit.scope_key}')
    _require(not any(connection.execute('SELECT 1 FROM tombstones WHERE media_key=?',
                                       (key,)).fetchone() for key in keys),
             'source_media_tombstoned')


def _audited_job(connection, binding, parsed, selected, tables):
    if 'audited_import_jobs' not in tables:
        return
    jobs = connection.execute(
        "SELECT job_id,manifest_id,job_json FROM audited_import_jobs "
        "WHERE json_extract(job_json,'$.permit_id')=?", (binding['permit_id'],),
    ).fetchall()
    if not jobs:
        return
    _require(len(jobs) == 1 and 'audited_import_manifest_history' in tables,
             'sealed_job_binding_ambiguous')
    job = json.loads(jobs[0]['job_json'])
    _require(connection.execute(
        'SELECT 1 FROM audited_import_manifest_history WHERE manifest_id=?',
        (jobs[0]['manifest_id'],),
    ).fetchone() is not None and isinstance(job, dict)
        and job.get('job_id') == jobs[0]['job_id']
        and job.get('permit_id') == binding['permit_id']
        and job.get('reservation_id') == binding['reservation_id']
        and job.get('metadata_sha256') == parsed.metadata_sha256
        and job.get('category') == binding['category']
        and job.get('expected_payload_bytes') == parsed.total_bytes == binding['budget_bytes']
        and job.get('payload_files') == [
            {'path': file.path, 'size_bytes': file.length} for file in parsed.files]
        and set(selected) == {file.path for file in parsed.files}, 'sealed_job_binding_mismatch')


def _completed_payload(parsed, registry, pool_id, destination):
    # Released reservations cannot fund a redownload. Native completion alone
    # is insufficient when a payload was removed while library hardlinks remain.
    for file in parsed.files:
        if file.length == 0:
            continue
        try:
            physical = registry.resolve(pool_id, destination + '/' + file.path)
            observed = physical.lstat()
        except (OSError, StorageUnavailable):
            raise SourceHold('completed_payload_missing_or_invalid') from None
        _require(stat.S_ISREG(observed.st_mode) and observed.st_size == file.length,
                 'completed_payload_missing_or_invalid')


def _source(native, binding, connection, registry, tables):
    content = binding['content']
    parsed = inspect_torrent(content)
    _require(binding['state'] == 'confirmed', 'source_permit_not_confirmed')
    _require(binding['infohash'] == binding['artifact_infohash'] == parsed.infohash
             and binding['metadata_sha256'] == binding['artifact_sha256'] == parsed.metadata_sha256
             and type(binding['budget_bytes']) is int
             and parsed.total_bytes <= binding['budget_bytes'],
             'sealed_descriptor_binding_mismatch')
    selected = _selected(binding, parsed)
    info, v2 = _info(content)
    v1 = parsed.infohash
    is_v1 = native['hash'] == v1 and native.get('infohash_v2') in (None, '')
    _require((is_v1 and native.get('infohash_v1') in (None, '', v1)) or (
        parsed.hybrid and native['hash'] == v2[:40]
        and native.get('infohash_v1') == v1 and native.get('infohash_v2') == v2),
        'native_infohash_binding_mismatch')
    total, left, progress = (native.get(key) for key in ('total_size', 'amount_left', 'progress'))
    _require(type(total) is int and total == parsed.total_bytes and type(left) is int
             and 0 <= left <= total and type(progress) in (int, float)
             and math.isfinite(progress) and 0 <= progress <= 1
             and (left == 0) == (progress == 1), 'native_whole_payload_progress_mismatch')
    complete = left == 0
    _require(native.get('state') in _STATES, 'native_source_state_unknown_or_forced')
    _require(complete or native['state'] not in {
        'uploading', 'stalledUP', 'queuedUP', 'stoppedUP', 'pausedUP', 'checkingUP'},
        'native_source_state_completion_mismatch')
    pool = registry.pools.get(binding['pool_id'])
    _require(pool is not None and binding['filesystem_id'] == pool.filesystem_id,
             'source_physical_identity_mismatch')
    destination = registry.validate_destination(binding['pool_id'], binding['permit_id'])
    root_name = info[b'name'].decode('utf-8')
    content_paths = {destination + '/' + root_name}
    # qBit reports the exact payload file for a multifile descriptor containing
    # one content file, including stopped sources which have never downloaded.
    if len(parsed.files) == 1:
        content_paths.add(destination + '/' + parsed.files[0].path)
    content_path = native.get('content_path')
    _require(binding['destination'] == destination
             and isinstance(native.get('save_path'), str)
             and native['save_path'].rstrip('/') == destination
             and isinstance(content_path, str) and content_path in content_paths
             and native.get('category') == binding['category'],
             'native_source_path_binding_mismatch')
    registry.resolve(binding['pool_id'], content_path)
    if complete and native['state'] not in {'stoppedUP', 'stoppedDL', 'pausedUP', 'pausedDL'}:
        _completed_payload(parsed, registry, binding['pool_id'], destination)
    _reservation(connection, binding, complete)
    _audited_job(connection, binding, parsed, selected, tables)
    result = {key: binding[key] for key in (
        'permit_id', 'reservation_id', 'scope_key', 'pool_id', 'filesystem_id',
        'category', 'destination', 'metadata_sha256', 'budget_bytes')}
    result.update(native_id=native['hash'], canonical_infohash=v1, total_size=total,
                  amount_left=left, progress=progress, state=native['state'], complete=complete,
                  infohash_v1=native.get('infohash_v1'), infohash_v2=native.get('infohash_v2'),
                  content_path=content_path, priority=native.get('priority'),
                  selected_files=selected,
                  descriptor_files=[{'name': f.path, 'size': f.length} for f in parsed.files],
                  padding_files=[{'name': f.path, 'size': f.length} for f in parsed.padding_files])
    fingerprint = {key: value for key, value in result.items()
                   if key not in {'amount_left', 'progress', 'state', 'complete', 'priority'}}
    result['binding_fingerprint'] = sha256(json.dumps(
        fingerprint, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return result


def _capacity(connection, bindings, sources, registry, minimum, tables):
    commitments = dict.fromkeys(registry.pool_ids, 0)
    native = {source['permit_id']: source for source in sources}
    active_reservations = {row['id']: dict(row) for row in connection.execute(
        "SELECT * FROM reservations WHERE state IN ('reserved','downloading','waiting_episodes')")}
    represented = set()
    hashes = {}
    for binding in bindings:
        if binding['state'] not in _LIVE:
            continue
        represented.add(binding['reservation_id'])
        source = native.get(binding['permit_id'])
        if source:
            amount = source['amount_left']
        elif (binding['state'] in {'authorized', 'dispatching', 'unknown'}
              or binding['reservation_id'] in active_reservations):
            amount = binding['budget_bytes']
        else:
            continue
        pool = registry.pools.get(binding['pool_id'])
        _require(pool is not None and pool.filesystem_id == binding['filesystem_id']
                 and type(amount) is int and amount >= 0, 'pending_capacity_binding_unknown')
        key = (binding['pool_id'], binding['infohash'])
        hashes[key] = max(hashes.get(key, 0), amount)
    for (pool_id, _), amount in hashes.items():
        commitments[pool_id] += amount
    for reservation_id, row in active_reservations.items():
        if reservation_id in represented:
            continue
        matches = [pool for pool in registry.pools.values()
                   if pool.filesystem_id == row['filesystem_id']]
        _require(len(matches) == 1 and type(row['budget_bytes']) is int
                 and row['budget_bytes'] >= 0, 'reservation_capacity_binding_unknown')
        commitments[matches[0].pool_id] += row['budget_bytes']
    if 'audited_derivations' in tables:
        for row in connection.execute(
            "SELECT * FROM audited_derivations WHERE state IN ('reserved','writing','verified')"):
            pool = registry.pools.get(row['pool_id'])
            _require(pool is not None and pool.filesystem_id == row['filesystem_id']
                     and type(row['budget_bytes']) is int and row['budget_bytes'] > 0,
                     'derivation_capacity_binding_unknown')
            commitments[row['pool_id']] += row['budget_bytes']
    # Fresh physical usage is read at the end, after all ledger/native accounting.
    for pool_id in registry.pool_ids:
        sample = registry.inspect(pool_id)
        _require(sample.state == 'ready' and type(sample.free_bytes) is int
                 and sample.free_bytes - commitments[pool_id] >= minimum, 'physical_capacity_hold')


def _files(source, files, registry):
    _require(isinstance(files, list), 'native_file_vector_unknown')
    expected = {(f['name'], f['size']) for f in source['descriptor_files']}
    padding = {(f['name'], f['size']) for f in source['padding_files']}
    actual, indexes = set(), set()
    priorities = []
    for file in files:
        _require(isinstance(file, dict), 'native_file_vector_invalid')
        name, size, priority, index = (
            file.get(key) for key in ('name', 'size', 'priority', 'index'))
        _require(isinstance(name, str) and type(size) is int and size >= 0
                 and type(index) is int and index >= 0 and index not in indexes
                 and type(priority) is int and priority in {0, 1, 6, 7},
                 'native_file_vector_invalid')
        item = (name, size)
        _require(item not in actual and item in expected | padding, 'native_file_manifest_drift')
        _require(name not in source['selected_files'] or priority > 0,
                 'native_selected_file_disabled')
        actual.add(item)
        indexes.add(index)
        registry.resolve(source['pool_id'], source['destination'] + '/' + name)
        priorities.append({'index': index, 'name': name, 'size': size, 'priority': priority})
    _require(expected.issubset(actual), 'native_file_manifest_drift')
    source['file_priorities'] = sorted(priorities, key=lambda file: file['index'])
    _require([{'name': f['name'], 'size': f['size']} for f in source['file_priorities']
              if (f['name'], f['size']) in expected] == source['descriptor_files'],
             'native_file_index_order_drift')


def _prove(rows, files, database, registry, minimum):
    _require(isinstance(rows, list) and type(minimum) is int and minimum >= 0,
             'recovery_source_input_invalid')
    _require(files is None or isinstance(files, dict), 'native_file_vector_unknown')
    try:
        with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=5)) as connection, \
                registry.read_snapshot() as physical:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            bindings = [dict(row) for row in connection.execute(
                'SELECT p.*,a.infohash AS artifact_infohash,a.metadata_sha256 AS artifact_sha256,'
                'a.content FROM gateway_permits p LEFT JOIN torrent_artifacts a '
                'ON a.permit_id=p.permit_id')]
            by_hash = {}
            for binding in bindings:
                if binding['state'] in _LIVE:
                    by_hash.setdefault(binding['infohash'], []).append(binding)
            for pool_id in physical.pool_ids:
                physical.inspect(pool_id)
            sources, native_ids, canonical_ids = [], set(), set()
            for row in rows:
                _require(isinstance(row, dict) and isinstance(row.get('hash'), str)
                         and _HASH.fullmatch(row['hash']) is not None
                         and row['hash'] not in native_ids, 'native_source_identity_ambiguous')
                native_ids.add(row['hash'])
                canonical = row.get('infohash_v1') or row['hash']
                matches = by_hash.get(canonical, [])
                _require(len(matches) == 1 and matches[0]['state'] == 'confirmed',
                         'native_source_not_uniquely_confirmed')
                source = _source(row, matches[0], connection, physical, tables)
                _require(source['canonical_infohash'] not in canonical_ids,
                         'native_source_identity_ambiguous')
                canonical_ids.add(source['canonical_infohash'])
                if files is not None:
                    _files(source, files.get(source['native_id']), physical)
                sources.append(source)
            _capacity(connection, bindings, sources, registry, minimum, tables)
            return sources
    except SourceHold:
        raise
    except StorageUnavailable:
        raise SourceHold('physical_storage_proof_unavailable') from None
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise SourceHold('sealed_source_evidence_unavailable') from None


def preflight_sources(rows: list[dict], database: Path, registry: StorageRegistry,
                      minimum: int) -> list[dict]:
    """Prove the stored pre-start native snapshot against fresh local evidence."""
    return _prove(rows, None, database, registry, minimum)


def validate_sources(rows: list[dict], files: dict[str, list[dict]], database: Path,
                     registry: StorageRegistry, minimum: int) -> list[dict]:
    """Prove current native identities and complete content/priority vectors."""
    return _prove(rows, files, database, registry, minimum)
