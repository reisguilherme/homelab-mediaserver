from unittest.mock import AsyncMock

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.worker.acquisition import MovieAcquirer


def torrent(name=b"fixture.mkv"):
    return (b"d4:infod6:lengthi5e4:name" + str(len(name)).encode() + b":" + name
            + b"12:piece lengthi16e6:pieces20:aaaaaaaaaaaaaaaaaaaaee")


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", [b"d4:info", None, b"<html>unavailable</html>"])
async def test_invalid_cache_uses_verified_peer_metadata_without_changing_swarm(cache):
    data = torrent()
    identity = inspect_torrent(data).infohash
    magnet = f"magnet:?xt=urn:btih:{identity}&tr=udp://tracker.example:1337/announce"
    resolver = AsyncMock(return_value=data)

    def handler(request):
        if request.url.host == "prowlarr":
            return httpx.Response(302, headers={"location": magnet})
        return httpx.Response(404 if cache is None else 200, content=cache or b"")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = object.__new__(MovieAcquirer)
        acquirer.client, acquirer.metadata_resolver = client, resolver
        result = await acquirer._metadata("http://prowlarr/1/download")
    resolver.assert_awaited_once_with(magnet)
    assert inspect_torrent(result).infohash == identity
    assert b"udp://tracker.example:1337/announce" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("peer_result", [None, b"corrupt", torrent(b"wrong.mkv")])
async def test_failed_or_wrong_swarm_peer_metadata_is_never_accepted(peer_result):
    identity = inspect_torrent(torrent()).infohash
    resolver = AsyncMock(return_value=peer_result)

    def handler(request):
        if request.url.host == "prowlarr":
            return httpx.Response(302, headers={"location": f"magnet:?xt=urn:btih:{identity}"})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = object.__new__(MovieAcquirer)
        acquirer.client, acquirer.metadata_resolver = client, resolver
        assert await acquirer._metadata("http://prowlarr/1/download") is None
    resolver.assert_awaited_once()


@pytest.mark.asyncio
async def test_valid_http_metadata_does_not_launch_peer_resolution():
    data = torrent()
    identity = inspect_torrent(data).infohash
    resolver = AsyncMock(side_effect=AssertionError("unexpected peer lookup"))

    def handler(request):
        if request.url.host == "prowlarr":
            return httpx.Response(302, headers={"location": f"magnet:?xt=urn:btih:{identity}"})
        return httpx.Response(200, content=data)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        acquirer = object.__new__(MovieAcquirer)
        acquirer.client, acquirer.metadata_resolver = client, resolver
        assert await acquirer._metadata("http://prowlarr/1/download") == data
    resolver.assert_not_awaited()
