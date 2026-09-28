"""Read seed availability from public UDP trackers without adding a torrent.

The tracker handshake and scrape follow BEP 15. An unsupported tracker or a
timeout is unknown availability, rather than evidence of an empty swarm.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
import socket
import struct
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from homeserver_control.domain.torrent_bytes import TorrentBytesError, _Decoder, inspect_torrent

_PROBE_TIMEOUT = 5.0
_MAX_TRACKERS = 4
_CACHE_SECONDS = 60
_MAX_CACHE_ENTRIES = 256
_FALLBACK_TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
)


@dataclass(frozen=True)
class _Endpoint:
    host: str
    port: int


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global and not address.is_multicast


def _endpoint(value: object) -> _Endpoint | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
    if (
        not isinstance(value, str) or len(value) > 2048
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme != "udp" or not url.hostname or url.port is None
            or not 1 <= url.port <= 65535 or url.username is not None
            or url.password is not None or url.query or url.fragment
            or url.path not in {"", "/announce"} or "%" in url.hostname
        ):
            return None
        host = url.hostname.encode("idna").decode("ascii").lower()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not _public_address(host):
                return None
        return _Endpoint(host, url.port)
    except (UnicodeError, ValueError):
        return None


async def _response(loop, udp_socket, *, action: int, transaction: int,
                    minimum_size: int) -> bytes | None:
    # A connected UDP socket accepts datagrams only from the resolved tracker.
    # Discard unrelated/late transactions, while bounding malformed responses.
    for _ in range(8):
        packet = await loop.sock_recv(udp_socket, 4096)
        if len(packet) < 8:
            continue
        returned_action, returned_transaction = struct.unpack("!II", packet[:8])
        if returned_transaction != transaction:
            continue
        if returned_action == 3:
            return None
        if returned_action == action and len(packet) >= minimum_size:
            return packet
    return None


async def _scrape_udp(family: int, address: tuple, infohash: str) -> int | None:
    loop = asyncio.get_running_loop()
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as udp_socket:
            udp_socket.setblocking(False)
            udp_socket.connect(address)
            transaction = secrets.randbits(32)
            await loop.sock_sendall(
                udp_socket, struct.pack("!QII", 0x41727101980, 0, transaction)
            )
            connected = await _response(
                loop, udp_socket, action=0, transaction=transaction, minimum_size=16
            )
            if connected is None:
                return None
            connection_id = struct.unpack("!Q", connected[8:16])[0]
            transaction = secrets.randbits(32)
            await loop.sock_sendall(
                udp_socket,
                struct.pack("!QII20s", connection_id, 2, transaction, bytes.fromhex(infohash)),
            )
            scraped = await _response(
                loop, udp_socket, action=2, transaction=transaction, minimum_size=20
            )
            # BEP 15: seeders precede completed downloads and leechers.
            return struct.unpack("!I", scraped[8:12])[0] if scraped is not None else None
    except (OSError, TimeoutError):
        return None


class TrackerAvailabilityProbe:
    """Return measured seeds, declared-tracker zero, or unknown (None).

    Only positive results from undeclared fallback trackers establish availability.
    A fallback zero does not establish that a torrent absent there has no seeds.
    A declared zero requires every selected declared endpoint to respond with zero;
    Unsupported or unmeasured declarations keep availability unknown.
    """

    def __init__(self, *, fallback_trackers: tuple[str, ...] = _FALLBACK_TRACKERS) -> None:
        self.fallback_trackers = fallback_trackers
        self._semaphore = asyncio.Semaphore(_MAX_TRACKERS)
        self._cache: dict[tuple[str, str], tuple[float, int | None]] = {}

    async def _measure(self, endpoint: _Endpoint, infohash: str) -> int | None:
        loop = asyncio.get_running_loop()
        addresses = await loop.getaddrinfo(
            endpoint.host, endpoint.port, family=socket.AF_UNSPEC,
            type=socket.SOCK_DGRAM, proto=socket.IPPROTO_UDP,
        )
        if not addresses or any(
            family not in {socket.AF_INET, socket.AF_INET6}
            or not _public_address(address[0]) or address[1] != endpoint.port
            for family, _, _, _, address in addresses
        ):
            return None
        # Connect to this already checked numeric address; never resolve it again.
        family, _, _, _, address = min(
            addresses, key=lambda item: item[0] != socket.AF_INET
        )
        return await _scrape_udp(family, address, infohash)

    async def _bounded_measure(self, endpoint: _Endpoint, infohash: str) -> int | None:
        async with self._semaphore:
            try:
                return await asyncio.wait_for(self._measure(endpoint, infohash), _PROBE_TIMEOUT)
            except (OSError, TimeoutError, ValueError):
                return None

    async def __call__(self, torrent: bytes, infohash: str) -> int | None:
        if not isinstance(infohash, str):
            return None
        try:
            inspected = inspect_torrent(torrent)
        except TorrentBytesError:
            return None
        if inspected.infohash != infohash.lower():
            return None
        key = (inspected.infohash, inspected.metadata_sha256)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and 0 <= now - cached[0] < _CACHE_SECONDS:
            return cached[1]
        root = _Decoder(torrent).parse()
        declarations = [root.get(b"announce")]
        tiers = root.get(b"announce-list")
        if isinstance(tiers, list):
            declarations.extend(value for tier in tiers if isinstance(tier, list) for value in tier)
        endpoints: dict[_Endpoint, bool] = {}
        unverified_declared = False
        for value in declarations:
            endpoint = _endpoint(value)
            if endpoint is None:
                unverified_declared |= value is not None
            elif endpoint in endpoints or len(endpoints) < _MAX_TRACKERS:
                endpoints[endpoint] = True
            else:
                unverified_declared = True
        fallbacks = () if root[b"info"].get(b"private") == 1 else self.fallback_trackers
        for value in fallbacks:
            if len(endpoints) == _MAX_TRACKERS:
                break
            endpoint = _endpoint(value)
            if endpoint is not None:
                endpoints.setdefault(endpoint, False)
        measurements = await asyncio.gather(
            *(self._bounded_measure(endpoint, inspected.infohash) for endpoint in endpoints)
        )
        positive = [count for count in measurements if count is not None and count > 0]
        declared_measurements = [
            count for declared, count in zip(endpoints.values(), measurements, strict=True)
            if declared
        ]
        result = max(positive) if positive else (
            0 if declared_measurements and not unverified_declared
            and all(count == 0 for count in declared_measurements) else None
        )
        if len(self._cache) >= _MAX_CACHE_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (now, result)
        return result
