from __future__ import annotations

from urllib.parse import urlencode

import pytest

from homeserver_control.domain import torrent_bytes
from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent

_INFO = b"d6:lengthi123e4:name8:test.mp412:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"e"


def _encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    return b"l" + b"".join(_encode(item) for item in value) + b"e"


def _torrent(fields=None):
    values = {b"comment": _encode(b"Preserve the cached metadata"), b"info": _INFO}
    values.update({key: _encode(value) for key, value in (fields or {}).items()})
    return b"d" + b"".join(_encode(key) + value for key, value in sorted(values.items())) + b"e"


def _magnet(torrent, trackers=(), *, infohash=None):
    return "magnet:?" + urlencode([
        ("xt", f"urn:btih:{infohash or inspect_torrent(torrent).infohash}"),
        *(("tr", item) for item in trackers),
    ])


def test_merge_preserves_info_bytes_files_and_other_metadata():
    torrent = _torrent()
    trackers = ["udp://tracker.example:1337/announce", "https://tracker.example/announce"]
    merged = torrent_bytes.merge_magnet_trackers(torrent, _magnet(torrent, trackers))

    assert merged == _torrent({
        b"announce": trackers[0].encode(),
        b"announce-list": [[item.encode()] for item in trackers],
    })
    assert b"4:info" + _INFO in merged
    original, updated = inspect_torrent(torrent), inspect_torrent(merged)
    assert updated.infohash == original.infohash
    assert updated.files == original.files
    assert updated.total_bytes == original.total_bytes
    assert updated.metadata_sha256 != original.metadata_sha256


def test_merge_keeps_existing_tiers_and_deduplicates_magnet_trackers():
    first = b"https://first.example/announce"
    second = b"udp://second.example:80/announce"
    fallback = b"http://fallback.example/announce"
    new = b"udp://new.example:1337/announce"
    torrent = _torrent({
        b"announce": first,
        b"announce-list": [[first, second], [fallback]],
    })
    magnet = _magnet(torrent, [second.decode(), new.decode(), new.decode()])

    assert torrent_bytes.merge_magnet_trackers(torrent, magnet) == _torrent({
        b"announce": first,
        b"announce-list": [[first, second], [fallback], [new]],
    })


def test_merge_retains_single_announce_when_creating_announce_list():
    first = b"https://first.example/announce"
    new = b"udp://new.example:1337/announce"
    torrent = _torrent({b"announce": first})

    assert torrent_bytes.merge_magnet_trackers(torrent, _magnet(torrent, [new.decode()])) == (
        _torrent({b"announce": first, b"announce-list": [[first], [new]]})
    )


@pytest.mark.parametrize("trackers", [
    [],
    ["file:///tmp/tracker"],
    ["ftp://tracker.example/announce"],
    ["https://user:password@tracker.example/announce"],
    ["https://tracker.example/announce#fragment"],
    ["udp://tracker.example:65536/announce"],
    ["udp://tracker.example:0/announce"],
    ["udp://tracker.example/announce"],
    ["https:///announce"],
    ["https://tracker.example/ann ounce"],
    ["https://tracker.example/announce\r\nHeader:value"],
    ["https://tracker.example/announce\x00"],
    ["https://tracker.example\\other/announce"],
    ["https://tracker.example/" + "a" * 2048],
])
def test_merge_without_usable_trackers_returns_original_bytes(trackers):
    torrent = _torrent()
    assert torrent_bytes.merge_magnet_trackers(torrent, _magnet(torrent, trackers)) == torrent


def test_merge_with_only_existing_trackers_is_idempotent():
    tracker = b"udp://tracker.example:1337/announce"
    torrent = _torrent({b"announce": tracker})
    magnet = _magnet(torrent, [tracker.decode(), tracker.decode()])
    assert torrent_bytes.merge_magnet_trackers(torrent, magnet) == torrent


def test_merge_bounds_new_trackers_without_removing_existing_ones():
    old = b"http://old.example/announce"
    torrent = _torrent({b"announce": old})
    trackers = [f"udp://tracker{i}.example:1337/announce" for i in range(70)]
    merged = torrent_bytes.merge_magnet_trackers(torrent, _magnet(torrent, trackers))

    assert merged == _torrent({
        b"announce": old,
        b"announce-list": [[old], *[[item.encode()] for item in trackers[:64]]],
    })


def test_merge_rejects_magnet_for_different_content():
    torrent = _torrent()
    magnet = _magnet(torrent, ["udp://tracker.example:1337/announce"], infohash="0" * 40)
    with pytest.raises(TorrentBytesError, match="infohash"):
        torrent_bytes.merge_magnet_trackers(torrent, magnet)


@pytest.mark.parametrize("magnet", [
    "https://tracker.example/announce", "magnet:?xt=invalid", "magnet:?xt=urn:btih:" + "0" * 40,
])
def test_merge_rejects_invalid_magnet_or_mismatched_hash(magnet):
    with pytest.raises(TorrentBytesError):
        torrent_bytes.merge_magnet_trackers(_torrent(), magnet)


def test_merge_rejects_malformed_cached_torrent():
    valid = _torrent()
    with pytest.raises(TorrentBytesError):
        torrent_bytes.merge_magnet_trackers(b"invalid", _magnet(valid))
