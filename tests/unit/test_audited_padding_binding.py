from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import Permit
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.audited_imports import AuditedManifestImporter
from homeserver_control.worker.validation import ValidationError


def encode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(encode(item) for item in value) + b'e'
    return b'd' + b''.join(encode(key) + encode(item)
                          for key, item in sorted(value.items())) + b'e'


@pytest.mark.asyncio
async def test_hybrid_reserves_padding_but_qbit_and_disk_need_only_content_files(tmp_path):
    raw = encode({b'info': {
        b'file tree': {
            b'a.mkv': {b'': {b'length': 101, b'pieces root': b'a' * 32}},
            b'b.mkv': {b'': {b'length': 222, b'pieces root': b'b' * 32}},
        }, b'files': [
            {b'length': 101, b'path': [b'a.mkv']},
            {b'attr': b'p', b'length': 16283, b'path': [b'.pad', b'16283']},
            {b'length': 222, b'path': [b'b.mkv']},
            {b'attr': b'p', b'length': 16162, b'path': [b'.pad', b'16162']},
        ], b'meta version': 2, b'name': b'pack', b'piece length': 16384,
        b'pieces': b'c' * 40,
    }})
    metadata = inspect_torrent(raw)
    permit = Permit('hybrid', 'fixture-token', metadata.infohash, '/data/torrents',
        datetime.now(UTC) + timedelta(hours=1), category='sonarr', reservation_id='hybrid-reserve',
        metadata_sha256=metadata.metadata_sha256,
        selected_files=tuple(file.path for file in metadata.files),
        budget_bytes=32768, state='confirmed')
    artifacts = TorrentArtifactStore(tmp_path / 'control.sqlite')
    artifacts.put(permit, raw)
    job = {'permit_id': permit.permit_id, 'reservation_id': permit.reservation_id,
        'metadata_sha256': metadata.metadata_sha256, 'category': 'sonarr',
        'expected_payload_bytes': 32768, 'padding_bytes': 32445,
        'payload_files': [{'path': file.path, 'size_bytes': file.length}
                          for file in metadata.files]}
    root = tmp_path / 'torrents'
    (root / 'pack').mkdir(parents=True)
    for file in metadata.files:
        (root / file.path).write_bytes(b'v' * file.length)

    def handler(request):
        if request.url.path.endswith('/info'):
            return httpx.Response(200, json=[{'hash': permit.infohash, 'category': 'sonarr',
                'save_path': '/data/torrents', 'content_path': '/data/torrents/pack',
                'progress': 1, 'amount_left': 0}])
        return httpx.Response(200, json=[{'name': file.path, 'size': file.length, 'progress': 1}
                                         for file in metadata.files])

    helper = SimpleNamespace(torrent_root=root,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        _source_path=lambda _permit, path: root / path,
        _content_path=lambda _permit, _raw: root / 'pack',
        _physical=lambda path, _permit: path)
    permits = SimpleNamespace(get_for_reservation=lambda *_a, **_kw: permit,
        probe_reservation_active=lambda _p: True, placement_valid=lambda _p, **_kw: True)
    worker = AuditedManifestImporter(store=None, manifest_loader=lambda: {}, permits=permits,
        torrent_store=artifacts, helpers={}, gateway_url='http://gateway', arr_token='fixture')
    assert worker._permit(job) is permit
    await worker._download(helper, permit, job)
    assert not (root / 'pack/.pad').exists()
    job['padding_bytes'] = 0
    with pytest.raises(ValidationError):
        worker._permit(job)
