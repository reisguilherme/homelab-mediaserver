import json
from types import SimpleNamespace

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.__main__ import _publish_storage_queue
from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence


def test_queue_summary_separates_stopped_sources_without_releasing_capacity(tmp_path):
    database = tmp_path / 'control.sqlite'
    ReservationRepository(database).initialize()
    permits = PermitRegistry(database)
    stopped = {f'{i:040x}': i * 100 for i in range(1, 24)}
    active = {'a' * 40: 300, 'b' * 40: 1200}
    # Completed torrents contribute no outstanding bytes, whether seeding or stopped.
    completed = {'c' * 40: 0}
    evidence = CapacityEvidence(100_000, {}, pools=(PoolCapacityEvidence(
        'ssd', 'ssd-uuid', 100_000, {**stopped, **active, **completed},
        paused_hashes=frozenset((*stopped, *completed)),
    ),))
    pending = permits.pending_bytes(evidence, pool_id='ssd')
    path = tmp_path / 'storage-queue.json'

    _publish_storage_queue(path, evidence, permits)

    pool = json.loads(path.read_text())['pools'][0]
    assert pool['active_pending_bytes'] == 1500
    assert pool['stopped_pending_bytes'] == sum(stopped.values())
    assert pool['pending_bytes'] == pending == 29_100
    assert pool['available_bytes'] == 100_000 - pending
    assert permits.pending_bytes(evidence, pool_id='ssd') == pending


def test_queue_summary_keeps_awaiting_dispatch_and_unmanaged_bytes_in_queue(tmp_path):
    evidence = CapacityEvidence(100, {}, pools=(
        PoolCapacityEvidence('ssd', 'ssd-uuid', 100, {'a' * 40: 20, 'b' * 40: 10},
                             other_pending_bytes=5, paused_hashes=frozenset({'b' * 40})),
        PoolCapacityEvidence('hdd', 'hdd-uuid', 200, {'c' * 40: 30}),
    ))
    # Includes a permit awaiting dispatch in addition to the measured native queue.
    permits = SimpleNamespace(pending_bytes=lambda _, pool_id: {'ssd': 42, 'hdd': 30}[pool_id])
    path = tmp_path / 'storage-queue.json'

    _publish_storage_queue(path, evidence, permits)

    ssd, hdd = json.loads(path.read_text())['pools']
    assert ssd['active_pending_bytes'] == 32
    assert ssd['stopped_pending_bytes'] == 10
    assert ssd['available_bytes'] == 58
    assert hdd['active_pending_bytes'] == 30
    assert hdd['stopped_pending_bytes'] == 0
    assert hdd['available_bytes'] == 170
