"""Bounded, isolated libtorrent metadata exchange; never request media pieces.

upload_mode without auto_managed is the libtorrent documented metadata-only
mode. Zero file priorities and an impossible Linux save path also prevent empty
files. This session has no relationship with the admitted qBittorrent catalog.
"""

from __future__ import annotations

import asyncio
import sys
import time

from homeserver_control.domain.magnet import magnet_infohash
from homeserver_control.domain.torrent_bytes import (
    TorrentBytesError,
    inspect_torrent,
    merge_magnet_trackers,
)

_MAX_METADATA_BYTES = 16 * 1024 * 1024
_MAX_MAGNET_BYTES = 16 * 1024
_TIMEOUT_SECONDS = 45.0
_MAX_FILES = 4096


def _valid_magnet(magnet: str) -> bool:
    try:
        return (
            isinstance(magnet, str)
            and len(magnet.encode("utf-8")) <= _MAX_MAGNET_BYTES
            and magnet_infohash(magnet) is not None
        )
    except (ValueError, UnicodeError):
        return False


def _validated(torrent: bytes, magnet: str) -> bytes | None:
    try:
        if len(torrent) > _MAX_METADATA_BYTES:
            return None
        return merge_magnet_trackers(torrent, magnet)
    except (TorrentBytesError, ValueError, UnicodeError):
        return None


def _fetch_info(magnet: str, lt) -> bytes | None:
    """Child-only native session, with an injectable binding for offline tests."""
    if not _valid_magnet(magnet):
        return None
    session = handle = None
    try:
        params = lt.parse_magnet_uri(magnet)
        params.flags |= lt.torrent_flags.upload_mode
        params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
        params.file_priorities = [0] * _MAX_FILES
        params.save_path = "/dev/null/homeserver-metadata"
        params.url_seeds = []
        params.http_seeds = []
        params.max_connections = 20
        session = lt.session({
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_lsd": False,
            "connections_limit": 20,
            "max_metadata_size": _MAX_METADATA_BYTES,
            "listen_interfaces": "0.0.0.0:0",
        })
        handle = session.add_torrent(params)
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            status = handle.status()
            if status.total_payload_download != 0:
                return None
            if status.has_metadata:
                raw_info = bytes(handle.torrent_file().metadata())
                torrent = b"d4:info" + raw_info + b"e"
                if len(torrent) > _MAX_METADATA_BYTES:
                    return None
                inspected = inspect_torrent(torrent)
                return torrent if inspected.infohash == magnet_infohash(magnet) else None
            # Consume alerts so the native queue cannot grow throughout the deadline.
            session.pop_alerts()
            time.sleep(0.1)
    except Exception:
        # Native errors must not log tracker URLs or leave a reusable session.
        return None
    finally:
        if session is not None and handle is not None:
            session.remove_torrent(handle)
    return None


def _child_command() -> list[str]:
    return [sys.executable, "-m", __name__, "--child"]


async def _read_bounded(process: asyncio.subprocess.Process, magnet: str) -> bytes | None:
    process.stdin.write(magnet.encode("utf-8"))
    await process.stdin.drain()
    process.stdin.close()
    chunks = []
    size = 0
    while chunk := await process.stdout.read(64 * 1024):
        size += len(chunk)
        if size > _MAX_METADATA_BYTES:
            return None
        chunks.append(chunk)
    if await process.wait() != 0:
        return None
    return _validated(b"".join(chunks), magnet)


async def resolve_magnet_metadata(magnet: str) -> bytes | None:
    """Return validated v1 metadata or unknown, bounded to 45 seconds and 16 MiB."""
    if not _valid_magnet(magnet) or sys.platform != "linux":
        return None
    process = None
    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            process = await asyncio.create_subprocess_exec(
                *_child_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=64 * 1024,
            )
            return await _read_bounded(process, magnet)
    except (TimeoutError, OSError, ValueError):
        return None
    finally:
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await asyncio.shield(process.wait())


def _child() -> int:
    try:
        import libtorrent as lt

        encoded = sys.stdin.buffer.read(_MAX_MAGNET_BYTES + 1)
        if len(encoded) > _MAX_MAGNET_BYTES:
            return 1
        result = _fetch_info(encoded.decode("utf-8", "strict"), lt)
        if result is None:
            return 1
        sys.stdout.buffer.write(result)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(_child() if sys.argv[1:] == ["--child"] else 1)
