"""Real gateway/permit transitions with hybrid files and virtual padding."""
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import CapacityEvidence


def encode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(encode(v) for v in value) + b'e'
    return b'd' + b''.join(encode(k) + encode(v) for k, v in sorted(value.items())) + b'e'


def descriptor():
    return encode({b'info': {b'file tree': {
        b'S01E01.mkv': {b'': {b'length': 101, b'pieces root': b'a' * 32}},
        b'S02E01.mkv': {b'': {b'length': 222, b'pieces root': b'b' * 32}},
    }, b'files': [
        {b'length': 101, b'path': [b'S01E01.mkv']},
        {b'attr': b'p', b'length': 16283, b'path': [b'.pad', b'16283']},
        {b'length': 222, b'path': [b'S02E01.mkv']},
        {b'attr': b'p', b'length': 16162, b'path': [b'.pad', b'16162']},
    ], b'meta version': 2, b'name': b'pack', b'piece length': 16384,
        b'pieces': b'c' * 40}})


class ContentOnlyQbit:
    """qBit's real API omits pad_file_at entries from public file indexes."""

    def __init__(self):
        self.effects = 0
        self.received = None
        self.manifest = None

    def add_torrent(self, payload):
        self.manifest = inspect_torrent(payload['torrent_bytes'])
        self.received = payload
        self.effects += 1
        return {'accepted': True, 'infohash': payload['infohash']}

    def read(self, path, params=None):
        if path == '/api/v2/torrents/files':
            return [{'name': f.path, 'size': f.length, 'progress': 1}
                    for f in self.manifest.files]
        if path == '/api/v2/torrents/info':
            return [{'hash':self.received['infohash'], 'category':'sonarr',
                     'save_path':'/data/torrents', 'amount_left':0, 'total_size':323,
                     'progress':1, 'state':'uploading'}]
        raise AssertionError('unexpected upstream call')


def test_hybrid_whole_budget_and_content_only_files_dispatch_exactly_once(tmp_path):
    torrent = descriptor()
    parsed = inspect_torrent(torrent)
    database = tmp_path / 'control.sqlite'
    repository = ReservationRepository(database)
    repository.initialize()
    reservation = repository.reserve(
        request_id='operator:fixture:collection', source_id='fixture',
        media_key='collection:fixture:series', filesystem_id='fixture-uuid',
        budget_bytes=0, free_bytes=100000, total_bytes=200000,
    )
    assert reservation.accepted
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash=parsed.infohash, metadata_sha256=parsed.metadata_sha256,
        destination='/data/torrents', category='sonarr',
        reservation_id=reservation.reservation_id,
        selected_files=tuple(f.path for f in parsed.files),
        budget_bytes=32768, reported_seeders=1, capacity=CapacityEvidence(100000, {}),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    upstream = ContentOnlyQbit()
    app = create_app(permits=permits,upstream=upstream,arr_token='fixture-token',
                     capacity_provider=lambda: CapacityEvidence(100000, {}))
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post('/api/v2/torrents/add',
                headers={'X-Arr-Token':'fixture-token','X-Admission-Permit':permit.token},
                data={'category':'sonarr','savepath':'/data/torrents','stopped':'false'},
                files={'torrents':('fixture.torrent',torrent,'application/x-bittorrent')})
            assert response.status_code == 200
        files = client.get('/api/v2/torrents/files', params={'hash':parsed.infohash},
                           headers={'X-Arr-Token':'fixture-token'})
        assert files.status_code == 200
        assert files.json() == [
            {'name':'pack/S01E01.mkv','size':101,'progress':1},
            {'name':'pack/S02E01.mkv','size':222,'progress':1},
        ]
    confirmed = permits.get(permit.token)
    assert confirmed.state == 'confirmed'
    assert confirmed.budget_bytes == 32768
    assert confirmed.selected_files == ('pack/S01E01.mkv','pack/S02E01.mkv')
    assert upstream.effects == 1
    assert repository.active_reservation(reservation.reservation_id)['budget_bytes'] == 32768
    assert permits.pending_bytes(CapacityEvidence(100000, {parsed.infohash: 323})) == 323
