"""Recovery proofs use temporary real ledgers, metadata, mounts and claims."""

import json
import sqlite3
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_third_storage_pool import registry as storage_fixture

from homeserver_common.usb_recovery_sources import (
    SourceHold,
    check_preferences,
    preflight_sources,
    validate_sources,
)
from homeserver_control.domain.torrent_bytes import inspect_torrent


def encode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(encode(item) for item in value) + b'e'
    return b'd' + b''.join(encode(k) + encode(v) for k, v in sorted(value.items())) + b'e'


def descriptor(*, hybrid=False, sole=False):
    info = {
        b'files': [{b'length': 0, b'path': [b'aux.nfo']},
                   {b'length': 123, b'path': [b'video.mkv']}],
        b'name': b'fixture', b'piece length': 16384, b'pieces': b'p' * 20,
    }
    if hybrid:
        info[b'files'].append({b'attr': b'p', b'length': 16261,
                               b'path': [b'.pad', b'16261']})
        info.update({b'meta version': 2, b'file tree': {
            b'aux.nfo': {b'': {b'length': 0}},
            b'video.mkv': {b'': {b'length': 123, b'pieces root': b'r' * 32}},
        }})
    if sole:
        info[b'files'].pop(0)
        if hybrid:
            del info[b'file tree'][b'aux.nfo']
    return encode({b'info': info}), sha256(encode(info)).hexdigest()


@pytest.fixture
def source(tmp_path):
    registry = storage_fixture.__wrapped__(tmp_path)
    registry._usage = lambda _: SimpleNamespace(total=100_000, used=10_000, free=90_000)
    database = tmp_path / 'control.sqlite'
    with sqlite3.connect(database) as c:
        c.executescript('''
            CREATE TABLE gateway_permits (
                permit_id TEXT PRIMARY KEY, reservation_id TEXT, scope_key TEXT,
                infohash TEXT, metadata_sha256 TEXT, destination TEXT, category TEXT,
                selected_files_json TEXT, budget_bytes INTEGER, state TEXT,
                pool_id TEXT, filesystem_id TEXT);
            CREATE TABLE torrent_artifacts (
                permit_id TEXT PRIMARY KEY, infohash TEXT, metadata_sha256 TEXT, content BLOB);
            CREATE TABLE reservations (
                id TEXT PRIMARY KEY, request_id TEXT, media_key TEXT, budget_bytes INTEGER,
                state TEXT, filesystem_id TEXT);
            CREATE TABLE requests (id TEXT PRIMARY KEY, state TEXT);
            CREATE TABLE tombstones (media_key TEXT PRIMARY KEY);
            CREATE TABLE audited_derivations (
                file_id TEXT PRIMARY KEY, pool_id TEXT, filesystem_id TEXT,
                budget_bytes INTEGER, state TEXT);
        ''')
        c.execute("INSERT INTO requests VALUES ('request', 'downloading')")
        c.execute("INSERT INTO reservations VALUES "
                  "('reservation','request','movie:tmdb:1',123,'downloading','hdd-uuid')")
    return SimpleNamespace(database=database, registry=registry)


def add_source(source, *, hybrid=False, sole=False, selected=None, permit='permit',
               reservation='reservation'):
    content, v2 = descriptor(hybrid=hybrid, sole=sole)
    parsed = inspect_torrent(content)
    destination = source.registry.prepare_destination('hdd', permit)
    selected = selected if selected is not None else (
        ['fixture/video.mkv'] if sole else ['fixture/aux.nfo', 'fixture/video.mkv'])
    with sqlite3.connect(source.database) as c:
        c.execute('INSERT INTO gateway_permits VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', (
            permit, reservation, None, parsed.infohash, parsed.metadata_sha256,
            destination, 'radarr', json.dumps(selected), parsed.total_bytes,
            'confirmed', 'hdd', 'hdd-uuid'))
        c.execute('INSERT INTO torrent_artifacts VALUES (?,?,?,?)', (
            permit, parsed.infohash, parsed.metadata_sha256, content))
    row = {'hash': v2[:40] if hybrid else parsed.infohash,
           'infohash_v1': parsed.infohash, 'infohash_v2': v2 if hybrid else '',
           'category': 'radarr', 'save_path': destination,
           'content_path': destination + '/fixture', 'name': 'fixture',
           'total_size': parsed.total_bytes, 'amount_left': 100, 'progress': .2,
           'state': 'error', 'priority': 3}
    files = [{'index': 0, 'name': 'fixture/aux.nfo', 'size': 0, 'priority': 1},
             {'index': 1, 'name': 'fixture/video.mkv', 'size': 123, 'priority': 6}]
    if sole:
        files = [dict(files[1], index=0)]
    return row, files


def execute(source, statement, parameters=()):
    with sqlite3.connect(source.database) as c:
        c.execute(statement, parameters)


def preflight(source, rows):
    return preflight_sources(rows, source.database, source.registry, 100)


def write_payload(source, row, *, size=123):
    payload = source.registry.resolve('hdd', row['save_path'] + '/fixture/video.mkv')
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_bytes(b'p' * size)
    return payload


def test_preflight_normalizes_confirmed_source_without_writes(source):
    row, _ = add_source(source)
    before = source.database.read_bytes()
    result = preflight(source, [row])[0]
    assert result['native_id'] == result['canonical_infohash'] == row['hash']
    assert result['permit_id'] == 'permit' and result['reservation_id'] == 'reservation'
    assert result['pool_id'] == 'hdd' and result['filesystem_id'] == 'hdd-uuid'
    assert result['total_size'] == result['budget_bytes'] == 123
    assert result['amount_left'] == 100 and result['complete'] is False
    assert source.database.read_bytes() == before


def test_hybrid_half_alias_padding_and_zero_length_content_are_proved(source):
    row, files = add_source(source, hybrid=True)
    result = validate_sources([row], {row['hash']: files}, source.database, source.registry, 100)
    assert result[0]['canonical_infohash'] == row['infohash_v1'] != row['hash']
    assert result[0]['total_size'] == 16384
    assert result[0]['file_priorities'] == [
        {'index': 0, 'name': 'fixture/aux.nfo', 'size': 0, 'priority': 1},
        {'index': 1, 'name': 'fixture/video.mkv', 'size': 123, 'priority': 6},
    ]
    files.append({'index': 2, 'name': 'fixture/.pad/16261', 'size': 16261, 'priority': 0})
    assert validate_sources([row], {row['hash']: files}, source.database, source.registry, 100)


@pytest.mark.parametrize(('state', 'left', 'progress'), [
    ('stoppedDL', 123, 0), ('stalledUP', 0, 1),
])
def test_sole_multifile_payload_content_path_is_admitted_file(source, state, left, progress):
    row, files = add_source(source, sole=True)
    row.update(state=state, amount_left=left, progress=progress,
               content_path=row['save_path'] + '/fixture/video.mkv')
    if left == 0:
        write_payload(source, row)
    proof = validate_sources([row], {row['hash']: files}, source.database, source.registry, 100)
    assert proof[0]['content_path'] == row['save_path'] + '/fixture/video.mkv'


@pytest.mark.parametrize('path', ['', '/fixture/other.mkv', '/fixture/video.mkv/descendant',
                                  '/fixture/../video.mkv'])
def test_sole_payload_rejects_empty_or_arbitrary_descendant_content_path(source, path):
    row, _ = add_source(source, sole=True)
    row['content_path'] = row['save_path'] + path if path else ''
    with pytest.raises(SourceHold):
        preflight(source, [row])


@pytest.mark.parametrize('field,value', [
    ('infohash_v1', 'a' * 40), ('infohash_v2', 'b' * 64),
    ('save_path', '/data/torrents'), ('content_path', '/data/torrents/fixture'),
    ('total_size', 123), ('progress', float('nan')), ('amount_left', -1),
    ('category', 'custom'), ('state', 'forcedDL'),
])
def test_native_identity_destination_size_progress_or_force_drift_holds(source, field, value):
    row, _ = add_source(source, hybrid=True)
    row[field] = value
    with pytest.raises(SourceHold):
        preflight(source, [row])


@pytest.mark.parametrize('mutation', ['artifact', 'digest', 'budget', 'selection', 'unknown',
                                       'filesystem', 'duplicate_permit'])
def test_sealed_permit_drift_holds(source, mutation):
    row, _ = add_source(source)
    statements = {
        'artifact': "UPDATE torrent_artifacts SET content=x'61'",
        'digest': "UPDATE torrent_artifacts SET metadata_sha256='changed'",
        'budget': 'UPDATE gateway_permits SET budget_bytes=1',
        'selection': "UPDATE gateway_permits SET selected_files_json='[\"other.mkv\"]'",
        'unknown': "UPDATE gateway_permits SET state='unknown'",
        'filesystem': "UPDATE gateway_permits SET filesystem_id='another-volume'",
        'duplicate_permit': "INSERT INTO gateway_permits SELECT 'second',reservation_id,scope_key,"
        'infohash,metadata_sha256,destination,category,selected_files_json,budget_bytes,state,'
        'pool_id,filesystem_id FROM gateway_permits',
    }
    execute(source, statements[mutation])
    with pytest.raises(SourceHold):
        preflight(source, [row])


@pytest.mark.parametrize('mutation', ['cancel', 'released', 'tombstone', 'episode_tombstone'])
def test_incomplete_reservation_or_tombstone_holds(source, mutation):
    row, _ = add_source(source)
    if mutation == 'cancel':
        execute(source, "UPDATE requests SET state='cancel_requested'")
    elif mutation == 'released':
        execute(source, "UPDATE reservations SET state='imported'")
    elif mutation == 'tombstone':
        execute(source, "INSERT INTO tombstones VALUES ('movie:tmdb:1')")
    else:
        execute(source, "UPDATE reservations SET media_key='season:tmdb:1:1'")
        execute(source, "UPDATE gateway_permits SET scope_key='S01E02'")
        execute(source, "INSERT INTO tombstones VALUES ('episode:tmdb:1:S01E02')")
    with pytest.raises(SourceHold):
        preflight(source, [row])


def test_completed_seed_uses_integrity_proof_after_reservation_release(source):
    row, _ = add_source(source)
    row.update(amount_left=0, progress=1, state='stalledUP')
    execute(source, "UPDATE reservations SET state='imported'")
    write_payload(source, row)
    assert preflight(source, [row])[0]['complete'] is True


@pytest.mark.parametrize('physical', ['missing', 'truncated'])
def test_completed_released_seed_requires_existing_exact_payload(source, physical):
    row, _ = add_source(source)
    row.update(amount_left=0, progress=1, state='stalledUP')
    execute(source, "UPDATE reservations SET state='imported'")
    if physical == 'truncated':
        write_payload(source, row, size=122)
    with pytest.raises(SourceHold, match='completed_payload_missing_or_invalid'):
        preflight(source, [row])
    write_payload(source, row)
    assert preflight(source, [row])[0]['complete'] is True


def test_completed_hybrid_ignores_absent_virtual_padding_and_empty_auxiliary(source):
    row, files = add_source(source, hybrid=True)
    row.update(amount_left=0, progress=1, state='stalledUP')
    execute(source, "UPDATE reservations SET state='imported'")
    write_payload(source, row)
    assert validate_sources([row], {row['hash']: files}, source.database, source.registry, 100)


@pytest.mark.parametrize('state', ['stoppedUP', 'stoppedDL', 'pausedUP', 'pausedDL'])
def test_completed_manually_stopped_source_can_remain_stopped_without_payload(source, state):
    row, files = add_source(source)
    row.update(amount_left=0, progress=1, state=state)
    execute(source, "UPDATE reservations SET state='imported'")
    proof = validate_sources([row], {row['hash']: files}, source.database, source.registry, 100)
    assert proof[0]['complete'] is True and proof[0]['state'] == state


def test_duplicate_native_or_canonical_aliases_and_unowned_sources_hold(source):
    row, _ = add_source(source, hybrid=True)
    for rows in ([row, row], [row, dict(row, hash=row['infohash_v1'], infohash_v2='')],
                 [dict(row, hash='f' * 40)]):
        with pytest.raises(SourceHold):
            preflight(source, rows)


@pytest.mark.parametrize('mutation', ['disabled_selected', 'renamed', 'size', 'missing_zero',
                                       'duplicate_index', 'unexpected', 'priority_bool'])
def test_content_vector_selection_and_priorities_are_exact(source, mutation):
    row, files = add_source(source)
    if mutation == 'disabled_selected':
        files[1]['priority'] = 0
    elif mutation == 'renamed':
        files[1]['name'] = 'fixture/renamed.mkv'
    elif mutation == 'size':
        files[1]['size'] = 122
    elif mutation == 'missing_zero':
        files.pop(0)
    elif mutation == 'duplicate_index':
        files[1]['index'] = 0
    elif mutation == 'unexpected':
        files.append({'index': 2, 'name': 'fixture/other', 'size': 1, 'priority': 1})
    else:
        files[1]['priority'] = True
    with pytest.raises(SourceHold):
        validate_sources([row], {row['hash']: files}, source.database, source.registry, 100)


def test_capacity_counts_native_remaining_pending_budgets_and_derivations(source):
    row, _ = add_source(source)
    execute(source, "INSERT INTO gateway_permits SELECT 'pending',reservation_id,scope_key,"
            "?,metadata_sha256,destination,category,selected_files_json,400,'dispatching',"
            'pool_id,filesystem_id FROM gateway_permits', ('f' * 40,))
    execute(source, "INSERT INTO audited_derivations VALUES ('copy','hdd','hdd-uuid',50,'writing')")
    source.registry._usage = lambda _: SimpleNamespace(total=1000, used=350, free=650)
    assert preflight(source, [row])  # 650 - (100 + 400 + 50) == minimum.
    source.registry._usage = lambda _: SimpleNamespace(total=1000, used=351, free=649)
    with pytest.raises(SourceHold):
        preflight(source, [row])


def test_capacity_rechecks_all_three_pools_and_claims(source):
    row, _ = add_source(source)
    assert preflight(source, [row])
    source.registry.host_mountinfo.write_text(source.registry.host_mountinfo.read_text()
                                            .replace('8:3', '8:9'))
    with pytest.raises(SourceHold):
        preflight(source, [row])


def test_revoked_claim_during_snapshot_never_publishes_source_proof(source):
    row, _ = add_source(source)

    class RevokingRows(list):
        def __iter__(self):
            yield row
            (source.registry.placement_claims / 'permit.json').unlink()

    with pytest.raises(SourceHold):
        preflight(source, RevokingRows([row]))


def test_content_indices_preserve_descriptor_order(source):
    row, files = add_source(source)
    files[0]['index'], files[1]['index'] = 1, 0
    with pytest.raises(SourceHold):
        validate_sources([row], {row['hash']: files}, source.database, source.registry, 100)


def test_missing_database_does_not_create_or_initialize_it(source):
    absent = source.database.with_name('absent.sqlite')
    with pytest.raises(SourceHold):
        preflight_sources([], absent, source.registry, 100)
    assert not absent.exists()


@pytest.mark.parametrize('field,value', [('queueing_enabled', False), ('max_active_downloads', 49),
                                       ('dont_count_slow_torrents', True),
                                       ('enable_piece_extent_affinity', False)])
def test_preferences_drift_holds(field, value):
    prefs = {'queueing_enabled': True, 'max_active_downloads': 50,
             'dont_count_slow_torrents': False, 'enable_piece_extent_affinity': True}
    assert check_preferences(prefs, 50, False) is None
    prefs[field] = value
    with pytest.raises(SourceHold):
        check_preferences(prefs, 50, False)


@pytest.mark.parametrize('limit', [5, 10, 50])
@pytest.mark.parametrize('ignore_slow', [False, True])
def test_preferences_accept_the_configured_download_limit(limit, ignore_slow):
    prefs = {'queueing_enabled': True, 'max_active_downloads': limit,
             'dont_count_slow_torrents': ignore_slow, 'enable_piece_extent_affinity': True}
    assert check_preferences(prefs, limit, ignore_slow) is None


@pytest.mark.parametrize('expected', [True, 0, -1, '5', None])
def test_preferences_reject_invalid_expected_download_limit(expected):
    prefs = {'queueing_enabled': True, 'max_active_downloads': 5,
             'dont_count_slow_torrents': False, 'enable_piece_extent_affinity': True}
    with pytest.raises(SourceHold, match='protected_queue_preferences_drift'):
        check_preferences(prefs, expected, False)


@pytest.mark.parametrize('expected', [0, 1, 'true', None])
def test_preferences_reject_nonboolean_expected_ignore_slow(expected):
    prefs = {'queueing_enabled': True, 'max_active_downloads': 5,
             'dont_count_slow_torrents': True, 'enable_piece_extent_affinity': True}
    with pytest.raises(SourceHold, match='protected_queue_preferences_drift'):
        check_preferences(prefs, 5, expected)


@pytest.mark.parametrize('expected', [False, True])
def test_preferences_reject_configured_ignore_slow_drift(expected):
    prefs = {'queueing_enabled': True, 'max_active_downloads': 5,
             'dont_count_slow_torrents': not expected, 'enable_piece_extent_affinity': True}
    with pytest.raises(SourceHold, match='protected_queue_preferences_drift'):
        check_preferences(prefs, 5, expected)
