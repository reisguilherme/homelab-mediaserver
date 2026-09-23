import httpx
import pytest

from homeserver_control.adapters.http import EffectUncertain
from homeserver_control.adapters.qbittorrent import QBittorrentAdapter


def test_adapter_forwards_only_verified_torrent_bytes() -> None:
    paths: list[str] = []
    infohash = "a" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=session"})
        if request.url.path == "/api/v2/torrents/add":
            assert b'name="torrents"' in request.content
            assert b"approved.torrent" in request.content
            assert b'name="urls"' not in request.content
            assert b"/data/torrents" in request.content
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/info":
            assert request.url.params["hashes"] == infohash
            return httpx.Response(200, json=[{
                "hash": infohash, "category": "sonarr", "save_path": "/data/torrents",
            }])
        raise AssertionError(request.url.path)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = QBittorrentAdapter(
        base_url="http://qbittorrent:8080", username="admin", password="secret", client=client
    )
    result = adapter.add_torrent(
        {
            "infohash": infohash,
            "savepath": "/data/torrents",
            "category": "sonarr",
            "torrent_bytes": b"verified metadata",
        }
    )
    assert result["accepted"] is True
    assert paths == ["/api/v2/auth/login", "/api/v2/torrents/add", "/api/v2/torrents/info"]


def test_adapter_forwards_permitted_magnet() -> None:
    infohash = "a" * 40
    magnet = f"magnet:?xt=urn:btih:{infohash}&dn=Film"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=session"})
        if request.url.path == "/api/v2/torrents/add":
            assert b'name="urls"' in request.content
            assert magnet.encode() in request.content
            assert b'name="torrents"' not in request.content
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/info":
            assert request.url.params["hashes"] == infohash
            return httpx.Response(200, json=[{
                "hash": infohash, "category": "radarr", "save_path": "/data/torrents",
            }])
        raise AssertionError(request.url.path)

    adapter = QBittorrentAdapter(
        base_url="http://qbittorrent:8080", username="admin", password="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert adapter.add_torrent({
        "infohash": infohash, "savepath": "/data/torrents",
        "category": "radarr", "magnet_url": magnet,
    })["accepted"] is True


def test_adapter_waits_for_torrent_to_appear_after_accepted_add() -> None:
    infohash = "c" * 40
    info_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal info_reads
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/info":
            assert request.url.params["hashes"] == infohash
            info_reads += 1
            return httpx.Response(200, json=[{
                "hash": infohash, "category": "sonarr", "save_path": "/data/torrents",
            }] if info_reads == 3 else [])
        raise AssertionError(request.url.path)

    adapter = QBittorrentAdapter(
        base_url="http://qbittorrent:8080", username="admin", password="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert adapter.add_torrent({
        "infohash": infohash, "savepath": "/data/torrents",
        "category": "sonarr", "torrent_bytes": b"verified metadata",
    }) == {"accepted": True, "infohash": infohash}
    assert info_reads == 3


def test_adapter_keeps_uncertain_result_when_accepted_torrent_never_appears() -> None:
    infohash = "d" * 40
    info_reads = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal info_reads
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/info":
            info_reads += 1
            return httpx.Response(200, json=[])
        raise AssertionError(request.url.path)

    adapter = QBittorrentAdapter(
        base_url="http://qbittorrent:8080", username="admin", password="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(EffectUncertain, match="accepted but torrent was not visible"):
        adapter.add_torrent({
            "infohash": infohash, "savepath": "/data/torrents",
            "category": "sonarr", "torrent_bytes": b"verified metadata",
        })
    assert info_reads == 5


@pytest.mark.parametrize("returned", [
    {"hash": "f" * 40, "category": "sonarr", "save_path": "/data/torrents"},
    {"hash": "e" * 40, "category": "radarr", "save_path": "/data/torrents"},
    {"hash": "e" * 40, "category": "sonarr", "save_path": "/other"},
])
def test_adapter_does_not_confirm_wrong_torrent_identity(returned) -> None:
    infohash = "e" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/add":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[returned])
        raise AssertionError(request.url.path)

    adapter = QBittorrentAdapter(
        base_url="http://qbittorrent:8080", username="admin", password="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(EffectUncertain):
        adapter.add_torrent({
            "infohash": infohash, "savepath": "/data/torrents",
            "category": "sonarr", "torrent_bytes": b"verified metadata",
        })


def test_adapter_starts_or_stops_only_one_exact_torrent_hash() -> None:
    infohash = "b" * 40
    commands = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.", headers={"set-cookie": "SID=session"})
        commands.append((request.url.path, request.content))
        return httpx.Response(200, text="")

    adapter = QBittorrentAdapter(
        base_url="http://qbittorrent:8080", username="admin", password="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    adapter.set_running(infohash, running=False)
    adapter.set_running(infohash, running=True)
    assert commands == [
        ("/api/v2/torrents/stop", f"hashes={infohash}".encode()),
        ("/api/v2/torrents/start", f"hashes={infohash}".encode()),
    ]
    with pytest.raises(ValueError, match="invalid infohash"):
        adapter.set_running("all", running=False)
    assert len(commands) == 2


def test_adapter_promotes_only_one_validated_torrent_hash() -> None:
    infohash = "b" * 40
    commands: list[tuple[str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        commands.append((request.url.path, request.content))
        return httpx.Response(200, text="")

    adapter = QBittorrentAdapter(
        base_url="http://qbittorrent:8080", username="admin", password="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    adapter.top_priority(infohash.upper())
    assert commands == [
        ("/api/v2/torrents/topPrio", f"hashes={infohash}".encode()),
    ]
    for invalid in ("all", f"{infohash}|{'c' * 40}", "not-a-hash"):
        with pytest.raises(ValueError, match="invalid infohash"):
            adapter.top_priority(invalid)
    assert len(commands) == 1
