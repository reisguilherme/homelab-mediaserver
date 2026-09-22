import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from homeserver_control.api.app import ControlState, create_app


@pytest.fixture
def client() -> TestClient:
    root = Path(".runtime") / f"control-api-{uuid4().hex}"
    root.mkdir(parents=True)
    media = root / "media"
    media.mkdir()
    item = media / "movie.mkv"
    item.write_bytes(b"payload")
    state = ControlState(
        media_roots=(media,),
        media_catalog={"movie:tmdb:1": (item,)},
        admin_token="admin-token",
        csrf_token="csrf-token",
        collector_token="collector-token",
        db_path=root / "control.sqlite",
        capacity_provider=lambda: {
            "filesystem_id": "uuid-fixture",
            "total_bytes": 100_000_000_000,
            "free_bytes": 70_000_000_000,
            "reserved_unallocated_bytes": 0,
            "admissible_bytes": 49_000_000_000,
            "measured_at": datetime.now(UTC).timestamp(),
        },
    )
    test_client = TestClient(create_app(state=state))
    try:
        yield test_client
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": "admin-token", "X-CSRF-Token": "csrf-token"}


def test_live_ready_queue_and_capacity_are_separate_contracts(client: TestClient) -> None:
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 200
    assert client.get("/api/v1/queue", headers=_admin_headers()).json()["items"] == []
    capacity = client.get("/api/v1/capacity", headers=_admin_headers())
    assert capacity.status_code == 200
    assert capacity.json()["filesystem_id"] == "uuid-fixture"
    assert client.get("/api/v1/queue").status_code == 401


def test_deletion_requires_csrf_and_confirmation_is_idempotent(client: TestClient) -> None:
    root = Path(".runtime")
    media = next(root.glob("control-api-*/media"))
    item = media / "movie.mkv"
    preview_response = client.post(
        "/api/v1/deletions/preview",
        headers=_admin_headers(),
        json={"media_key": "movie:tmdb:1", "paths": [str(item)]},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["bytes_estimated"] == 7
    assert (
        client.post(
            "/api/v1/deletions/preview",
            headers={"X-Admin-Token": "admin-token"},
            json={"media_key": "movie:tmdb:1", "paths": [str(item)]},
        ).status_code
        == 403
    )

    payload = {
        "token": preview["token"],
        "version": preview["version"],
        "operation_id": "delete-operation-1",
    }
    confirmed = client.post("/api/v1/deletions", headers=_admin_headers(), json=payload)
    assert confirmed.status_code == 202
    assert confirmed.json()["operation_id"] == "delete-operation-1"
    repeated = client.post("/api/v1/deletions", headers=_admin_headers(), json=payload)
    assert repeated.status_code == 202
    operation = client.get(
        "/api/v1/operations/delete-operation-1", headers=_admin_headers()
    ).json()
    assert operation["state"] == "authorized"


def test_internal_collector_routes_do_not_accept_admin_token(client: TestClient) -> None:
    assert (
        client.get("/internal/v1/events", headers={"X-Collector-Token": "admin-token"}).status_code
        == 401
    )
    response = client.post(
        "/internal/v1/seed-limit",
        headers={"X-Collector-Token": "collector-token"},
        json={"bytes_per_second": 625_000, "reason": "remote_playback"},
    )
    assert response.status_code == 200
    assert response.json()["bytes_per_second"] == 625_000


def test_recovery_mode_exposes_blocked_admission_at_top_level() -> None:
    root = Path('.runtime') / f'recovery-{uuid4().hex}'
    root.mkdir(parents=True)
    media = root / 'media'
    media.mkdir()
    state = ControlState(
        media_roots=(media,),
        admin_token='admin-token',
        csrf_token='csrf-token',
        collector_token='collector-token',
        admission_enabled=False,
    )
    try:
        response = TestClient(create_app(state=state)).get('/health/ready')
        assert response.status_code == 503
        assert response.json()['admission_enabled'] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_recovery_marker_blocks_readiness_on_process_start() -> None:
    root = Path(".runtime") / f"recovery-marker-{uuid4().hex}"
    root.mkdir(parents=True)
    media = root / "media"
    media.mkdir()
    marker = root / "RECOVERY_MODE"
    marker.write_text("admission_enabled=false\nrecovery_snapshot=test\n", encoding="utf-8")
    state = ControlState(
        media_roots=(media,),
        admin_token="admin-token",
        csrf_token="csrf-token",
        collector_token="collector-token",
        db_path=root / "control.sqlite",
        recovery_mode_path=marker,
        capacity_provider=lambda: {
            "filesystem_id": "uuid-fixture",
            "total_bytes": 100,
            "free_bytes": 50,
            "measured_at": datetime.now(UTC).timestamp(),
        },
    )
    try:
        client = TestClient(create_app(state=state))
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["admission_enabled"] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_recovery_marker_added_after_start_blocks_readiness_and_mutations() -> None:
    root = Path(".runtime") / f"recovery-live-{uuid4().hex}"
    root.mkdir(parents=True)
    media = root / "media"
    media.mkdir()
    item = media / "movie.mkv"
    item.write_bytes(b"fixture")
    marker = root / "RECOVERY_MODE"
    state = ControlState(
        media_roots=(media,),
        media_catalog={"movie:tmdb:1": (item,)},
        admin_token="admin-token",
        csrf_token="csrf-token",
        collector_token="collector-token",
        db_path=root / "control.sqlite",
        recovery_mode_path=marker,
        capacity_provider=lambda: {
            "filesystem_id": "uuid-fixture",
            "total_bytes": 100,
            "free_bytes": 50,
            "measured_at": datetime.now(UTC).timestamp(),
        },
    )
    try:
        client = TestClient(create_app(state=state))
        assert client.get("/health/ready").status_code == 200
        marker.write_text("admission_enabled=false\n", encoding="utf-8")
        blocked = client.get("/health/ready")
        assert blocked.status_code == 503
        assert blocked.json()["admission_enabled"] is False
        preview = client.post(
            "/api/v1/deletions/preview",
            headers=_admin_headers(),
            json={"media_key": "movie:tmdb:1"},
        )
        assert preview.status_code == 503
        seed_limit = client.post(
            "/internal/v1/seed-limit",
            headers={"X-Collector-Token": "collector-token"},
            json={"bytes_per_second": 625_000, "reason": "remote_playback"},
        )
        assert seed_limit.status_code == 503
        assert item.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_readiness_requires_persistent_state_and_valid_capacity() -> None:
    root = Path('.runtime') / f'readiness-{uuid4().hex}'
    root.mkdir(parents=True)
    media = root / 'media'
    media.mkdir()
    try:
        state = ControlState(
            media_roots=(media,),
            admin_token='admin-token',
            collector_token='collector-token',
            capacity_provider=lambda: {
                'filesystem_id': None,
                'total_bytes': 0,
                'free_bytes': 0,
                'measured_at': None,
            },
        )
        response = TestClient(create_app(state=state)).get('/health/ready')
        assert response.status_code == 503
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_capacity_provider_reads_snapshot_and_rejects_stale_data(tmp_path: Path) -> None:
    from homeserver_control.api.app import _capacity_from_snapshot

    path = tmp_path / "capacity.json"
    snapshot = {
        "filesystem_id": "uuid-fixture",
        "total_bytes": 1000,
        "free_bytes": 500,
        "measured_at": datetime.now(UTC).timestamp(),
    }
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert _capacity_from_snapshot(path)["filesystem_id"] == "uuid-fixture"

    snapshot["measured_at"] -= 90
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert _capacity_from_snapshot(path)["filesystem_id"] is None

    snapshot["measured_at"] = datetime.now(UTC).timestamp() + 90
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert _capacity_from_snapshot(path)["filesystem_id"] is None


def test_default_app_readiness_uses_capacity_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "media"
    media.mkdir()
    snapshot = tmp_path / "capacity.json"
    snapshot.write_text(
        json.dumps(
            {
                "filesystem_id": "uuid-fixture",
                "total_bytes": 1000,
                "free_bytes": 500,
                "measured_at": datetime.now(UTC).timestamp(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOMESERVER_MEDIA_ROOTS", str(media))
    monkeypatch.setenv("HOMESERVER_DB_PATH", str(tmp_path / "control.sqlite"))
    monkeypatch.setenv("HOMESERVER_ADMIN_TOKEN", "admin-token")
    monkeypatch.setenv("HOMESERVER_COLLECTOR_TOKEN", "collector-token")
    monkeypatch.setenv("HOMESERVER_CAPACITY_SNAPSHOT", str(snapshot))
    monkeypatch.setenv("HOMESERVER_RECOVERY_MODE", str(tmp_path / "RECOVERY_MODE"))
    client = TestClient(create_app())
    assert client.get("/health/ready").status_code == 200
    snapshot.unlink()
    assert client.get("/health/ready").status_code == 503
