from hashlib import sha1

import pytest

from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent


def _torrent(info: bytes) -> bytes:
    return b"d4:info" + info + b"e"


def test_inspect_single_file_v1_torrent() -> None:
    info = b"d6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"e"
    inspected = inspect_torrent(_torrent(info))
    assert inspected.infohash == sha1(info).hexdigest()
    assert [(item.path, item.length) for item in inspected.files] == [("test.mp4", 123)]
    assert inspected.total_bytes == 123


def test_large_v1_torrent_is_allowed_when_piece_hashes_match() -> None:
    size = 120_000_000_000
    piece_length = 1_073_741_824
    hashes = b"a" * (20 * ((size + piece_length - 1) // piece_length))
    info = (b"d6:lengthi" + str(size).encode() + b"e4:name8:test.mp4"
            + b"12:piece lengthi" + str(piece_length).encode() + b"e6:pieces"
            + str(len(hashes)).encode() + b":" + hashes + b"e")
    assert inspect_torrent(_torrent(info)).total_bytes == size


@pytest.mark.parametrize(
    "info",
    [
        b"d6:lengthi123e4:name9:../escape12:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"e",
        b"d6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:pieces20:"
        + b"a" * 20
        + b"12:meta versioni2ee",
        b"d6:lengthi-1e4:name8:test.mp412:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"e",
    ],
)
def test_reject_unsafe_or_unsupported_torrent(info: bytes) -> None:
    with pytest.raises(TorrentBytesError):
        inspect_torrent(_torrent(info))
