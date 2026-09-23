"""Inspect v1 BitTorrent metadata without fetching any payload."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha1, sha256

from .torrent_manifest import ManifestFile


class TorrentBytesError(ValueError):
    """Torrent metadata is malformed, unsafe, or outside the supported v1 subset."""


@dataclass(frozen=True)
class InspectedTorrent:
    infohash: str
    metadata_sha256: str
    files: tuple[ManifestFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(item.length for item in self.files)


class _Decoder:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def parse(self, depth: int = 0) -> object:
        if depth > 16 or self.offset >= len(self.data):
            raise TorrentBytesError("invalid bencoding depth or end")
        marker = self.data[self.offset : self.offset + 1]
        if marker == b"i":
            self.offset += 1
            end = self.data.find(b"e", self.offset)
            if end < 0:
                raise TorrentBytesError("unterminated integer")
            raw = self.data[self.offset : end]
            if not re.fullmatch(rb"0|-?[1-9][0-9]*", raw) or raw == b"-0":
                raise TorrentBytesError("invalid integer")
            self.offset = end + 1
            return int(raw)
        if marker in {b"l", b"d"}:
            self.offset += 1
            if marker == b"l":
                items = []
                while self.data[self.offset : self.offset + 1] != b"e":
                    items.append(self.parse(depth + 1))
                    if len(items) > 8192:
                        raise TorrentBytesError("too many list entries")
                self.offset += 1
                return items
            mapping: dict[bytes, object] = {}
            previous: bytes | None = None
            while self.data[self.offset : self.offset + 1] != b"e":
                key = self.parse(depth + 1)
                if not isinstance(key, bytes) or (previous is not None and key <= previous):
                    raise TorrentBytesError("invalid dictionary keys")
                previous = key
                mapping[key] = self.parse(depth + 1)
                if len(mapping) > 8192:
                    raise TorrentBytesError("too many dictionary entries")
            self.offset += 1
            return mapping
        colon = self.data.find(b":", self.offset, self.offset + 12)
        if colon < 0:
            raise TorrentBytesError("invalid byte string")
        raw_length = self.data[self.offset : colon]
        if not re.fullmatch(rb"0|[1-9][0-9]*", raw_length):
            raise TorrentBytesError("invalid byte string length")
        length = int(raw_length)
        self.offset = colon + 1
        if length > len(self.data) - self.offset:
            raise TorrentBytesError("truncated byte string")
        value = self.data[self.offset : self.offset + length]
        self.offset += length
        return value


def _safe_component(raw: object) -> str:
    if not isinstance(raw, bytes):
        raise TorrentBytesError("invalid path component")
    try:
        component = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise TorrentBytesError("path is not UTF-8") from error
    if (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or ":" in component
        or any(ord(char) < 32 for char in component)
    ):
        raise TorrentBytesError("unsafe torrent path")
    return component


def inspect_torrent(data: bytes) -> InspectedTorrent:
    if not isinstance(data, bytes) or not 1 <= len(data) <= 16 * 1024 * 1024:
        raise TorrentBytesError("invalid torrent metadata size")
    decoder = _Decoder(data)
    if data[:1] != b"d":
        raise TorrentBytesError("torrent root must be a dictionary")
    decoder.offset = 1
    root: dict[bytes, object] = {}
    info_start = info_end = None
    previous: bytes | None = None
    while data[decoder.offset : decoder.offset + 1] != b"e":
        key = decoder.parse(1)
        if not isinstance(key, bytes) or (previous is not None and key <= previous):
            raise TorrentBytesError("invalid root keys")
        previous = key
        start = decoder.offset
        root[key] = decoder.parse(1)
        if key == b"info":
            info_start, info_end = start, decoder.offset
    decoder.offset += 1
    if decoder.offset != len(data) or info_start is None or info_end is None:
        raise TorrentBytesError("invalid torrent root")
    info = root[b"info"]
    if not isinstance(info, dict) or b"meta version" in info or b"file tree" in info:
        raise TorrentBytesError("only v1 torrent metadata is supported")
    name = _safe_component(info.get(b"name"))
    piece_length = info.get(b"piece length")
    pieces = info.get(b"pieces")
    if not isinstance(piece_length, int) or piece_length <= 0 or not isinstance(pieces, bytes):
        raise TorrentBytesError("missing v1 pieces")
    files: list[ManifestFile] = []
    if b"files" in info:
        raw_files = info[b"files"]
        if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= 4096:
            raise TorrentBytesError("invalid torrent file list")
        for raw in raw_files:
            if not isinstance(raw, dict) or b"attr" in raw:
                raise TorrentBytesError("unsupported torrent file")
            components = raw.get(b"path")
            length = raw.get(b"length")
            if not isinstance(components, list) or not components:
                raise TorrentBytesError("invalid torrent path")
            if not isinstance(length, int) or length <= 0:
                raise TorrentBytesError("invalid torrent file length")
            path = "/".join([name, *(_safe_component(part) for part in components)])
            files.append(ManifestFile(path, length))
    else:
        length = info.get(b"length")
        if not isinstance(length, int) or length <= 0:
            raise TorrentBytesError("invalid torrent file length")
        files.append(ManifestFile(name, length))
    paths = [entry.path for entry in files]
    if len(paths) != len(set(paths)):
        raise TorrentBytesError("duplicate torrent file path")
    total = sum(entry.length for entry in files)
    if total > 100_000_000_000 or len(pieces) != ((total + piece_length - 1) // piece_length) * 20:
        raise TorrentBytesError("invalid torrent size or piece hashes")
    return InspectedTorrent(
        infohash=sha1(data[info_start:info_end]).hexdigest(),
        metadata_sha256=sha256(data).hexdigest(),
        files=tuple(files),
    )
