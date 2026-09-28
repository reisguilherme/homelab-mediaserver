from __future__ import annotations

import asyncio
import socket
import struct

import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.worker import tracker_availability as availability


def _encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    return b"d" + b"".join(_encode(key) + _encode(item)
                            for key, item in sorted(value.items())) + b"e"


def _torrent(*trackers, private=False):
    info = {b"name": b"The.Rookie.S02E02.mkv", b"length": 16,
            b"piece length": 16, b"pieces": b"a" * 20}
    if private:
        info[b"private"] = 1
    return _encode({
        b"announce-list": [[tracker.encode()] for tracker in trackers],
        b"info": info,
    })


def _network(monkeypatch, outcomes, *, addresses=("8.8.8.8",)):
    called = []
    active = peak = 0

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", (ip, port))
                for ip in addresses]

    async def scrape(family, address, infohash):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        called.append((address, infohash))
        await asyncio.sleep(0)
        active -= 1
        return outcomes.get(address[1])

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(availability, "_scrape_udp", scrape)
    return called, lambda: peak


@pytest.mark.asyncio
async def test_probe_returns_live_seeders_not_an_indexer_claim(monkeypatch):
    torrent = _torrent("udp://tracker.example:1337/announce", "udp://second.example:6969")
    infohash = inspect_torrent(torrent).infohash
    _network(monkeypatch, {1337: 0, 6969: 20})
    probe = availability.TrackerAvailabilityProbe(fallback_trackers=())
    assert await probe(torrent, infohash) == 20


@pytest.mark.asyncio
async def test_responsive_declared_zero_is_distinct_from_unavailable(monkeypatch):
    torrent = _torrent("udp://tracker.example:1337/announce")
    infohash = inspect_torrent(torrent).infohash
    _network(monkeypatch, {1337: 0})
    assert await availability.TrackerAvailabilityProbe(fallback_trackers=())(
        torrent, infohash
    ) == 0
    _network(monkeypatch, {})
    assert await availability.TrackerAvailabilityProbe(fallback_trackers=())(
        torrent, infohash
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("other_tracker", [
    "udp://unresponsive.example:6969/announce",
    "https://unsupported.example/announce",
])
async def test_a_declared_zero_does_not_hide_an_unverified_tracker(monkeypatch, other_tracker):
    torrent = _torrent("udp://tracker.example:1337/announce", other_tracker)
    _network(monkeypatch, {1337: 0})
    probe = availability.TrackerAvailabilityProbe(fallback_trackers=())
    assert await probe(torrent, inspect_torrent(torrent).infohash) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [0, None, 7])
async def test_undeclared_fallback_can_only_establish_positive_availability(monkeypatch, reported):
    torrent = _torrent("https://tracker.example/announce")
    infohash = inspect_torrent(torrent).infohash
    _network(monkeypatch, {1337: reported})
    probe = availability.TrackerAvailabilityProbe(
        fallback_trackers=("udp://tracker.example:1337/announce",)
    )
    assert await probe(torrent, infohash) == (7 if reported == 7 else None)


@pytest.mark.asyncio
async def test_private_torrent_does_not_disclose_its_hash_to_fallback_trackers(monkeypatch):
    torrent = _torrent("https://tracker.example/announce?passkey=secret", private=True)
    called, _ = _network(monkeypatch, {1337: 30})
    probe = availability.TrackerAvailabilityProbe(
        fallback_trackers=("udp://tracker.example:1337/announce",)
    )
    assert await probe(torrent, inspect_torrent(torrent).infohash) is None
    assert called == []


@pytest.mark.asyncio
async def test_same_hash_with_different_tracker_metadata_has_distinct_availability(monkeypatch):
    dead = _torrent("udp://dead.example:1337/announce")
    alive = _torrent("udp://alive.example:6969/announce")
    infohash = inspect_torrent(dead).infohash
    assert inspect_torrent(alive).infohash == infohash
    _network(monkeypatch, {1337: 0, 6969: 7})
    probe = availability.TrackerAvailabilityProbe(fallback_trackers=())
    assert await probe(dead, infohash) == 0
    assert await probe(alive, infohash) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("tracker", [
    "udp://user:password@tracker.example:1337/announce",
    "udp://tracker.example:1337/announce?passkey=secret",
    "udp://tracker.example:1337/announce#fragment",
    "udp://127.0.0.1:1337/announce",
    "udp://192.168.1.19:1337/announce",
    "udp://100.100.100.100:1337/announce",
    "udp://[::1]:1337/announce",
    "udp://tracker.example:invalid/announce",
    "http://tracker.example:1337/announce",
])
async def test_unsafe_or_unsupported_tracker_never_sends_udp(monkeypatch, tracker):
    torrent = _torrent(tracker)
    called, _ = _network(monkeypatch, {1337: 30})
    assert await availability.TrackerAvailabilityProbe(fallback_trackers=())(
        torrent, inspect_torrent(torrent).infohash
    ) is None
    assert called == []


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses", [("192.168.1.19",), ("8.8.8.8", "127.0.0.1")])
async def test_public_hostname_resolving_to_private_addresses_is_rejected(monkeypatch, addresses):
    torrent = _torrent("udp://tracker.example:1337/announce")
    called, _ = _network(monkeypatch, {1337: 30}, addresses=addresses)
    assert await availability.TrackerAvailabilityProbe(fallback_trackers=())(
        torrent, inspect_torrent(torrent).infohash
    ) is None
    assert called == []


@pytest.mark.asyncio
async def test_metadata_identity_is_verified_before_network(monkeypatch):
    torrent = _torrent("udp://tracker.example:1337/announce")
    called, _ = _network(monkeypatch, {1337: 30})
    probe = availability.TrackerAvailabilityProbe(fallback_trackers=())
    assert await probe(torrent, "b" * 40) is None
    assert await probe(b"invalid", "b" * 40) is None
    assert called == []


@pytest.mark.asyncio
async def test_probe_bounds_requests_and_caches_for_one_minute(monkeypatch):
    torrent = _torrent(*(f"udp://tracker{number}.example:{1337 + number}/announce"
                         for number in range(8)))
    infohash = inspect_torrent(torrent).infohash
    called, peak = _network(monkeypatch, {1337: 5})
    now = [1000.0]
    monkeypatch.setattr(availability.time, "monotonic", lambda: now[0])
    probe = availability.TrackerAvailabilityProbe(fallback_trackers=())
    assert await probe(torrent, infohash) == 5
    assert await probe(torrent, infohash) == 5
    assert len(called) == 4 and peak() <= 4
    now[0] = 1061.0
    assert await probe(torrent, infohash) == 5
    assert len(called) == 8


@pytest.mark.asyncio
async def test_zero_from_first_four_trackers_does_not_hide_an_unmeasured_fifth(monkeypatch):
    torrent = _torrent(*(f"udp://tracker{number}.example:{1337 + number}/announce"
                         for number in range(5)))
    called, _ = _network(monkeypatch, {1337 + number: 0 for number in range(5)})
    probe = availability.TrackerAvailabilityProbe(fallback_trackers=())
    assert await probe(torrent, inspect_torrent(torrent).infohash) is None
    assert len(called) == 4


@pytest.mark.asyncio
async def test_unresponsive_dns_is_bounded(monkeypatch):
    torrent = _torrent("udp://tracker.example:1337/announce")

    async def hung(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", hung)
    monkeypatch.setattr(availability, "_PROBE_TIMEOUT", 0.01)
    probe = availability.TrackerAvailabilityProbe(fallback_trackers=())
    assert await probe(torrent, inspect_torrent(torrent).infohash) is None


@pytest.mark.asyncio
async def test_waiting_candidate_gets_its_timeout_after_a_tracker_slot_is_available(monkeypatch):
    dead = _torrent(*(f"udp://dead{number}.example:{1337 + number}/announce"
                      for number in range(4)))
    alive = _torrent("udp://alive.example:6969/announce")
    active = peak = 0

    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP,
                 "", ("8.8.8.8", port))]

    async def scrape(family, address, infohash):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if address[1] != 6969:
                await asyncio.Future()
            return 9
        finally:
            active -= 1

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(availability, "_scrape_udp", scrape)
    monkeypatch.setattr(availability, "_PROBE_TIMEOUT", 0.02)
    probe = availability.TrackerAvailabilityProbe(fallback_trackers=())
    results = await asyncio.gather(
        probe(dead, inspect_torrent(dead).infohash),
        probe(alive, inspect_torrent(alive).infohash),
    )
    assert results == [None, 9]
    assert peak == 4 and active == 0


class _DatagramSocket:
    def __init__(self):
        self.address = None
        self.closed = False

    def setblocking(self, blocking):
        assert blocking is False

    def connect(self, address):
        self.address = address

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class _DatagramLoop:
    def __init__(self, *, seeders=3, corrupt=None):
        self.seeders = seeders
        self.corrupt = corrupt
        self.sent = []
        self.responses = []

    async def sock_sendall(self, sock, packet):
        self.sent.append(packet)
        _, action, transaction = struct.unpack("!QII", packet[:16])
        if action == 0:
            valid = struct.pack("!IIQ", 0, transaction, 12345)
        else:
            valid = struct.pack("!IIIII", 2, transaction, self.seeders, 99999, 4)
        if self.corrupt == "transaction":
            wrong = struct.pack("!II", action, transaction ^ 1) + valid[8:]
        elif self.corrupt == "action":
            wrong = struct.pack("!II", 1, transaction) + valid[8:]
        elif self.corrupt == "short":
            wrong = valid[:7]
        elif self.corrupt == "error":
            wrong = struct.pack("!II", 3, transaction) + b"tracker failure"
        else:
            wrong = None
        if wrong:
            self.responses.append(wrong)
        self.responses.append(valid + b"extension bytes")

    async def sock_recv(self, sock, size):
        if not self.responses:
            raise TimeoutError
        return self.responses.pop(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [None, "transaction", "action", "short"])
async def test_udp_handshake_rejects_unrelated_datagrams_and_uses_seeders(monkeypatch, corrupt):
    udp_socket = _DatagramSocket()
    loop = _DatagramLoop(corrupt=corrupt)
    with monkeypatch.context() as boundary:
        boundary.setattr(availability.socket, "socket", lambda *args: udp_socket)
        boundary.setattr(availability.asyncio, "get_running_loop", lambda: loop)
        assert await availability._scrape_udp(socket.AF_INET, ("8.8.8.8", 1337), "a" * 40) == 3
    assert udp_socket.address == ("8.8.8.8", 1337) and udp_socket.closed
    assert struct.unpack("!QII", loop.sent[0])[0:2] == (0x41727101980, 0)
    assert struct.unpack("!QII", loop.sent[1][:16])[0:2] == (12345, 2)
    assert loop.sent[1][16:] == bytes.fromhex("a" * 40)


@pytest.mark.asyncio
async def test_tracker_error_is_unknown_and_closes_socket(monkeypatch):
    udp_socket = _DatagramSocket()
    loop = _DatagramLoop(corrupt="error")
    with monkeypatch.context() as boundary:
        boundary.setattr(availability.socket, "socket", lambda *args: udp_socket)
        boundary.setattr(availability.asyncio, "get_running_loop", lambda: loop)
        assert await availability._scrape_udp(socket.AF_INET, ("8.8.8.8", 1337), "a" * 40) is None
    assert udp_socket.closed
