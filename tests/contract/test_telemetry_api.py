from datetime import UTC, datetime

from fastapi.testclient import TestClient

from homeserver_telemetry.app import create_app


def _snapshot() -> dict:
    return {
        "schema_version": 1,
        "sequence": 4,
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": {},
        "host": {
            "cpu_percent": None,
            "ram_percent": None,
            "cpu_celsius": None,
            "uptime_seconds": None,
        },
        "capacity": {
            "total_bytes": 0,
            "free_bytes": 0,
            "reserved_unallocated_bytes": 0,
            "admissible_bytes": 0,
        },
        "transfers": {"download_bps": 0, "upload_bps": 0, "active_count": 0, "queue_count": 0},
        "downloads": [],
        "playback": {"active_count": None, "remote_count": None, "items": []},
        "alerts": [],
    }


def test_telemetry_endpoint_returns_versioned_snapshot() -> None:
    client = TestClient(create_app(snapshot_provider=lambda: _snapshot()))
    response = client.get("/api/v1/telemetry")
    assert response.status_code == 200
    assert response.json()["schema_version"] == 1


def test_telemetry_endpoint_returns_503_when_provider_fails() -> None:
    def failed_provider() -> dict:
        raise RuntimeError("controller unavailable")

    client = TestClient(create_app(snapshot_provider=failed_provider))
    response = client.get("/api/v1/telemetry")
    assert response.status_code == 503


def test_status_dashboard_serves_read_only_page_and_live_snapshot() -> None:
    status = {
        "host": {"state": "ok", "cpu_percent": 42.0, "ram_percent": 68.0},
        "network": {"interface": "enxusb", "rx_bps": 1200, "tx_bps": 300},
        "capacity": {"total_bytes": 10000, "used_bytes": 6000, "free_bytes": 4000},
        "storage": {"movies_bytes": 2000, "series_bytes": 1000, "torrents_bytes": 500},
    }
    client = TestClient(
        create_app(snapshot_provider=lambda: _snapshot(), status_provider=lambda: status)
    )
    page = client.get("/ui/status")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "/api/v1/status" in page.text
    assert "Tráfego da interface da rota padrão" in page.text
    assert "password" not in page.text.lower()
    assert page.headers["cache-control"] == "no-store"

    response = client.get("/api/v1/status")
    assert response.status_code == 200
    assert response.json()["network"]["rx_bps"] == 1200
    assert response.headers["cache-control"] == "no-store"
