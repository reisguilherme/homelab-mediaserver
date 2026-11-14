"""Real temporary storage/claim tests for request-local capacity proofs."""
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_storage_pools import registry as storage_fixture

from homeserver_common.storage import StorageUnavailable
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository


@pytest.fixture
def registry(tmp_path):
    return storage_fixture.__wrapped__(tmp_path)


def queue(registry, count=20):
    permits = PermitRegistry()
    entries = []
    for index in range(count):
        permit = permits.issue(
            infohash=f'{index + 1:040x}', destination='/data/torrents',
            category='radarr', expires_at=datetime.now(UTC) + timedelta(minutes=10),
            budget_bytes=100, metadata_sha256='a' * 64,
            selected_files=('movie.mkv',), reported_seeders=1,
        )
        permit.pool_id = ('ssd', 'hdd')[index % 2]
        permit.filesystem_id = registry.pools[permit.pool_id].filesystem_id
        permit.destination = registry.prepare_destination(permit.pool_id, permit.permit_id)
        permit.state = 'confirmed'
        entries.append({'hash': permit.infohash, 'save_path': permit.destination,
                        'category': 'radarr', 'total_size': 100, 'amount_left': 25,
                        'state': 'downloading', 'name': 'private name omitted'})
    database = registry.placement_claims.parent / 'control.sqlite'
    ReservationRepository(database).initialize()
    persistent = PermitRegistry(database)
    columns = ('permit_id', 'token', 'operation_id', 'infohash', 'metadata_sha256',
               'destination', 'category', 'selected_files_json', 'budget_bytes',
               'reported_seeders', 'expires_at', 'state', 'pool_id', 'filesystem_id')
    with persistent._session() as connection:
        for permit in permits._permits.values():
            data = {key: getattr(permit, key) for key in columns
                    if key not in {'selected_files_json', 'expires_at'}}
            data['selected_files_json'] = json.dumps(permit.selected_files)
            data['expires_at'] = permit.expires_at.isoformat()
            connection.execute(
                'INSERT INTO gateway_permits (' + ','.join(columns) + ') VALUES ('
                + ','.join('?' for _ in columns) + ')', tuple(data[key] for key in columns),
            )
        connection.commit()
    persistent.storage_registry = registry
    persistent._permits = permits._permits
    return persistent, entries


def client_for(permits, entries):
    class Upstream:
        def read(self, path, params=None):
            assert path == '/api/v2/torrents/info' and params is None
            return entries

    return TestClient(create_app(permits=permits, upstream=Upstream(), arr_token='fixture'))


def test_capacity_get_reuses_physical_pool_proof_within_one_request(registry, monkeypatch):
    # Reintroducing per-torrent inspect makes physical-evidence I/O scale with N.
    permits, entries = queue(registry)
    old = [permits.queue_entry(entry) for entry in entries]
    reads = []
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path in {registry.host_mountinfo, registry.mountinfo}:
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read)
    response = client_for(permits, entries).get(
        '/internal/queue-capacity', headers={'X-Arr-Token': 'fixture'},
    )
    assert response.status_code == 200
    assert response.json() == old
    assert len(response.json()) == 20
    assert all(row['placement_verified'] for row in response.json())
    assert all('name' not in row for row in response.json())
    assert len(reads) <= 12


@pytest.mark.parametrize('mutation', [
    'host_swap', 'bind_swap', 'uuid_swap', 'revoked_placement', 'bad_marker',
    'directory_rebind', 'symlink', 'counterpart', 'root_device_swap',
])
def test_mid_batch_changes_reject_the_whole_snapshot(registry, mutation):
    # Dropping end verification would publish progress against revoked identity.
    permits, entries = queue(registry, 2)
    permit = next(iter(permits._permits.values()))
    root = registry.pools[permit.pool_id].root
    destination = root / 'torrents/.placements' / permit.permit_id
    claim = registry.placement_claims / f'{permit.permit_id}.json'

    def stream():
        yield entries[0]
        if mutation == 'host_swap':
            registry.host_mountinfo.write_text(
                registry.host_mountinfo.read_text().replace('8:1', '8:9'),
            )
        elif mutation == 'bind_swap':
            registry.mountinfo.write_text(registry.mountinfo.read_text().replace('8:1', '8:9'))
        elif mutation == 'uuid_swap':
            link = registry.uuid_dir / permit.filesystem_id
            link.unlink()
            link.symlink_to('../../other-device')
        elif mutation == 'revoked_placement':
            claim.unlink()
        elif mutation == 'bad_marker':
            claim.write_text(json.dumps({'pool_id': 'hdd', 'filesystem_id': 'wrong'}))
        elif mutation == 'directory_rebind':
            destination.rename(destination.with_name(destination.name + '-original'))
            destination.mkdir()
        elif mutation == 'symlink':
            destination.rmdir()
            destination.symlink_to(root / 'media', target_is_directory=True)
        elif mutation == 'counterpart':
            (registry.pools['hdd'].root / 'torrents/.placements' / permit.permit_id).mkdir()
        elif mutation == 'root_device_swap':
            registry._device_id = lambda path: os.makedev(8, 9)
        yield entries[1]

    with pytest.raises(StorageUnavailable):
        permits.queue_entries(stream())


@pytest.mark.parametrize('mutation', ['unknown', 'wrong_path', 'bad_marker', 'revoked', 'symlink'])
def test_invalid_individual_sources_keep_old_fail_closed_result(registry, mutation):
    permits, entries = queue(registry, 2)
    permit = next(iter(permits._permits.values()))
    if mutation == 'unknown':
        entries[0]['hash'] = 'f' * 40
    elif mutation == 'wrong_path':
        entries[0]['save_path'] = '/data/torrents/wrong'
    elif mutation == 'bad_marker':
        (registry.placement_claims / f'{permit.permit_id}.json').write_text('{}')
    elif mutation == 'revoked':
        (registry.placement_claims / f'{permit.permit_id}.json').unlink()
    else:
        path = registry.pools['ssd'].root / 'torrents/.placements' / permit.permit_id
        path.rmdir()
        path.symlink_to(registry.pools['ssd'].root / 'media', target_is_directory=True)
    old = [permits.queue_entry(entry) for entry in entries]
    result = permits.queue_entries(entries)
    assert result == old
    assert result[0]['placement_verified'] is False
    assert result[1]['placement_verified'] is True


def test_next_get_rechecks_revoked_claim_instead_of_reusing_previous_snapshot(registry):
    permits, entries = queue(registry, 2)
    client = client_for(permits, entries)
    headers = {'X-Arr-Token': 'fixture'}
    first = client.get('/internal/queue-capacity', headers=headers)
    assert first.status_code == 200
    assert first.json()[0]['placement_verified'] is True
    permit = next(iter(permits._permits.values()))
    (registry.placement_claims / f'{permit.permit_id}.json').unlink()
    second = client.get('/internal/queue-capacity', headers=headers)
    assert second.status_code == 200
    assert second.json()[0]['placement_verified'] is False


def test_snapshot_is_not_used_by_later_admission_or_mutation(registry):
    permits, entries = queue(registry, 2)
    assert all(row['placement_verified'] for row in permits.queue_entries(entries))
    registry.host_mountinfo.write_text(registry.host_mountinfo.read_text().splitlines()[0])
    with pytest.raises(StorageUnavailable):
        registry.prepare_destination('hdd', 'new-permit')
    assert not (registry.pools['hdd'].root / 'torrents/.placements/new-permit').exists()


def test_request_without_physical_registry_preserves_legacy_contract():
    permits = PermitRegistry()
    entries = [{'hash': 'b' * 40, 'total_size': 500, 'amount_left': 200, 'state': 'stoppedDL'}]
    assert permits.queue_entries(entries) == [
        {'hash': 'b' * 40, 'total_size': 500, 'amount_left': 200,
         'state': 'stoppedDL', 'admitted': False},
    ]


def test_capacity_endpoint_reports_changed_evidence_without_private_details(registry, monkeypatch):
    permits, entries = queue(registry, 2)
    permit = next(iter(permits._permits.values()))
    original = PermitRegistry.queue_entry

    def read(self, entry):
        result = original(self, entry)
        if entry['hash'] == entries[0]['hash']:
            (registry.placement_claims / f'{permit.permit_id}.json').unlink()
        return result

    monkeypatch.setattr(PermitRegistry, 'queue_entry', read)
    response = client_for(permits, entries).get(
        '/internal/queue-capacity', headers={'X-Arr-Token': 'fixture'},
    )
    assert response.status_code == 503
    assert response.json() == {'detail': 'queue placement evidence changed'}


def test_snapshot_cannot_prepare_storage_and_expires_after_the_request(registry):
    with registry.read_snapshot() as snapshot:
        with pytest.raises(StorageUnavailable):
            snapshot.prepare_destination('ssd', 'unauthorized-placement')
        assert snapshot.inspect('ssd').free_bytes == 650
    assert not (registry.pools['ssd'].root / 'torrents/.placements/unauthorized-placement').exists()
    with pytest.raises(StorageUnavailable):
        snapshot.inspect('ssd')


def test_legacy_torrent_directory_rebind_also_rejects_the_snapshot(registry):
    permits, entries = queue(registry, 2)
    permit = next(iter(permits._permits.values()))
    with permits._session() as connection:
        connection.execute('UPDATE gateway_permits SET destination=? WHERE permit_id=?',
                           ('/data/torrents', permit.permit_id))
        connection.commit()
    entries[0]['save_path'] = '/data/torrents'
    torrents = registry.pools['ssd'].root / 'torrents'

    def stream():
        yield entries[0]
        torrents.rename(torrents.with_name('torrents-original'))
        (torrents / '.placements').mkdir(parents=True)
        yield entries[1]

    with pytest.raises(StorageUnavailable):
        permits.queue_entries(stream())


def test_configured_capacity_batches_pool_proof_and_reads_fresh_free_and_ledger(
    registry, monkeypatch,
):
    # Cached sample/free or ledger outside the queue read would undercount new commitments.
    from homeserver_control.gateway import app as module

    permits, entries = queue(registry)
    capacity = registry.placement_claims.parent / 'capacity.json'
    capacity.write_text(json.dumps({'pools': [
        {'pool_id': pool.pool_id, 'filesystem_id': pool.filesystem_id,
         'state': 'ready', 'measured_at': time.time()}
        for pool in registry.pools.values()
    ]}))
    monkeypatch.setenv('HOMESERVER_CAPACITY_SNAPSHOT', str(capacity))
    monkeypatch.setattr(module, '_production_permits', permits)

    class Upstream:
        def read(self, path, params=None):
            assert path == '/api/v2/torrents/info' and params is None
            return entries

    monkeypatch.setattr(module, '_production_upstream', Upstream())
    reads = []
    read_text = Path.read_text
    queue_entry = PermitRegistry.queue_entry

    def text(path, *args, **kwargs):
        if path in {registry.host_mountinfo, registry.mountinfo}:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    def read(self, entry):
        result = queue_entry(self, entry)
        if entry['hash'] == entries[-1]['hash']:
            registry._usage = lambda path: SimpleNamespace(total=1000, used=950, free=50)
        return result

    monkeypatch.setattr(Path, 'read_text', text)
    monkeypatch.setattr(PermitRegistry, 'queue_entry', read)
    evidence = module._configured_capacity()
    assert evidence.free_bytes == 50
    assert sum(evidence.remaining_by_hash.values()) == 500
    assert len(reads) <= 16
    before = permits.pending_bytes(evidence)
    assert before == 250
    with permits._session() as connection:
        connection.execute('INSERT INTO audited_derivations '
                           '(file_id,permit_id,reservation_id,pool_id,budget_bytes,binding_json) '
                           'VALUES (?,?,?,?,?,?)', ('fixture-file', 'fixture-permit',
                                                   'fixture-reservation', 'ssd', 200, '{}'))
        connection.commit()
    assert permits.pending_bytes(evidence) == 450
