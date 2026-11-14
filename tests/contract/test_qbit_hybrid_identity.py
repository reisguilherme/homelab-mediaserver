"""Hybrid native IDs are derived from exact metadata; permits keep v1 identity."""

from hashlib import sha1, sha256
from urllib.parse import parse_qs

import httpx
import pytest

from homeserver_control.adapters.http import ContractError
from homeserver_control.adapters.qbittorrent import QBittorrentAdapter


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(map(encode, value)) + b"e"
    return b"d" + b"".join(encode(k) + encode(v) for k, v in sorted(value.items())) + b"e"


@pytest.fixture
def identity():
    info = {
        b"file tree": {b"video.mkv": {b"": {b"length": 10, b"pieces root": b"a" * 32}}},
        b"files": [{b"length": 10, b"path": [b"video.mkv"]}],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
        b"pieces": b"a" * 20,
    }
    raw = encode(info)
    torrent = encode({b"announce": b"https://fixture.test/announce", b"info": info})
    v1, v2 = sha1(raw).hexdigest(), sha256(raw).hexdigest()
    row = {
        "hash": v2[:40],
        "infohash_v1": v1,
        "infohash_v2": v2,
        "category": "sonarr",
        "save_path": "/data/torrents",
        "total_size": 10,
    }
    return torrent, v1, v2, row


def adapter(identity, rows=None, provider=True):
    torrent, v1, v2, row = identity
    calls = []
    metadata_reads = []

    def metadata(value):
        metadata_reads.append(value)
        return torrent if value == v1 else None

    def handler(request):
        calls.append(request)
        path = request.url.path
        if path == "/api/v2/torrents/info":
            query = request.url.params.get("hashes")
            assert query is None or query == v2[:40]
            return httpx.Response(200, json=rows if rows is not None else [dict(row)])
        if path == "/api/v2/torrents/properties":
            assert request.url.params["hash"] == v2[:40]
            return httpx.Response(200, json=dict(row))
        if path == "/api/v2/torrents/files":
            assert request.url.params["hash"] == v2[:40]
            return httpx.Response(200, json=[{"name": "Show/video.mkv", "size": 10}])
        assert request.method == "POST"
        return httpx.Response(200, text="Ok.")

    client = QBittorrentAdapter(
        base_url="http://fixture",
        username="fixture",
        password="fixture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **({"metadata_provider": metadata} if provider else {}),
    )
    client._logged_in = True
    return client, calls, metadata_reads


def test_find_and_global_read_return_canonical_v1(identity):
    client, calls, reads = adapter(identity)
    _, v1, v2, row = identity
    assert client.find_by_infohash(v1) == [dict(row, hash=v1)]
    assert client.read("/api/v2/torrents/info") == [dict(row, hash=v1)]
    assert all(call.method == "GET" for call in calls)
    assert reads == [v1, v1]


@pytest.mark.parametrize("route", ["info", "files", "properties"])
def test_reads_translate_only_derived_native_id(identity, route):
    client, calls, _ = adapter(identity)
    _, v1, _, _ = identity
    result = client.read("/api/v2/torrents/" + route, {"hashes" if route == "info" else "hash": v1})
    assert calls[-1].url.params["hashes" if route == "info" else "hash"] == identity[2][:40]
    if route == "info":
        assert result[0]["hash"] == v1
    elif route == "properties":
        assert result["hash"] == v1
    else:
        assert result == [{"name": "Show/video.mkv", "size": 10}]


@pytest.mark.parametrize("action", ["stop", "start", "priority", "delete"])
def test_mutations_get_exact_identity_then_post_one_native_id(identity, action):
    client, calls, _ = adapter(identity)
    _, v1, v2, _ = identity
    if action in ("stop", "start"):
        client.set_running(v1, running=action == "start")
    elif action == "priority":
        client.top_priority(v1)
    else:
        client.delete_torrent(v1, delete_files=True)
    assert [call.method for call in calls] == ["GET", "POST"]
    assert parse_qs(calls[-1].content.decode())["hashes"] == [v2[:40]]


def test_accepted_hybrid_add_uses_its_exact_bytes_without_repeat_add(identity):
    client, calls, _ = adapter(identity, provider=False)
    torrent, v1, _, _ = identity
    assert client.add_torrent(
        {
            "infohash": v1,
            "torrent_bytes": torrent,
            "savepath": "/data/torrents",
            "category": "sonarr",
        }
    ) == {
        "accepted": True,
        "infohash": v1,
    }
    assert [call.method for call in calls] == ["POST", "GET"]


@pytest.mark.parametrize(
    "change", ["v1", "v2", "id", "prefix_collision", "duplicate", "same_v1_other_id"]
)
def test_alias_ambiguity_and_tampering_fail_before_native_post(identity, change):
    torrent, v1, v2, original = identity
    row = dict(original)
    if change == "v1":
        row["infohash_v1"] = "f" * 40
    elif change == "v2":
        row["infohash_v2"] = "f" * 64
    elif change == "id":
        row["hash"] = "f" * 40
    elif change == "prefix_collision":
        row["infohash_v2"] = v2[:40] + "f" * 24
    rows = [row]
    if change == "duplicate":
        rows.append(dict(row))
    elif change == "same_v1_other_id":
        rows.append(dict(row, hash="e" * 40))
    client, calls, _ = adapter(identity, rows=rows)
    with pytest.raises(ContractError):
        client.set_running(v1, running=False)
    assert all(call.method == "GET" for call in calls)


def test_wrong_metadata_binding_is_rejected_before_add(identity):
    client, calls, _ = adapter(identity, provider=False)
    with pytest.raises(ContractError):
        client.add_torrent(
            {
                "infohash": "f" * 40,
                "torrent_bytes": identity[0],
                "savepath": "/data/torrents",
                "category": "sonarr",
            }
        )
    assert calls == []


def test_global_reads_do_not_load_every_v1_descriptor(identity):
    row = identity[3]
    legacy = [
        {"hash": f"{index:040x}", "infohash_v1": f"{index:040x}", "infohash_v2": ""}
        for index in range(1, 250)
    ]
    client, _, reads = adapter(identity, rows=[row, *legacy])
    result = client.read("/api/v2/torrents/info")
    assert len(result) == 250 and result[1:] == legacy
    assert reads == [identity[1]]


def test_mixed_hashes_filter_preserves_category_and_caller_params(identity):
    torrent, v1, v2, row = identity
    legacy = "e" * 40
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["hashes"] == v2[:40] + "|" + legacy
        assert request.url.params["category"] == "sonarr"
        return httpx.Response(200, json=[row, {"hash": legacy}])

    client = QBittorrentAdapter(
        base_url="http://fixture",
        username="fixture",
        password="fixture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        metadata_provider=lambda value: torrent if value == v1 else None,
    )
    client._logged_in = True
    params = {"category": "sonarr", "hashes": v1 + "|" + legacy}
    assert client.read("/api/v2/torrents/info", params) == [dict(row, hash=v1), {"hash": legacy}]
    assert params == {"category": "sonarr", "hashes": v1 + "|" + legacy} and len(calls) == 1


def test_raw_native_id_collision_cannot_be_hidden_by_canonicalization(identity):
    _, _, v2, row = identity
    other = {"hash": v2[:40], "infohash_v1": v2[:40], "infohash_v2": ""}
    client, _, _ = adapter(identity, rows=[row, other])
    with pytest.raises(ContractError):
        client.read("/api/v2/torrents/info")


@pytest.mark.parametrize("missing", ["callback", "content"])
@pytest.mark.parametrize(
    "operation", ["stop", "priority", "delete", "find", "info", "files", "properties", "global"]
)
def test_known_hybrid_requires_fresh_proof_for_every_operation(identity, missing, operation):
    client, calls, _ = adapter(identity)
    v1 = identity[1]
    assert client.find_by_infohash(v1)[0]["hash"] == v1
    calls.clear()
    client.metadata_provider = None if missing == "callback" else lambda value: None
    with pytest.raises(ContractError):
        if operation == "stop":
            client.set_running(v1, running=False)
        elif operation == "priority":
            client.top_priority(v1)
        elif operation == "delete":
            client.delete_torrent(v1, delete_files=True)
        elif operation == "find":
            client.find_by_infohash(v1)
        elif operation == "global":
            client.read("/api/v2/torrents/info")
        else:
            client.read(
                "/api/v2/torrents/" + operation, {"hashes" if operation == "info" else "hash": v1}
            )
    assert all(call.method == "GET" for call in calls)
    assert len(calls) == (1 if operation == "global" else 0)


def test_permitted_hybrid_magnet_add_uses_fresh_sealed_identity(identity):
    client, calls, reads = adapter(identity)
    _, v1, _, _ = identity
    assert client.add_torrent(
        {
            "infohash": v1,
            "magnet_url": "magnet:?xt=urn:btih:" + v1,
            "savepath": "/data/torrents",
            "category": "sonarr",
        }
    ) == {"accepted": True, "infohash": v1}
    assert reads == [v1] and [call.method for call in calls] == ["POST", "GET"]


def test_hash_filter_keeps_negative_category_match(identity):
    torrent, v1, v2, row = identity
    calls = []

    def handler(request):
        calls.append(request)
        assert request.url.params["hashes"] == v2[:40]
        return httpx.Response(
            200, json=[] if request.url.params.get("category") == "radarr" else [row]
        )

    client = QBittorrentAdapter(
        base_url="http://fixture",
        username="fixture",
        password="fixture",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        metadata_provider=lambda value: torrent if value == v1 else None,
    )
    client._logged_in = True
    assert client.read("/api/v2/torrents/info", {"hashes": v1, "category": "radarr"}) == []
    assert len(calls) == 1
