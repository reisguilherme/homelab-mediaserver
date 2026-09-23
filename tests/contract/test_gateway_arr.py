from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi.testclient import TestClient

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore

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


def test_worker_capacity_view_includes_unmanaged_queue_without_names() -> None:
    client, _, upstream = _client()
    original = upstream.read
    def read(path, params=None):
        if path == "/api/v2/torrents/info":
            return [{"hash": "a" * 40, "total_size": 3000, "amount_left": 2000,
                     "name": "private torrent"}]
        return original(path, params)
    upstream.read = read
    assert client.get("/internal/queue-capacity").status_code == 403
    response = client.get("/internal/queue-capacity", headers={"X-Arr-Token": "secret"})
    assert response.status_code == 200
    assert response.json() == [{"hash": "a" * 40, "total_size": 3000,
                                "amount_left": 2000, "admitted": False}]


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


def test_radarr_boolean_form_state_is_accepted_only_when_starting() -> None:
    client, permits, upstream = _client()
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    inspected = inspect_torrent(TORRENT)
    permits.issue(
        infohash=inspected.infohash, destination="/data/torrents", category="radarr",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256=inspected.metadata_sha256, budget_bytes=123,
    )
    stopped = client.post(
        "/api/v2/torrents/add", data={"category": "radarr", "stopped": "True"},
        files={"torrents": ("movie.torrent", TORRENT)},
    )
    assert stopped.status_code == 422
    started = client.post(
        "/api/v2/torrents/add", data={"category": "radarr", "stopped": "False"},
        files={"torrents": ("movie.torrent", TORRENT)},
    )
    assert started.status_code == 200
    assert len(upstream.added) == 1


def test_arr_url_and_unsafe_mutations_are_rejected() -> None:
    client, _, upstream = _client()
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    assert client.post("/api/v2/torrents/add", data={"urls": "magnet:?xt=bad"}).status_code == 422
    assert client.post("/api/v2/torrents/delete", data={"hashes": "a" * 40}).status_code == 404
    assert upstream.added == []


def test_arr_magnet_requires_inspected_metadata_permit() -> None:
    client, permits, upstream = _client()
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    infohash = inspect_torrent(TORRENT).infohash
    magnet = f"magnet:?xt=urn:btih:{infohash.upper()}&dn=Film"
    form = {"urls": magnet, "category": "radarr", "savepath": "/data/torrents"}
    assert client.post("/api/v2/torrents/add", data=form).status_code == 403
    assert upstream.added == []
    permits.issue(
        infohash=infohash, destination="/data/torrents", category="radarr",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256=sha256(TORRENT).hexdigest(),
        selected_files=("test.mp4",), budget_bytes=123,
    )
    accepted = client.post("/api/v2/torrents/add", data=form)
    assert accepted.status_code == 200
    assert accepted.text == "Ok."
    assert upstream.added == [{
        "infohash": infohash, "magnet_url": magnet,
        "savepath": "/data/torrents", "category": "radarr",
    }]
    repeated = client.post(
        "/api/v2/torrents/add",
        data={"category": "radarr", "savepath": "/data/torrents"},
        files={"urls": (None, magnet)},
    )
    assert repeated.status_code == 200
    assert len(upstream.added) == 1


def test_arr_magnet_dispatches_verified_torrent_bytes_when_cached(tmp_path) -> None:
    permits = PermitRegistry()
    upstream = Upstream()
    inspected = inspect_torrent(TORRENT)
    permit = permits.issue(
        infohash=inspected.infohash, destination="/data/torrents", category="radarr",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256=inspected.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
    )
    store = TorrentArtifactStore(tmp_path / "artifacts.sqlite")
    store.put(permit, TORRENT)
    client = TestClient(create_app(
        permits=permits, upstream=upstream, arr_token="secret", torrent_store=store,
    ))
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    response = client.post("/api/v2/torrents/add", data={
        "urls": f"magnet:?xt=urn:btih:{inspected.infohash}",
        "category": "radarr", "savepath": "/data/torrents",
    })
    assert response.status_code == 200
    assert upstream.added == [{
        "infohash": inspected.infohash, "torrent_bytes": TORRENT,
        "savepath": "/data/torrents", "category": "radarr",
    }]


def test_internal_repair_resends_metadata_only_for_stalled_admitted_torrent(tmp_path) -> None:
    permits = PermitRegistry()
    inspected = inspect_torrent(TORRENT)
    permit = permits.issue(
        infohash=inspected.infohash, destination="/data/torrents", category="radarr",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256=inspected.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _permit: {"accepted": True},
    )
    store = TorrentArtifactStore(tmp_path / "artifacts.sqlite")
    store.put(permit, TORRENT)

    class StalledUpstream(Upstream):
        total_size = -1

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [{"hash": inspected.infohash, "total_size": self.total_size,
                         "downloaded": 0, "progress": 0}]
            return super().read(path, params)

        def add_torrent(self, payload):
            result = super().add_torrent(payload)
            self.total_size = 123
            return result

    upstream = StalledUpstream()
    client = TestClient(create_app(
        permits=permits, upstream=upstream, arr_token="secret", torrent_store=store,
    ))
    body = {"permit_token": permit.token}
    assert client.post("/internal/repair-metadata", json=body).status_code == 403
    response = client.post(
        "/internal/repair-metadata", json=body, headers={"X-Arr-Token": "secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"state": "metadata_available"}
    assert upstream.added[0]["torrent_bytes"] == TORRENT
    assert client.post(
        "/internal/repair-metadata", json=body, headers={"X-Arr-Token": "secret"},
    ).status_code == 409


def test_arr_magnet_rejects_mismatched_hash_and_unverified_permit() -> None:
    client, permits, upstream = _client()
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    infohash = inspect_torrent(TORRENT).infohash
    permits.issue(
        infohash=infohash, destination="/data/torrents", category="radarr",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        budget_bytes=123,
    )
    assert client.post("/api/v2/torrents/add", data={
        "urls": f"magnet:?xt=urn:btih:{infohash}", "category": "radarr",
    }).status_code == 403
    assert client.post("/api/v2/torrents/add", data={
        "urls": f"magnet:?xt=urn:btih:{'a' * 40}", "category": "radarr",
    }).status_code == 403
    assert client.post("/api/v2/torrents/add", data={
        "urls": f"magnet:?xt=urn:btih:{infohash}&xs=https%3A%2F%2Fevil.invalid",
        "category": "radarr",
    }).status_code == 422
    assert upstream.added == []
