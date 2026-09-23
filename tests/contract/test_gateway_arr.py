from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi.testclient import TestClient

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry

TORRENT = (
    b"d4:info"
    + b"d6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:pieces20:"
    + b"a" * 20
    + b"ee"
)


class Upstream:
    def __init__(self) -> None:
        self.added: list[dict] = []

    def add_torrent(self, payload: dict) -> dict:
        self.added.append(payload)
        return {"accepted": True, "infohash": payload["infohash"]}

    def read(self, path: str, params: dict | None = None) -> object:
        return {
            "/api/v2/app/webapiVersion": "2.11.4",
            "/api/v2/app/version": "5.1.2",
            "/api/v2/app/preferences": {"save_path": "/data/torrents"},
            "/api/v2/torrents/categories": {"sonarr": {"name": "sonarr", "savePath": ""}},
            "/api/v2/torrents/info": [],
        }[path]


def _client() -> tuple[TestClient, PermitRegistry, Upstream]:
    permits = PermitRegistry()
    upstream = Upstream()
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))
    return client, permits, upstream


def test_arr_login_and_read_contract() -> None:
    client, _, _ = _client()
    assert client.get("/api/v2/app/webapiVersion").status_code == 403
    bad = client.post("/api/v2/auth/login", data={"username": "arr", "password": "bad"})
    good = client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    assert bad.text == "Fails."
    assert good.text == "Ok."
    assert client.get("/api/v2/app/webapiVersion").text == "2.11.4"
    assert client.get("/api/v2/app/preferences").json()["save_path"] == "/data/torrents"
    assert "sonarr" in client.get("/api/v2/torrents/categories").json()


def test_arr_add_requires_preissued_metadata_permit() -> None:
    client, permits, upstream = _client()
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    rejected = client.post(
        "/api/v2/torrents/add",
        data={"category": "sonarr"},
        files={"torrents": ("test.torrent", TORRENT)},
    )
    assert rejected.status_code == 403
    assert upstream.added == []
    inspected = inspect_torrent(TORRENT)
    permits.issue(
        infohash=inspected.infohash,
        destination="/data/torrents",
        category="sonarr",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256=sha256(TORRENT).hexdigest(),
        budget_bytes=123,
    )
    accepted = client.post(
        "/api/v2/torrents/add",
        data={"category": "sonarr"},
        files={"torrents": ("test.torrent", TORRENT)},
    )
    assert accepted.status_code == 200
    assert accepted.text == "Ok."
    assert len(upstream.added) == 1
    assert upstream.added[0]["infohash"] == inspected.infohash


def test_arr_url_and_unsafe_mutations_are_rejected() -> None:
    client, _, upstream = _client()
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    assert client.post("/api/v2/torrents/add", data={"urls": "magnet:?xt=bad"}).status_code == 403
    assert client.post("/api/v2/torrents/delete", data={"hashes": "a" * 40}).status_code == 404
    assert upstream.added == []
