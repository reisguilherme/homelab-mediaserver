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
