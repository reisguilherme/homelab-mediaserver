from hashlib import sha1

import pytest

from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent
from homeserver_control.domain.torrent_manifest import ManifestError, parse_torrent_metadata


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    return b"d" + b"".join(encode(key) + encode(item) for key, item in sorted(value.items())) + b"e"


def multifile(files, *, hybrid=False, tree=None):
    total = sum(file[b"length"] for file in files)
    info = {
        b"files": files,
        b"name": b"fixture",
        b"piece length": 16384,
        b"pieces": b"p" * (20 * max(0, (total + 16383) // 16384)),
    }
    if hybrid:
        info.update({b"meta version": 2, b"file tree": tree})
    return encode({b"info": info}), sha1(encode(info)).hexdigest()


def test_v1_retains_empty_auxiliary_and_original_identity():
    data, digest = multifile(
        [{b"length": 0, b"path": [b"aux.nfo"]}, {b"length": 123, b"path": [b"video.mkv"]}]
    )
    result = inspect_torrent(data)
    assert result.infohash == digest
    assert [(file.path, file.length) for file in result.files] == [
        ("fixture/aux.nfo", 0),
        ("fixture/video.mkv", 123),
    ]
    assert result.total_bytes == result.content_bytes == 123 and result.padding_bytes == 0


def test_hybrid_empty_auxiliary_has_no_merkle_root_and_same_v1_v2_files():
    data, digest = multifile(
        [{b"length": 0, b"path": [b"aux.nfo"]}, {b"length": 123, b"path": [b"video.mkv"]}],
        hybrid=True,
        tree={
            b"aux.nfo": {b"": {b"length": 0}},
            b"video.mkv": {b"": {b"length": 123, b"pieces root": b"r" * 32}},
        },
    )
    result = inspect_torrent(data)
    assert result.infohash == digest and result.hybrid
    assert result.v2_piece_layers_verified is True
    assert len(result.files) == 2 and result.total_bytes == 123


@pytest.mark.parametrize(
    "files",
    [
        [{b"length": -1, b"path": [b"aux.nfo"]}, {b"length": 123, b"path": [b"video.mkv"]}],
        [{b"length": 0, b"path": [b"aux.nfo"]}],
        [
            {b"length": 16384, b"path": [b"video.mkv"]},
            {b"attr": b"p", b"length": 0, b"path": [b".pad", b"0"]},
        ],
    ],
)
def test_negative_empty_total_and_empty_padding_stay_rejected(files):
    data, _ = multifile(files)
    with pytest.raises(TorrentBytesError):
        inspect_torrent(data)


@pytest.mark.parametrize(
    "zero_leaf",
    [
        {b"length": 0, b"pieces root": b"r" * 32},
        {b"length": -1},
        {b"length": 1, b"pieces root": b"r" * 32},
    ],
)
def test_hybrid_zero_leaf_invalid_root_negative_or_disagreement_rejected(zero_leaf):
    data, _ = multifile(
        [{b"length": 0, b"path": [b"aux.nfo"]}, {b"length": 123, b"path": [b"video.mkv"]}],
        hybrid=True,
        tree={
            b"aux.nfo": {b"": zero_leaf},
            b"video.mkv": {b"": {b"length": 123, b"pieces root": b"r" * 32}},
        },
    )
    with pytest.raises(TorrentBytesError):
        inspect_torrent(data)


def test_legal_metadata_does_not_approve_empty_required_video():
    data, _ = multifile(
        [{b"length": 0, b"path": [b"empty.mkv"]}, {b"length": 123, b"path": [b"video.mkv"]}]
    )
    result = inspect_torrent(data)
    with pytest.raises(ManifestError):
        parse_torrent_metadata(
            {
                "infohash": result.infohash,
                "files": [{"path": file.path, "length": file.length} for file in result.files],
            }
        )
