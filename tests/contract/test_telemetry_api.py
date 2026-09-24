from datetime import UTC, datetime
from html.parser import HTMLParser

from fastapi.testclient import TestClient

from homeserver_telemetry.app import create_app


class _ServiceCards(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and (card_id := attributes.get("id")):
            self.attributes[card_id] = attributes


def _service_cards(page: str) -> dict[str, dict[str, str | None]]:
    parser = _ServiceCards()
    parser.feed(page)
    return parser.attributes


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


def test_status_dashboard_links_native_services_through_tailscale(monkeypatch) -> None:
    monkeypatch.setenv("HOMESERVER_TAILSCALE_HOSTNAME", "server.example.ts.net")
    client = TestClient(create_app(snapshot_provider=lambda: _snapshot()))

    page = client.get("/ui/status")
    cards = _service_cards(page.text)

    assert page.status_code == 200
    assert {
        name: cards[name]["href"]
        for name in (
            "service-jellyfin",
            "service-seerr",
            "service-sonarr",
            "service-radarr",
            "service-qbittorrent",
        )
    } == {
        "service-jellyfin": "http://server.example.ts.net:8096/",
        "service-seerr": "http://server.example.ts.net:5055/",
        "service-sonarr": "http://server.example.ts.net:8989/",
        "service-radarr": "http://server.example.ts.net:7878/",
        "service-qbittorrent": "http://server.example.ts.net:18080/",
    }
    for name in (
        "service-jellyfin",
        "service-seerr",
        "service-sonarr",
        "service-radarr",
        "service-qbittorrent",
    ):
        assert cards[name]["target"] == "_blank"
        assert cards[name]["rel"] == "noopener noreferrer"


def test_status_dashboard_disables_links_without_valid_tailscale_hostname(monkeypatch) -> None:
    client = TestClient(create_app(snapshot_provider=lambda: _snapshot()))
    for hostname in (None, "https://server.example.ts.net/\" onmouseover=\"alert(1)"):
        if hostname is None:
            monkeypatch.delenv("HOMESERVER_TAILSCALE_HOSTNAME", raising=False)
        else:
            monkeypatch.setenv("HOMESERVER_TAILSCALE_HOSTNAME", hostname)

        page = client.get("/ui/status")
        cards = _service_cards(page.text)

        assert page.status_code == 200
        for name in (
            "service-jellyfin",
            "service-seerr",
            "service-sonarr",
            "service-radarr",
            "service-qbittorrent",
        ):
            assert "href" not in cards[name]
            assert cards[name]["aria-disabled"] == "true"
        assert "Hostname Tailscale não configurado" in page.text
        if hostname is not None:
            assert hostname not in page.text
