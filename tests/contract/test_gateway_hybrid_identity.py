"""Real SQLite ownership and gateway guards remain canonical across native aliases."""

import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from homeserver_control.adapters.http import EffectUncertain
from homeserver_control.adapters.qbittorrent import QBittorrentAdapter
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(map(encode, value)) + b"e"
    return b"d" + b"".join(encode(k) + encode(v) for k, v in sorted(value.items())) + b"e"


@pytest.fixture
def source(tmp_path, request):
    category = getattr(request, "param", "radarr")
    info = {
        b"file tree": {b"video.mkv": {b"": {b"length": 10, b"pieces root": b"a" * 32}}},
        b"files": [{b"length": 10, b"path": [b"video.mkv"]}],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
        b"pieces": b"a" * 20,
    }
    torrent = encode({b"info": info})
    parsed = inspect_torrent(torrent)
    native = sha256(encode(info)).hexdigest()
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    reservation = repository.reserve(
        request_id="fixture",
        source_id="fixture",
        media_key="movie:tmdb:1" if category == "radarr" else "season:tmdb:1:1",
        filesystem_id="fixture",
        budget_bytes=10,
        free_bytes=10000,
        total_bytes=20000,
    )
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash=parsed.infohash,
        metadata_sha256=parsed.metadata_sha256,
        destination="/data/torrents",
        category=category,
        scope_key="S01E01" if category == "sonarr" else None,
        budget_bytes=10,
        selected_files=("Show/video.mkv",),
        reservation_id=reservation.reservation_id,
        reported_seeders=1,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    store = TorrentArtifactStore(database)
    store.put(permit, torrent)

    def uncertain(_):
        raise EffectUncertain("fixture prior acknowledgement lost")

    with pytest.raises(EffectUncertain):
        permits.authorize(
            token=permit.token,
            infohash=permit.infohash,
            destination=permit.destination,
            metadata_sha256=permit.metadata_sha256,
            effect=uncertain,
        )
    assert permits.get(permit.token).state == "unknown"
    calls, protected = [], []
    row = {
        "hash": native[:40],
        "infohash_v1": parsed.infohash,
        "infohash_v2": native,
        "category": category,
        "save_path": "/data/torrents",
        "total_size": 10,
        "amount_left": 5,
        "state": "stoppedDL",
        "progress": 0.5,
    }
    deleted = False
    protection = {"enabled": False}

    def handler(request):
        nonlocal deleted
        calls.append(request)
        if request.url.path == "/api/v2/torrents/info":
            assert request.method == "GET"
            assert request.url.params.get("hashes") in (None, native[:40])
            return httpx.Response(200, json=[] if deleted else [row])
        if request.url.path == "/api/v2/torrents/files":
            assert request.method == "GET" and request.url.params["hash"] == native[:40]
            return httpx.Response(200, json=[{"name": "Show/video.mkv", "size": 10, "priority": 1}])
        assert request.url.path == "/api/v2/torrents/delete" and request.method == "POST"
        assert parse_qs(request.content.decode()) == {
            "hashes": [native[:40]],
            "deleteFiles": ["true"],
        }
        deleted = True
        return httpx.Response(200, text="Ok.")

    upstream = QBittorrentAdapter(
        base_url="http://fixture",
        username="fixture",
        password="fixture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        metadata_provider=store.verified_for_infohash,
    )
    upstream._logged_in = True

    def is_protected(value):
        protected.append(value)
        return protection["enabled"]

    with TestClient(
        create_app(
            permits=permits,
            upstream=upstream,
            torrent_store=store,
            arr_token="fixture-token",
            protected_source_provider=is_protected,
        ),
        raise_server_exceptions=False,
    ) as client:
        yield client, permits, permit, calls, protected, protection, database


def reconcile(source):
    client, _, permit, *_ = source
    return client.post(
        "/internal/reconcile-source",
        headers={"X-Arr-Token": "fixture-token"},
        json={"permit_token": permit.token},
    )


def test_unknown_hybrid_reconciles_existing_source_without_another_add(source):
    client, permits, permit, calls, *_ = source
    assert reconcile(source).json() == {"state": "confirmed"}
    assert permits.get(permit.token).state == "confirmed"
    assert reconcile(source).json() == {"state": "confirmed"}
    info = client.get(
        "/api/v2/torrents/info",
        params={"hashes": permit.infohash, "category": "radarr"},
        headers={"X-Arr-Token": "fixture-token"},
    )
    assert info.status_code == 200 and info.json()[0]["hash"] == permit.infohash
    files = client.get(
        "/api/v2/torrents/files",
        params={"hash": permit.infohash},
        headers={"X-Arr-Token": "fixture-token"},
    )
    assert files.status_code == 200 and files.json()[0]["name"] == "Show/video.mkv"
    queue = client.get("/internal/queue-capacity", headers={"X-Arr-Token": "fixture-token"})
    assert queue.status_code == 200 and queue.json() == [
        {
            "hash": permit.infohash,
            "total_size": 10,
            "amount_left": 5,
            "state": "stoppedDL",
            "admitted": True,
        }
    ]
    assert all(call.method == "GET" for call in calls)


@pytest.mark.parametrize("source", ["sonarr"], indirect=True)
def test_protected_hybrid_action_rechecks_canonical_v1_and_never_posts(source):
    client, _, permit, calls, protected, protection, _ = source
    assert reconcile(source).status_code == 200
    calls.clear()
    protection["enabled"] = True
    response = client.post(
        "/internal/series-queue-state",
        headers={"X-Arr-Token": "fixture-token"},
        json={"permit_token": permit.token, "action": "stop"},
    )
    assert response.status_code == 403
    assert protected and set(protected) == {permit.infohash}
    assert all(call.method == "GET" for call in calls)


def test_exact_hybrid_delete_uses_native_alias_once(source):
    client, _, permit, calls, protected, _, _ = source
    assert reconcile(source).status_code == 200
    response = client.post(
        "/internal/delete-source",
        headers={"X-Arr-Token": "fixture-token"},
        json={"permit_token": permit.token, "media_key": "movie:tmdb:1", "scope_key": None},
    )
    assert response.status_code == 200 and response.json() == {"state": "deleted"}
    assert [call.url.path for call in calls if call.method == "POST"] == ["/api/v2/torrents/delete"]
    # Explicit deletion keeps its existing policy; automated mutation protection
    # is independently covered by the series queue state fixture above.
    assert protected == []


def test_metadata_identity_never_authorizes_failed_source(source):
    client, _, permit, calls, _, _, database = source
    assert reconcile(source).status_code == 200
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE gateway_permits SET state='failed'")
    calls.clear()
    assert reconcile(source).status_code == 403
    assert calls == []
    queue = client.get("/internal/queue-capacity", headers={"X-Arr-Token": "fixture-token"})
    assert queue.status_code == 200
    assert queue.json()[0]["hash"] == permit.infohash and queue.json()[0]["admitted"] is False
