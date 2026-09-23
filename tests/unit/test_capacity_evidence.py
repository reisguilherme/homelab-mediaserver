import json
import time
from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.worker.capacity_evidence import read_capacity_evidence


@pytest.mark.asyncio
async def test_capacity_reads_current_free_and_all_queue_bytes(tmp_path, monkeypatch) -> None:
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(json.dumps({"filesystem_id": "media-uuid",
                                    "measured_at": time.time()}), encoding="utf-8")
    monkeypatch.setattr("os.path.ismount", lambda path: True)
    monkeypatch.setattr("os.statvfs", lambda path: SimpleNamespace(
        f_bavail=3_000_000, f_frsize=1000))
    def handler(request):
        assert request.headers["X-Arr-Token"] == "private"
        assert request.url.path == "/internal/queue-capacity"
        return httpx.Response(200, json=[
            {"hash": "a" * 40, "total_size": 2_000_000_000,
             "amount_left": 1_000_000_000, "admitted": True},
            {"hash": "b" * 40, "total_size": 0,
             "amount_left": 0, "admitted": True},
            {"hash": "c" * 40, "total_size": 1_000_000_000,
             "amount_left": 400_000_000, "admitted": False},
        ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await read_capacity_evidence(
            snapshot_path=snapshot, data_root=tmp_path,
            gateway_url="http://gateway:8081", arr_token="private", client=client)
    assert evidence.free_bytes == 3_000_000_000
    assert evidence.remaining_by_hash == {"a" * 40: 1_000_000_000}
    assert evidence.other_pending_bytes == 400_000_000


@pytest.mark.asyncio
async def test_capacity_fails_closed_for_unmanaged_unknown_magnet(tmp_path, monkeypatch) -> None:
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(json.dumps({"filesystem_id": "media-uuid",
                                    "measured_at": time.time()}), encoding="utf-8")
    monkeypatch.setattr("os.path.ismount", lambda path: True)
    monkeypatch.setattr("os.statvfs", lambda path: SimpleNamespace(f_bavail=10, f_frsize=1000))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json=[{"hash": "a" * 40, "total_size": 0,
                    "amount_left": 0, "admitted": False}]
    ))) as client:
        with pytest.raises(ValueError, match="unmanaged torrent size is unknown"):
            await read_capacity_evidence(
                snapshot_path=snapshot, data_root=tmp_path,
                gateway_url="http://gateway:8081", arr_token="private", client=client)


@pytest.mark.asyncio
async def test_free_bytes_are_measured_after_queue_progress(tmp_path, monkeypatch) -> None:
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(json.dumps({"filesystem_id": "media-uuid",
                                    "measured_at": time.time()}), encoding="utf-8")
    monkeypatch.setattr("os.path.ismount", lambda path: True)
    progress_read = False

    def statvfs(path):
        return SimpleNamespace(f_bavail=2000 if progress_read else 3000, f_frsize=1000)

    def handler(request):
        nonlocal progress_read
        progress_read = True
        return httpx.Response(200, json=[])

    monkeypatch.setattr("os.statvfs", statvfs)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        evidence = await read_capacity_evidence(
            snapshot_path=snapshot, data_root=tmp_path,
            gateway_url="http://gateway:8081", arr_token="private", client=client)
    assert evidence.free_bytes == 2_000_000


@pytest.mark.asyncio
async def test_capacity_excludes_known_stopped_torrents_but_counts_active_and_unknown_state(
    tmp_path, monkeypatch,
) -> None:
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(json.dumps({"filesystem_id": "media-uuid",
                                    "measured_at": time.time()}), encoding="utf-8")
    monkeypatch.setattr("os.path.ismount", lambda path: True)
    monkeypatch.setattr("os.statvfs", lambda path: SimpleNamespace(
        f_bavail=10_000_000, f_frsize=1000))
    queue = [
        {"hash": "a" * 40, "total_size": 3000, "amount_left": 2000,
         "admitted": True, "state": "stoppedDL"},
        {"hash": "b" * 40, "total_size": 3000, "amount_left": 1500,
         "admitted": True, "state": "downloading"},
        {"hash": "c" * 40, "total_size": 3000, "amount_left": 2500,
         "admitted": False, "state": "pausedDL"},
        {"hash": "d" * 40, "total_size": 3000, "amount_left": 500,
         "admitted": False, "state": "downloading"},
        {"hash": "e" * 40, "total_size": 3000, "amount_left": 400,
         "admitted": False},
        {"hash": "f" * 40, "total_size": 0, "amount_left": 0,
         "admitted": True, "state": "stoppedDL"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=queue)
    )) as client:
        evidence = await read_capacity_evidence(
            snapshot_path=snapshot, data_root=tmp_path,
            gateway_url="http://gateway:8081", arr_token="private", client=client)
    assert evidence.remaining_by_hash == {"a" * 40: 2000, "b" * 40: 1500}
    assert evidence.paused_hashes == frozenset({"a" * 40, "f" * 40})
    assert evidence.other_pending_bytes == 900
