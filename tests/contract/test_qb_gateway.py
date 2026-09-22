import shutil
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry


class RecordingQbitClient:
    def __init__(self) -> None:
        self.added: list[dict] = []

    def add_torrent(self, payload: dict) -> dict:
        self.added.append(payload)
        return {"accepted": True, "infohash": payload["infohash"]}


def _client() -> tuple[TestClient, PermitRegistry, RecordingQbitClient]:
    permits = PermitRegistry()
    upstream = RecordingQbitClient()
    app = create_app(permits=permits, upstream=upstream, arr_token="arr-token")
    return TestClient(app), permits, upstream


def test_mutation_without_permit_is_denied_and_not_forwarded() -> None:
    client, _, upstream = _client()
    response = client.post(
        "/api/v2/torrents/add",
        headers={"X-Arr-Token": "arr-token"},
        json={"infohash": "a" * 40, "savepath": "/data/torrents"},
    )
    assert response.status_code == 403
    assert upstream.added == []


def test_multipart_mutation_without_permit_is_denied_and_not_forwarded() -> None:
    client, _, upstream = _client()
    response = client.post(
        "/api/v2/torrents/add",
        headers={"X-Arr-Token": "arr-token"},
        data={"savepath": "/data/torrents"},
        files={"torrents": ("fixture.torrent", b"fixture", "application/x-bittorrent")},
    )
    assert response.status_code == 403
    assert upstream.added == []


def test_authorized_multipart_mutation_forwards_verified_metadata_once() -> None:
    client, permits, upstream = _client()
    permit = permits.issue(
        infohash="f" * 40,
        destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256=sha256(b"fixture").hexdigest(),
    )
    response = client.post(
        "/api/v2/torrents/add",
        headers={
            "X-Arr-Token": "arr-token",
            "X-Admission-Permit": permit.token,
            "X-Infohash": "f" * 40,
        },
        data={"savepath": "/data/torrents"},
        files={"torrents": ("fixture.torrent", b"fixture", "application/x-bittorrent")},
    )
    assert response.status_code == 200
    assert upstream.added[0]["torrent_bytes"] == b"fixture"


def test_authorized_multipart_mutation_rejects_metadata_mismatch() -> None:
    client, permits, upstream = _client()
    permit = permits.issue(
        infohash="f" * 40,
        destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256=sha256(b"expected").hexdigest(),
    )
    response = client.post(
        "/api/v2/torrents/add",
        headers={
            "X-Arr-Token": "arr-token",
            "X-Admission-Permit": permit.token,
            "X-Infohash": "f" * 40,
        },
        data={"savepath": "/data/torrents"},
        files={"torrents": ("fixture.torrent", b"actual", "application/x-bittorrent")},
    )
    assert response.status_code == 409
    assert upstream.added == []


def test_authorized_multipart_mutation_requires_metadata_digest() -> None:
    client, permits, upstream = _client()
    permit = permits.issue(
        infohash="f" * 40,
        destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    response = client.post(
        "/api/v2/torrents/add",
        headers={
            "X-Arr-Token": "arr-token",
            "X-Admission-Permit": permit.token,
            "X-Infohash": "f" * 40,
        },
        data={"savepath": "/data/torrents"},
        files={"torrents": ("fixture.torrent", b"fixture", "application/x-bittorrent")},
    )
    assert response.status_code == 409
    assert upstream.added == []


def test_authorized_mutation_is_forwarded_once() -> None:
    client, permits, upstream = _client()
    permit = permits.issue(
        infohash="b" * 40,
        destination="/data/torrents/job-1",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    headers = {"X-Arr-Token": "arr-token", "X-Admission-Permit": permit.token}
    payload = {"infohash": "b" * 40, "savepath": "/data/torrents/job-1"}
    first = client.post("/api/v2/torrents/add", headers=headers, json=payload)
    second = client.post("/api/v2/torrents/add", headers=headers, json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert len(upstream.added) == 1


def test_permit_cannot_change_infohash_or_destination() -> None:
    client, permits, upstream = _client()
    permit = permits.issue(
        infohash="c" * 40,
        destination="/data/torrents/job-2",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    response = client.post(
        "/api/v2/torrents/add",
        headers={"X-Arr-Token": "arr-token", "X-Admission-Permit": permit.token},
        json={"infohash": "d" * 40, "savepath": "/data/torrents/job-2"},
    )
    assert response.status_code == 409
    assert upstream.added == []


def test_unknown_upstream_path_is_not_a_proxy() -> None:
    client, _, upstream = _client()
    response = client.post(
        "/api/v2/torrents/delete",
        headers={"X-Arr-Token": "arr-token"},
        json={"hashes": "e" * 40},
    )
    assert response.status_code == 404
    assert upstream.added == []


def test_recovery_marker_blocks_gateway_mutation() -> None:
    root = Path(".runtime") / f"gateway-recovery-{uuid4().hex}"
    root.mkdir(parents=True)
    marker = root / "RECOVERY_MODE"
    marker.write_text("admission_enabled=false\n", encoding="utf-8")
    permits = PermitRegistry()
    upstream = RecordingQbitClient()
    app = create_app(
        permits=permits,
        upstream=upstream,
        arr_token="arr-token",
        recovery_mode_path=marker,
    )
    permit = permits.issue(
        infohash="a" * 40,
        destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    try:
        response = TestClient(app).post(
            "/api/v2/torrents/add",
            headers={"X-Arr-Token": "arr-token", "X-Admission-Permit": permit.token},
            json={"infohash": "a" * 40, "savepath": "/data/torrents"},
        )
        assert response.status_code == 503
        assert upstream.added == []
    finally:
        shutil.rmtree(root, ignore_errors=True)
