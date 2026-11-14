"""Hybrid admission must preserve identity and reserve padding too."""
from hashlib import sha1, sha256

import pytest

from homeserver_control.domain.torrent_bytes import TorrentBytesError, inspect_torrent


def encode(value):
    if isinstance(value, int):
        return b'i' + str(value).encode() + b'e'
    if isinstance(value, bytes):
        return str(len(value)).encode() + b':' + value
    if isinstance(value, list):
        return b'l' + b''.join(encode(v) for v in value) + b'e'
    return b'd' + b''.join(encode(k) + encode(v) for k, v in sorted(value.items())) + b'e'


def hybrid(*, tree_length=222, attr=b'p'):
    return {b'file tree': {
        b'a.mkv': {b'': {b'length': 101, b'pieces root': b'a' * 32}},
        b'b.mkv': {b'': {b'length': tree_length, b'pieces root': b'b' * 32}},
    }, b'files': [
        {b'length': 101, b'path': [b'a.mkv']},
        {b'attr': attr, b'length': 16283, b'path': [b'.pad', b'16283']},
        {b'length': 222, b'path': [b'b.mkv']},
        {b'attr': b'p', b'length': 16162, b'path': [b'.pad', b'16162']},
    ], b'meta version': 2, b'name': b'pack', b'piece length': 16384,
        b'pieces': b'c' * 40}


def test_hybrid_preserves_exact_info_hash_and_counts_padding():
    info = encode(hybrid())
    descriptor = encode({b'announce': b'https://example.test/announce', b'info': hybrid()})
    inspected = inspect_torrent(descriptor)
    assert inspected.infohash == sha1(info).hexdigest()
    assert inspected.metadata_sha256 == sha256(descriptor).hexdigest()
    assert inspected.total_bytes == 32768
    assert inspected.padding_bytes == 32445
    assert inspected.content_bytes == 323
    assert [(f.path, f.length, f.padding) for f in inspected.files] == [
        ('pack/a.mkv', 101, False), ('pack/b.mkv', 222, False),
    ]
    assert [(f.path, f.length, f.padding) for f in inspected.padding_files] == [
        ('pack/.pad/16283', 16283, True), ('pack/.pad/16162', 16162, True),
    ]


@pytest.mark.parametrize('change', [
    'v2_only', 'tree_mismatch', 'symlink', 'bad_pieces',
    'future_version', 'unsafe_tree', 'bad_padding',
])
def test_hybrid_rejects_incomplete_or_conflicting_identity(change):
    value = hybrid()
    if change == 'v2_only':
        del value[b'files']
        del value[b'pieces']
    elif change == 'tree_mismatch':
        value[b'file tree'][b'b.mkv'][b''][b'length'] = 221
    elif change == 'symlink':
        value[b'files'][1][b'attr'] = b'l'
    elif change == 'bad_pieces':
        value[b'pieces'] = b'c' * 20
    elif change == 'future_version':
        value[b'meta version'] = 3
    elif change == 'unsafe_tree':
        value[b'file tree'][b'..'] = value[b'file tree'].pop(b'b.mkv')
    else:
        value[b'files'][1][b'length'] = 16282
        value[b'files'][-1][b'length'] += 1
    with pytest.raises(TorrentBytesError):
        inspect_torrent(encode({b'info': value}))


def test_hybrid_requires_v2_piece_layers_to_match_root():
    layer = b'a' * 32 + b'b' * 32
    root = sha256(layer).digest()
    value = {b'file tree': {b'a.mkv': {b'': {b'length': 20000, b'pieces root': root}}},
             b'files': [{b'length': 20000, b'path': [b'a.mkv']}], b'meta version': 2,
             b'name': b'pack', b'piece length': 16384, b'pieces': b'c' * 40}
    descriptor = encode({b'info': value, b'piece layers': {root: layer}})
    assert inspect_torrent(descriptor).total_bytes == 20000
    for layers in ({}, {root: layer[:-1]}, {root: b'c' * 64}):
        with pytest.raises(TorrentBytesError):
            inspect_torrent(encode({b'info': value, b'piece layers': layers}))


def test_hybrid_metadata_without_layers_remains_v1_compatible():
    value = {b'file tree': {b'a.mkv': {b'': {b'length': 20000, b'pieces root': b'a' * 32}}},
             b'files': [{b'length': 20000, b'path': [b'a.mkv']}], b'meta version': 2,
             b'name': b'pack', b'piece length': 16384, b'pieces': b'c' * 40}
    inspected = inspect_torrent(encode({b'info': value}))
    assert inspected.infohash == sha1(encode(value)).hexdigest()
    assert inspected.total_bytes == 20000
    assert inspected.hybrid is True
    assert inspected.v2_piece_layers_verified is False


def test_hybrid_repeated_virtual_padding_paths_are_not_content_duplicates():
    value = hybrid(tree_length=101)
    value[b'files'][2][b'length'] = 101
    value[b'files'][3][b'length'] = 16283
    value[b'files'][3][b'path'] = [b'.pad', b'16283']
    inspected = inspect_torrent(encode({b'info': value}))
    assert inspected.total_bytes == 32768
    assert len(inspected.files) == 2
    assert len(inspected.padding_files) == 2
