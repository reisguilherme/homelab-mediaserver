import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from homeserver_control.adapters.http import ContractError
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence

TORRENT = (
    b"d4:info"
    + b"d6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:pieces20:"
    + b"a" * 20
    + b"ee"
)
REPLACEMENT_TORRENT = (
    b"d4:info"
    + b"d6:lengthi124e4:name9:other.mp412:piece lengthi16384e6:pieces20:"
    + b"b" * 20
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
                     "state": "stoppedDL", "name": "private torrent"}]
        return original(path, params)
    upstream.read = read
    assert client.get("/internal/queue-capacity").status_code == 403
    response = client.get("/internal/queue-capacity", headers={"X-Arr-Token": "secret"})
    assert response.status_code == 200
    assert response.json() == [{"hash": "a" * 40, "total_size": 3000,
                                "amount_left": 2000, "state": "stoppedDL", "admitted": False}]


def _confirmed_queue_permit(
    permits: PermitRegistry, infohash: str, *, category: str = "radarr",
    reported_seeders: int = 0, reservation_suffix: str = "",
):
    permit = permits.issue(
        infohash=infohash, destination="/data/torrents", category=category,
        reservation_id=f"reservation-{infohash}{reservation_suffix}",
        scope_key="S01E01" if category == "sonarr" else None,
        metadata_sha256="a" * 64, selected_files=("media.mkv",),
        budget_bytes=123, reported_seeders=reported_seeders,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    return permit


class QueueUpstream(Upstream):
    def __init__(self, ordered_hashes: list[str], seeders: dict[str, int]) -> None:
        super().__init__()
        self.ordered_hashes = ordered_hashes
        self.seeders = seeders
        self.calls: list[str] = []
        self.queueing_enabled = True
        self.categories: dict[str, str] = {}
        self.paths: dict[str, str] = {}
        self.states: dict[str, str] = {}

    def read(self, path: str, params: dict | None = None) -> object:
        if path == "/api/v2/app/preferences":
            return {"queueing_enabled": self.queueing_enabled}
        if path == "/api/v2/torrents/info":
            return [
                {
                    "hash": infohash,
                    "category": self.categories.get(infohash, "radarr"),
                    "save_path": self.paths.get(infohash, "/data/torrents"),
                    "state": self.states.get(infohash, "queuedDL"),
                    "progress": 0.1, "amount_left": 100,
                    "priority": index + 1,
                    "num_seeds": 0, "num_complete": self.seeders.get(infohash, -1),
                    "force_start": False,
                }
                for index, infohash in enumerate(self.ordered_hashes)
            ]
        return super().read(path, params)

    def top_priority(self, infohash: str) -> None:
        self.calls.append(infohash)
        self.ordered_hashes.remove(infohash)
        self.ordered_hashes.insert(0, infohash)


def test_movie_queue_prioritizes_more_seeds_without_reordering_series() -> None:
    movies = ["a" * 40, "b" * 40, "c" * 40]
    episode = "d" * 40
    permits = PermitRegistry()
    for infohash, reported in zip(movies, (2, 30, 10), strict=True):
        _confirmed_queue_permit(permits, infohash, reported_seeders=reported)
    _confirmed_queue_permit(permits, episode, category="sonarr", reported_seeders=100)
    upstream = QueueUpstream([movies[0], episode, movies[1], movies[2]], {
        movies[0]: 2, movies[1]: 30, movies[2]: 10, episode: 100,
    })
    upstream.categories[episode] = "sonarr"
    upstream.states[episode] = "stoppedDL"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    response = client.post("/internal/prioritize-movies", headers={"X-Arr-Token": "secret"})
    assert response.status_code == 200
    assert response.json() == {"state": "reordered", "count": 3}
    assert upstream.ordered_hashes == [movies[1], movies[2], movies[0], episode]
    assert upstream.calls == [movies[0], movies[2], movies[1]]

    second = client.post("/internal/prioritize-movies", headers={"X-Arr-Token": "secret"})
    assert second.json() == {"state": "unchanged", "count": 0}
    assert len(upstream.calls) == 3


def test_running_first_episode_stays_ahead_of_seed_ranked_movies() -> None:
    low, high, first = "a" * 40, "b" * 40, "c" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)
    episode = _confirmed_queue_permit(
        permits, first, category="sonarr", reported_seeders=0,
    )
    permits.list_active_confirmed_episodes = lambda: [("season:tmdb:123", episode)]
    upstream = QueueUpstream([first, low, high], {low: 1, high: 50, first: 1})
    upstream.categories[first] = "sonarr"
    upstream.states[first] = "downloading"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    response = client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    )
    assert response.json() == {"state": "reordered", "count": 3}
    assert upstream.ordered_hashes == [first, high, low]
    assert first in upstream.calls


def test_running_episode_moves_ahead_even_when_movies_are_already_seed_ranked() -> None:
    low, high, first = "a" * 40, "b" * 40, "c" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)
    episode = _confirmed_queue_permit(permits, first, category="sonarr")
    permits.list_active_confirmed_episodes = lambda: [("season:tmdb:123", episode)]
    upstream = QueueUpstream([high, low, first], {low: 1, high: 50, first: 1})
    upstream.categories[first] = "sonarr"
    upstream.states[first] = "downloading"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).json() == {"state": "reordered", "count": 3}
    assert upstream.ordered_hashes == [first, high, low]
    assert upstream.calls == [first]


def test_movie_queue_uses_in_memory_registry_episode_grouping() -> None:
    movie, first = "a" * 40, "b" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, movie, reported_seeders=10)
    _confirmed_queue_permit(permits, first, category="sonarr")
    upstream = QueueUpstream([movie, first], {movie: 10, first: 1})
    upstream.categories[first] = "sonarr"
    upstream.states[first] = "downloading"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).json() == {"state": "reordered", "count": 2}
    assert upstream.ordered_hashes == [first, movie]


def test_first_episodes_of_two_series_keep_their_relative_queue_order() -> None:
    low, high, first_a, first_b = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)
    episode_a = _confirmed_queue_permit(permits, first_a, category="sonarr")
    episode_b = _confirmed_queue_permit(permits, first_b, category="sonarr")
    permits.list_active_confirmed_episodes = lambda: [
        ("season:tmdb:111", episode_a), ("season:tmdb:222", episode_b),
    ]
    upstream = QueueUpstream([low, first_b, high, first_a], {
        low: 1, high: 50, first_a: 1, first_b: 1,
    })
    upstream.categories[first_a] = upstream.categories[first_b] = "sonarr"
    upstream.states[first_a] = upstream.states[first_b] = "downloading"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).json() == {"state": "reordered", "count": 4}
    assert upstream.ordered_hashes == [first_b, first_a, high, low]


def test_episode_scope_comparison_is_numeric() -> None:
    movie, earlier, later = "a" * 40, "b" * 40, "c" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, movie, reported_seeders=10)
    first = _confirmed_queue_permit(permits, earlier, category="sonarr")
    future = _confirmed_queue_permit(permits, later, category="sonarr")
    first.scope_key = "S01E99"
    future.scope_key = "S01E100"
    permits.list_active_confirmed_episodes = lambda: [
        ("season:tmdb:123", future), ("season:tmdb:123", first),
    ]
    upstream = QueueUpstream([later, movie, earlier], {
        later: 10, movie: 10, earlier: 1,
    })
    upstream.categories[earlier] = upstream.categories[later] = "sonarr"
    upstream.states[earlier] = upstream.states[later] = "downloading"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).json() == {"state": "reordered", "count": 2}
    assert upstream.ordered_hashes[0] == earlier
    assert future.infohash not in upstream.calls


def test_future_stopped_episode_is_not_promoted_across_seasons() -> None:
    low, high, first, later = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)
    early = _confirmed_queue_permit(permits, first, category="sonarr")
    future = _confirmed_queue_permit(permits, later, category="sonarr")
    early.scope_key = "S01E100"
    future.scope_key = "S02E01"
    permits.list_active_confirmed_episodes = lambda: [
        ("season:tmdb:123", future), ("season:tmdb:123", early),
    ]
    upstream = QueueUpstream([later, first, low, high], {
        low: 1, high: 50, first: 1, later: 100,
    })
    upstream.categories[first] = upstream.categories[later] = "sonarr"
    upstream.states[first] = "downloading"
    upstream.states[later] = "stoppedDL"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).json() == {"state": "reordered", "count": 3}
    assert upstream.ordered_hashes[0] == first
    assert upstream.ordered_hashes.index(high) < upstream.ordered_hashes.index(low)
    assert later not in upstream.calls


def test_missing_first_episode_does_not_promote_later_episode() -> None:
    low, high, first, later = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)
    early = _confirmed_queue_permit(permits, first, category="sonarr")
    future = _confirmed_queue_permit(permits, later, category="sonarr")
    early.scope_key = "S01E01"
    future.scope_key = "S01E02"
    permits.list_active_confirmed_episodes = lambda: [
        ("season:tmdb:123", early), ("season:tmdb:123", future),
    ]
    upstream = QueueUpstream([low, high, later], {low: 1, high: 50, later: 100})
    upstream.categories[later] = "sonarr"
    upstream.states[later] = "downloading"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).json() == {"state": "reordered", "count": 2}
    assert later not in upstream.calls


def test_movie_queue_uses_reported_seeds_when_qbit_has_no_queued_observation() -> None:
    low, high = "a" * 40, "b" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)
    upstream = QueueUpstream([low, high], {low: -1, high: -1})
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).json() == {"state": "reordered", "count": 2}
    assert upstream.ordered_hashes == [high, low]


def test_movie_queue_reads_persisted_reported_seeds_for_queued_movies(tmp_path) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    low, high = "a" * 40, "b" * 40
    for index, (infohash, reported) in enumerate(((low, 1), (high, 50)), start=1):
        reservation = repository.reserve(
            request_id=f"seerr:movie:{index}", source_id=f"seer-{index}",
            media_key=f"movie:tmdb:{index}", filesystem_id="test-uuid",
            budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
        )
        permit = permits.issue(
            infohash=infohash, destination="/data/torrents", category="radarr",
            reservation_id=reservation.reservation_id, metadata_sha256="a" * 64,
            selected_files=("movie.mkv",), budget_bytes=123,
            reported_seeders=reported,
            capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        permits.authorize(
            token=permit.token, infohash=permit.infohash,
            destination=permit.destination, metadata_sha256=permit.metadata_sha256,
            effect=lambda _: {"accepted": True},
        )
    # A fresh registry proves the priority endpoint reads the stored seed reports.
    upstream = QueueUpstream([low, high], {low: -1, high: -1})
    client = TestClient(create_app(
        permits=PermitRegistry(database), upstream=upstream, arr_token="secret",
    ))

    response = client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    )
    assert response.json() == {"state": "reordered", "count": 2}
    assert upstream.ordered_hashes == [high, low]


def test_movie_queue_requires_worker_and_enabled_qbit_queue() -> None:
    permits = PermitRegistry()
    infohash = "a" * 40
    _confirmed_queue_permit(permits, infohash, reported_seeders=10)
    upstream = QueueUpstream([infohash], {infohash: 10})
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post("/internal/prioritize-movies").status_code == 403
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    assert client.post("/internal/prioritize-movies").status_code == 403
    upstream.queueing_enabled = False
    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).status_code == 409
    assert upstream.calls == []


def test_movie_queue_rejects_ambiguous_permits_for_one_torrent() -> None:
    low, high = "a" * 40, "b" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)
    _confirmed_queue_permit(
        permits, high, reported_seeders=100, reservation_suffix="-duplicate",
    )
    upstream = QueueUpstream([low, high], {low: 1, high: 50})
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    response = client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    )
    assert response.status_code == 409
    assert upstream.calls == []


def test_movie_queue_rejects_changed_torrent_identity_after_priority_update() -> None:
    low, high = "a" * 40, "b" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)

    class ChangedAfterPromotion(QueueUpstream):
        def top_priority(self, infohash: str) -> None:
            super().top_priority(infohash)
            self.paths[infohash] = "/data/other"

    upstream = ChangedAfterPromotion([low, high], {low: 1, high: 50})
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    response = client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    )
    assert response.status_code == 409
    assert upstream.calls == [low, high]


@pytest.mark.parametrize("changed", ["category", "save_path", "state", "force_start"])
def test_movie_queue_ignores_torrents_with_changed_identity_or_unsafe_state(changed) -> None:
    low, high = "a" * 40, "b" * 40
    permits = PermitRegistry()
    _confirmed_queue_permit(permits, low, reported_seeders=1)
    _confirmed_queue_permit(permits, high, reported_seeders=50)
    upstream = QueueUpstream([low, high], {low: 1, high: 50})
    if changed == "category":
        upstream.categories[high] = "sonarr"
    elif changed == "save_path":
        upstream.paths[high] = "/other"
    elif changed == "state":
        upstream.states[high] = "stoppedDL"
    else:
        upstream.states[high] = "forcedDL"
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))

    assert client.post(
        "/internal/prioritize-movies", headers={"X-Arr-Token": "secret"},
    ).json() == {"state": "unchanged", "count": 0}
    assert upstream.calls == []


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


def test_internal_inspected_add_rejects_mismatched_permit_token() -> None:
    client, permits, upstream = _client()
    inspected = inspect_torrent(TORRENT)
    permits.issue(
        infohash=inspected.infohash, destination="/data/torrents",
        category="radarr", expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256=inspected.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
    )
    wrong = permits.issue(
        infohash="f" * 40, destination="/data/torrents",
        category="radarr", expires_at=datetime.now(UTC) + timedelta(minutes=5),
        metadata_sha256="e" * 64,
        selected_files=("other.mp4",), budget_bytes=123,
    )
    response = client.post(
        "/api/v2/torrents/add",
        data={"category": "radarr", "savepath": "/data/torrents"},
        files={"torrents": ("approved.torrent", TORRENT)},
        headers={"X-Arr-Token": "secret", "X-Admission-Permit": wrong.token},
    )
    assert response.status_code == 403
    assert upstream.added == []


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


def test_internal_repair_accepts_metadata_applied_despite_duplicate_response(tmp_path) -> None:
    inspected = inspect_torrent(TORRENT)
    permits = PermitRegistry()
    permit = permits.issue(
        infohash=inspected.infohash, destination="/data/torrents", category="sonarr",
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

    class DuplicateUpstream(Upstream):
        total_size = -1

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [{"hash": inspected.infohash, "total_size": self.total_size,
                         "downloaded": 0, "progress": 0}]
            return super().read(path, params)

        def add_torrent(self, payload):
            self.total_size = 123
            raise ContractError("qBittorrent add response is incompatible")

    client = TestClient(create_app(
        permits=permits, upstream=DuplicateUpstream(), arr_token="secret", torrent_store=store,
    ))
    response = client.post(
        "/internal/repair-metadata", json={"permit_token": permit.token},
        headers={"X-Arr-Token": "secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"state": "metadata_available"}


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


def test_internal_series_queue_state_stops_and_starts_only_confirmed_episode() -> None:
    inspected = inspect_torrent(TORRENT)
    permits = PermitRegistry()
    permit = permits.issue(
        infohash=inspected.infohash, destination="/data/torrents",
        category="sonarr", reservation_id="season-reservation",
        scope_key="S01E02", metadata_sha256=inspected.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )

    class QueueUpstream(Upstream):
        state = "downloading"

        def __init__(self):
            super().__init__()
            self.mutations = []

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [{"hash": inspected.infohash, "category": "sonarr",
                         "save_path": "/data/torrents", "progress": 0.5,
                         "state": self.state}]
            return super().read(path, params)

        def set_running(self, infohash, *, running):
            self.mutations.append((infohash, running))
            self.state = "downloading" if running else "stoppedDL"

    upstream = QueueUpstream()
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))
    body = {"permit_token": permit.token, "action": "stop"}
    assert client.post("/internal/series-queue-state", json=body).status_code == 403
    client.post("/api/v2/auth/login", data={"username": "arr", "password": "secret"})
    assert client.post("/internal/series-queue-state", json=body).status_code == 403
    headers = {"X-Arr-Token": "secret"}
    assert client.post("/internal/series-queue-state", json=body, headers=headers).json() == {
        "state": "stopped"
    }
    assert client.post("/internal/series-queue-state", json=body, headers=headers).json() == {
        "state": "already_stopped"
    }
    assert client.post("/internal/series-queue-state", json={
        **body, "action": "start",
    }, headers=headers).json() == {"state": "started"}
    assert upstream.mutations == [(inspected.infohash, False), (inspected.infohash, True)]


def test_internal_series_queue_state_rejects_other_torrents_and_completed_data() -> None:
    inspected = inspect_torrent(TORRENT)
    permits = PermitRegistry()
    permit = permits.issue(
        infohash=inspected.infohash, destination="/data/torrents",
        category="sonarr", reservation_id="season-reservation",
        scope_key="S01E02", metadata_sha256=inspected.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    bad_permit = permits.issue(
        infohash="a" * 40, destination="/data/torrents", category="radarr",
        reservation_id="movie-reservation", metadata_sha256="a" * 64,
        selected_files=("movie.mp4",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    for item in (permit, bad_permit):
        permits.authorize(
            token=item.token, infohash=item.infohash,
            destination=item.destination, metadata_sha256=item.metadata_sha256,
            effect=lambda _: {"accepted": True},
        )

    class QueueUpstream(Upstream):
        info = {"hash": inspected.infohash, "category": "radarr",
                "save_path": "/data/torrents", "progress": 0.5,
                "state": "downloading"}

        def __init__(self):
            super().__init__()
            self.mutations = []

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [self.info]
            return super().read(path, params)

        def set_running(self, infohash, *, running):
            self.mutations.append((infohash, running))

    upstream = QueueUpstream()
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))
    headers = {"X-Arr-Token": "secret"}
    body = {"permit_token": permit.token, "action": "stop"}
    assert client.post("/internal/series-queue-state", json={
        **body, "permit_token": bad_permit.token,
    }, headers=headers).status_code == 403
    assert client.post("/internal/series-queue-state", json={
        **body, "action": "delete",
    }, headers=headers).status_code == 422
    assert client.post(
        "/internal/series-queue-state", json=body, headers=headers
    ).status_code == 409
    upstream.info = {**upstream.info, "category": "sonarr", "save_path": "/other"}
    assert client.post(
        "/internal/series-queue-state", json=body, headers=headers
    ).status_code == 409
    upstream.info = {**upstream.info, "save_path": "/data/torrents", "progress": 1.0}
    assert client.post("/internal/series-queue-state", json=body, headers=headers).json() == {
        "state": "complete"
    }
    assert upstream.mutations == []


def test_internal_source_health_and_stop_are_bound_to_confirmed_movie_permit() -> None:
    inspected = inspect_torrent(TORRENT)
    permits = PermitRegistry()
    permit = permits.issue(
        infohash=inspected.infohash, destination="/data/torrents",
        category="radarr", reservation_id="movie-reservation",
        metadata_sha256=inspected.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )

    class SourceUpstream(Upstream):
        state = "downloading"

        def __init__(self):
            super().__init__()
            self.mutations = []

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [{
                    "hash": inspected.infohash, "category": "radarr",
                    "save_path": "/data/torrents", "progress": 0.5,
                    "downloaded": 62, "amount_left": 61, "num_seeds": 0,
                    "dlspeed": 0, "state": self.state,
                }]
            return super().read(path, params)

        def set_running(self, infohash, *, running):
            self.mutations.append((infohash, running))
            self.state = "downloading" if running else "stoppedDL"

    upstream = SourceUpstream()
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))
    path = "/internal/torrent-health"
    body = {"permit_token": permit.token, "action": "stop"}
    assert client.get(path).status_code == 403
    assert client.post("/internal/source-state", json=body).status_code == 403
    headers = {"X-Arr-Token": "secret", "X-Admission-Permit": permit.token}
    assert client.get(
        f"{path}?permit_token={permit.token}", headers={"X-Arr-Token": "secret"},
    ).status_code == 403
    assert client.get(path, headers=headers).json() == {
        "hash": inspected.infohash, "progress": 0.5,
        "downloaded": 62, "amount_left": 61, "num_seeds": 0,
        "dlspeed": 0, "state": "downloading",
    }
    assert client.post("/internal/source-state", json=body, headers=headers).json() == {
        "state": "stopped"
    }
    assert client.post("/internal/source-state", json=body, headers=headers).json() == {
        "state": "already_stopped"
    }
    assert client.post("/internal/source-state", json={
        **body, "action": "start",
    }, headers=headers).json() == {"state": "started"}
    assert upstream.mutations == [(inspected.infohash, False), (inspected.infohash, True)]


def test_internal_source_state_rejects_completed_or_changed_torrent() -> None:
    inspected = inspect_torrent(TORRENT)
    permits = PermitRegistry()
    permit = permits.issue(
        infohash=inspected.infohash, destination="/data/torrents",
        category="sonarr", reservation_id="season-reservation", scope_key="S01E01",
        metadata_sha256=inspected.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )

    class SourceUpstream(Upstream):
        info = {"hash": inspected.infohash, "category": "sonarr",
                "save_path": "/other", "progress": 0.5,
                "downloaded": 62, "amount_left": 61, "num_seeds": 0,
                "dlspeed": 0, "state": "downloading"}
        mutations = []

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [self.info]
            return super().read(path, params)

        def set_running(self, infohash, *, running):
            self.mutations.append((infohash, running))

    upstream = SourceUpstream()
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))
    headers = {"X-Arr-Token": "secret", "X-Admission-Permit": permit.token}
    body = {"permit_token": permit.token, "action": "stop"}
    assert client.get(
        "/internal/torrent-health", headers=headers,
    ).status_code == 409
    assert client.post("/internal/source-state", json=body, headers=headers).status_code == 409
    upstream.info = {**upstream.info, "save_path": "/data/torrents", "progress": 1,
                     "amount_left": 0}
    assert client.post("/internal/source-state", json=body, headers=headers).status_code == 409
    assert upstream.mutations == []


def test_internal_source_state_waits_for_bounded_qbit_stop_readback() -> None:
    inspected = inspect_torrent(TORRENT)
    permits = PermitRegistry()
    permit = permits.issue(
        infohash=inspected.infohash, destination="/data/torrents",
        category="radarr", reservation_id="movie-reservation",
        metadata_sha256=inspected.metadata_sha256,
        selected_files=("test.mp4",), budget_bytes=123,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )

    class DelayedUpstream(Upstream):
        reads_after_stop = 0
        stop_requested = False

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                if self.stop_requested:
                    self.reads_after_stop += 1
                state = (
                    "stoppedDL" if self.stop_requested and self.reads_after_stop >= 3
                    else "downloading"
                )
                return [{
                    "hash": inspected.infohash, "category": "radarr",
                    "save_path": "/data/torrents", "progress": 0.5,
                    "amount_left": 61, "state": state,
                }]
            return super().read(path, params)

        def set_running(self, infohash, *, running):
            assert infohash == inspected.infohash and running is False
            self.stop_requested = True

    upstream = DelayedUpstream()
    client = TestClient(create_app(permits=permits, upstream=upstream, arr_token="secret"))
    response = client.post(
        "/internal/source-state",
        json={"permit_token": permit.token, "action": "stop"},
        headers={"X-Arr-Token": "secret"},
    )
    assert response.json() == {"state": "stopped"}
    assert upstream.reads_after_stop == 3


@pytest.mark.parametrize("uncertain_state", ["unknown", "dispatching"])
def test_reconcile_uncertain_replacement_only_after_verified_qbit_presence(
    tmp_path, uncertain_state,
) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="seerr:replacement", source_id="replacement",
        media_key="movie:tmdb:12", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    assert reservation.reservation_id
    old = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reservation.reservation_id, selected_files=("old.mp4",),
        budget_bytes=123, expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=1_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=old.token, infohash=old.infohash,
        destination=old.destination, metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    inspected = inspect_torrent(REPLACEMENT_TORRENT)
    new = permits.replace_confirmed(
        old.token, infohash=inspected.infohash,
        metadata_sha256=inspected.metadata_sha256,
        selected_files=("other.mp4",), budget_bytes=inspected.total_bytes,
        capacity=CapacityEvidence(
            free_bytes=1_000, remaining_by_hash={old.infohash: 100},
            paused_hashes=frozenset({old.infohash}),
        ), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store = TorrentArtifactStore(database)
    store.put(new, REPLACEMENT_TORRENT)

    def uncertain(_permit):
        raise RuntimeError("upstream response lost")

    with pytest.raises(RuntimeError, match="response lost"):
        permits.authorize(
            token=new.token, infohash=new.infohash,
            destination=new.destination, metadata_sha256=new.metadata_sha256,
            effect=uncertain,
        )
    if uncertain_state == "dispatching":
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE gateway_permits SET state = 'dispatching' WHERE token = ?",
                (new.token,),
            )
    assert permits.get(new.token).state == uncertain_state

    class ReplacementUpstream(Upstream):
        info = None

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [self.info] if self.info is not None else []
            return super().read(path, params)

    upstream = ReplacementUpstream()
    client = TestClient(create_app(
        permits=permits, upstream=upstream, arr_token="secret", torrent_store=store,
    ))
    path = "/internal/reconcile-source"
    body = {"permit_token": new.token}
    headers = {"X-Arr-Token": "secret"}
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json={"permit_token": old.token}, headers=headers).status_code == 403
    assert client.post(path, json=body, headers=headers).json() == {"state": "missing"}
    assert permits.get(new.token).state == uncertain_state
    upstream.info = {
        "hash": new.infohash, "category": "sonarr", "save_path": "/data/torrents",
        "total_size": inspected.total_bytes,
    }
    assert client.post(path, json=body, headers=headers).status_code == 409
    assert permits.get(new.token).state == uncertain_state
    upstream.info = {**upstream.info, "category": "radarr"}
    assert client.post(path, json=body, headers=headers).json() == {"state": "confirmed"}
    assert permits.get(new.token).state == "confirmed"
    assert permits.get(new.token).result == {
        "accepted": True, "infohash": new.infohash,
    }
    assert client.post(path, json=body, headers=headers).json() == {"state": "confirmed"}


@pytest.mark.parametrize(
    ("category", "scope_key", "media_key"),
    [
        ("radarr", None, "movie:tmdb:15"),
        ("sonarr", "S01E03", "season:tmdb:15:1"),
    ],
)
@pytest.mark.parametrize("uncertain_state", ["unknown", "dispatching"])
def test_reconcile_initial_uncertain_source_with_verified_identity(
    tmp_path, category, scope_key, media_key, uncertain_state,
) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id=f"seerr:{category}", source_id=category,
        media_key=media_key, filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    inspected = inspect_torrent(TORRENT)
    permit = permits.issue(
        infohash=inspected.infohash, metadata_sha256=inspected.metadata_sha256,
        destination="/data/torrents", category=category,
        reservation_id=reservation.reservation_id, scope_key=scope_key,
        selected_files=("test.mp4",), budget_bytes=inspected.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=1_000, remaining_by_hash={}),
    )
    store = TorrentArtifactStore(database)
    store.put(permit, TORRENT)
    with pytest.raises(RuntimeError, match="response lost"):
        permits.authorize(
            token=permit.token, infohash=permit.infohash,
            destination=permit.destination, metadata_sha256=permit.metadata_sha256,
            effect=lambda _: (_ for _ in ()).throw(RuntimeError("response lost")),
        )
    if uncertain_state == "dispatching":
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE gateway_permits SET state = 'dispatching' WHERE token = ?",
                (permit.token,),
            )

    class InitialUpstream(Upstream):
        info = None

        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [self.info] if self.info is not None else []
            return super().read(path, params)

    upstream = InitialUpstream()
    client = TestClient(create_app(
        permits=permits, upstream=upstream, arr_token="secret", torrent_store=store,
    ))
    path = "/internal/reconcile-source"
    body = {"permit_token": permit.token}
    headers = {"X-Arr-Token": "secret"}
    assert client.post(path, json=body, headers=headers).json() == {"state": "missing"}
    upstream.info = {
        "hash": permit.infohash, "category": category,
        "save_path": "/data/elsewhere", "total_size": inspected.total_bytes,
    }
    assert client.post(path, json=body, headers=headers).status_code == 409
    upstream.info = {**upstream.info, "save_path": "/data/torrents", "total_size": 122}
    assert client.post(path, json=body, headers=headers).status_code == 409
    assert permits.get(permit.token).state == uncertain_state
    upstream.info = {**upstream.info, "total_size": inspected.total_bytes}
    assert client.post(path, json=body, headers=headers).json() == {"state": "confirmed"}
    assert permits.get(permit.token).state == "confirmed"
    assert permits.is_admitted(permit.infohash)
    assert client.post(path, json=body, headers=headers).json() == {"state": "confirmed"}


@pytest.mark.parametrize("invalid_state", ["revoked", "inactive_reservation"])
def test_reconcile_rejects_revoked_or_inactive_reservation(
    tmp_path, invalid_state,
) -> None:
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    permits = PermitRegistry(database)
    reservation = repository.reserve(
        request_id="seerr:inactive", source_id="inactive",
        media_key="movie:tmdb:16", filesystem_id="test-uuid",
        budget_bytes=0, free_bytes=10_000, total_bytes=20_000,
    )
    inspected = inspect_torrent(TORRENT)
    permit = permits.issue(
        infohash=inspected.infohash, metadata_sha256=inspected.metadata_sha256,
        destination="/data/torrents", category="radarr",
        reservation_id=reservation.reservation_id, selected_files=("test.mp4",),
        budget_bytes=inspected.total_bytes,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        capacity=CapacityEvidence(free_bytes=1_000, remaining_by_hash={}),
    )
    store = TorrentArtifactStore(database)
    store.put(permit, TORRENT)
    with sqlite3.connect(database) as connection:
        if invalid_state == "revoked":
            connection.execute(
                "UPDATE gateway_permits SET state = 'revoked' WHERE token = ?",
                (permit.token,),
            )
        else:
            connection.execute(
                "UPDATE reservations SET state = 'cancelled' WHERE id = ?",
                (reservation.reservation_id,),
            )

    class PresentUpstream(Upstream):
        def read(self, path, params=None):
            if path == "/api/v2/torrents/info":
                return [{
                    "hash": permit.infohash, "category": "radarr",
                    "save_path": "/data/torrents", "total_size": inspected.total_bytes,
                }]
            return super().read(path, params)

    client = TestClient(create_app(
        permits=permits, upstream=PresentUpstream(), arr_token="secret",
        torrent_store=store,
    ))
    response = client.post(
        "/internal/reconcile-source", json={"permit_token": permit.token},
        headers={"X-Arr-Token": "secret"},
    )
    assert response.status_code == 403
    assert permits.get(permit.token).state != "confirmed"
