"""Due import receipts recover before unstarted files without changing eligibility."""

import sqlite3

import pytest
from test_audited_import_store import manifest

from homeserver_control.persistence.audited_imports import AuditedImportStore


def queued(tmp_path):
    data = manifest()
    job = data['jobs'][0]
    targets = [
        {'path': f'Show/Show.S01E{index + 1:02}.mkv', 'size_bytes': 10,
         'kind': 'episode', 'native_series_id': 5, 'native_episode_ids': [index + 1],
         'canonical_episode_ids': [100 + index], 'scopes': [f'S01E{index + 1:02}']}
        for index in range(8)
    ]
    job.update(expected_payload_bytes=80, padding_bytes=0, files=targets,
               payload_files=[{'path': target['path'], 'size_bytes': 10} for target in targets],
               excluded_files=[])
    database = tmp_path / 'control.sqlite'
    store = AuditedImportStore(database)
    store.install_manifest(data)
    with sqlite3.connect(database) as connection:
        ids = [row[0] for row in connection.execute(
            'SELECT file_id FROM audited_import_files ORDER BY ordinal')]
    return store, database, ids


def set_state(database, file_id, state, next_attempt, last_attempt=0):
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE audited_import_files SET state=?,next_attempt=?,last_attempt=? WHERE file_id=?',
            (state, next_attempt, last_attempt, file_id),
        )


@pytest.mark.parametrize('state', ['import_unknown', 'import_requested'])
def test_due_recovery_receipt_precedes_older_unstarted_files(tmp_path, state):
    store, database, ids = queued(tmp_path)
    set_state(database, ids[6], state, next_attempt=90, last_attempt=300)
    rows = store.pending_files(now=100, limit=4)
    assert [row['file_id'] for row in rows] == [ids[6], *ids[:3]]
    assert rows[0]['state'] == state and len(rows) == 4


@pytest.mark.parametrize('state', ['import_unknown', 'import_requested'])
def test_future_recovery_never_bypasses_its_retry_eligibility(tmp_path, state):
    store, database, ids = queued(tmp_path)
    set_state(database, ids[6], state, next_attempt=101)
    assert [row['file_id'] for row in store.pending_files(now=100)] == ids[:4]
    assert [row['file_id'] for row in store.pending_files(now=101)] == [ids[6], *ids[:3]]


def test_equal_recovery_priority_preserves_retry_then_attempt_then_ordinal(tmp_path):
    store, database, ids = queued(tmp_path)
    set_state(database, ids[7], 'import_unknown', 80, last_attempt=30)
    set_state(database, ids[6], 'import_requested', 80, last_attempt=20)
    set_state(database, ids[5], 'import_unknown', 70, last_attempt=99)
    assert [row['file_id'] for row in store.pending_files(now=100)] == [
        ids[5], ids[6], ids[7], ids[0],
    ]


def test_due_subtitle_has_one_slot_after_import_work_and_complete_is_excluded(tmp_path):
    store, database, ids = queued(tmp_path)
    set_state(database, ids[6], 'subtitle_pending', next_attempt=90)
    set_state(database, ids[7], 'complete', next_attempt=0)
    assert [row['file_id'] for row in store.pending_files(now=100)] == [*ids[:3], ids[6]]
    set_state(database, ids[5], 'import_requested', next_attempt=90)
    assert [row['file_id'] for row in store.pending_files(now=100)] == [ids[5], *ids[:2], ids[6]]
    assert ids[7] not in [row['file_id'] for row in store.pending_files(now=100, limit=8)]


@pytest.mark.parametrize('state', ['imported', 'subtitle_pending'])
@pytest.mark.parametrize('retry_at', [0, 10])
def test_due_new_imports_precede_older_subtitle_work_without_increasing_batch(
    tmp_path, state, retry_at,
):
    store, database, ids = queued(tmp_path)
    for file_id in ids:
        set_state(database, file_id, 'planned', next_attempt=90, last_attempt=80)
    for file_id in ids[:4]:
        set_state(database, file_id, state, next_attempt=retry_at, last_attempt=1)

    rows = store.pending_files(now=100)

    assert [row['file_id'] for row in rows] == [*ids[4:7], ids[0]]
    assert len(rows) == 4
    assert [row['state'] for row in rows] == ['planned'] * 3 + [state]


def test_interrupted_subtitle_after_mark_imported_does_not_retake_new_import_slot(tmp_path):
    store, database, ids = queued(tmp_path)
    for file_id in ids:
        set_state(database, file_id, 'planned', next_attempt=90)
    # The durable transition happens before awaiting subtitles. A cycle cancellation
    # can leave it here, with next_attempt reset to zero and no subtitle backoff yet.
    for _attempt in range(3):
        store.mark_imported(ids[0], str(tmp_path / 'native.mkv'))
        assert [row['file_id'] for row in store.pending_files(now=100)] == [*ids[1:4], ids[0]]


def test_planned_priority_never_bypasses_retry_time_or_drops_due_subtitles(tmp_path):
    store, database, ids = queued(tmp_path)
    for file_id in ids:
        set_state(database, file_id, 'planned', next_attempt=101)
    set_state(database, ids[0], 'subtitle_pending', next_attempt=70)
    set_state(database, ids[1], 'imported', next_attempt=80)

    assert [row['file_id'] for row in store.pending_files(now=100)] == ids[:2]
    assert [row['file_id'] for row in store.pending_files(now=101)] == [*ids[2:5], ids[0]]


def test_recovery_then_planned_then_subtitle_work_preserves_order_within_each_class(tmp_path):
    store, database, ids = queued(tmp_path)
    for file_id in ids:
        set_state(database, file_id, 'complete', next_attempt=0)
    set_state(database, ids[0], 'subtitle_pending', next_attempt=0)
    set_state(database, ids[1], 'planned', next_attempt=80, last_attempt=50)
    set_state(database, ids[2], 'planned', next_attempt=80, last_attempt=40)
    set_state(database, ids[3], 'import_requested', next_attempt=90)

    assert [row['file_id'] for row in store.pending_files(now=100)] == [
        ids[3], ids[2], ids[1], ids[0],
    ]


@pytest.mark.parametrize('state', ['planned', 'import_unknown', 'import_requested'])
def test_perpetually_due_import_work_cannot_starve_due_subtitles(tmp_path, state):
    store, database, ids = queued(tmp_path)
    for file_id in ids[:4]:
        set_state(database, file_id, state, next_attempt=0)
    for file_id in ids[4:]:
        set_state(database, file_id, 'subtitle_pending', next_attempt=0)
    seen = set()
    for now in (100, 200, 300, 400):
        rows = store.pending_files(now=now)
        assert len(rows) == 4
        assert [row['state'] for row in rows] == [state] * 3 + ['subtitle_pending']
        seen.add(rows[-1]['file_id'])
        for row in rows:
            set_state(database, row['file_id'], row['state'], next_attempt=now + 30,
                      last_attempt=now)
    assert seen == set(ids[4:])


@pytest.mark.parametrize('limit', [1, 2, 3, 4])
def test_small_batches_reserve_one_subtitle_only_when_two_slots_exist(tmp_path, limit):
    store, database, ids = queued(tmp_path)
    for file_id in ids[:4]:
        set_state(database, file_id, 'import_requested', next_attempt=0)
    for file_id in ids[4:]:
        set_state(database, file_id, 'subtitle_pending', next_attempt=0)
    rows = store.pending_files(now=100, limit=limit)
    expected = ids[:1] if limit == 1 else [*ids[:limit - 1], ids[4]]
    assert [row['file_id'] for row in rows] == expected


def test_mixed_recovery_and_new_work_keep_priority_inside_three_import_slots(tmp_path):
    store, database, ids = queued(tmp_path)
    set_state(database, ids[5], 'import_unknown', next_attempt=90)
    set_state(database, ids[6], 'import_requested', next_attempt=80)
    set_state(database, ids[7], 'subtitle_pending', next_attempt=0)
    assert [row['file_id'] for row in store.pending_files(now=100)] == [
        ids[6], ids[5], ids[0], ids[7],
    ]


def test_unused_import_slots_are_filled_with_due_subtitles(tmp_path):
    store, database, ids = queued(tmp_path)
    for file_id in ids[1:]:
        set_state(database, file_id, 'subtitle_pending', next_attempt=0)
    assert [row['file_id'] for row in store.pending_files(now=100)] == ids[:4]
    set_state(database, ids[0], 'complete', next_attempt=0)
    assert [row['file_id'] for row in store.pending_files(now=100)] == ids[1:5]
