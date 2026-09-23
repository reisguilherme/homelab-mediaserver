import httpx
import pytest

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
            return httpx.Response(200, json=[{"hash": infohash}])
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
            return httpx.Response(200, json=[{"hash": infohash}])
        raise AssertionError(request.url.path)

    adapter = QBittorrentAdapter(
        base_url="http://qbittorrent:8080", username="admin", password="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert adapter.add_torrent({
        "infohash": infohash, "savepath": "/data/torrents",
        "category": "radarr", "magnet_url": magnet,
    })["accepted"] is True


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
