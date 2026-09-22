import re
from dataclasses import dataclass
from typing import Any


class ManifestError(ValueError):
    """The untrusted torrent metadata is not safe to admit."""


@dataclass(frozen=True)
class ManifestFile:
    path: str
    length: int


@dataclass(frozen=True)
class TorrentManifest:
    infohash: str
    files: tuple[ManifestFile, ...]

    @property
    def total_bytes(self) -> int:
        return sum(entry.length for entry in self.files)


def _validate_path(path: object) -> str:
    if not isinstance(path, str) or not path or "\\" in path:
        raise ManifestError("invalid path")
    normalized = path.replace("/", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ManifestError("invalid path")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ManifestError("invalid path")
    return normalized


def parse_torrent_metadata(metadata: dict[str, Any]) -> TorrentManifest:
    if not isinstance(metadata, dict):
        raise ManifestError("metadata must be an object")
    infohash = metadata.get("infohash")
    if not isinstance(infohash, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", infohash
    ):
        raise ManifestError("invalid infohash")
    if metadata.get("layout") == "compact" or metadata.get("private_links"):
        raise ManifestError("unsupported compact or linked metadata")
    raw_files = metadata.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ManifestError("files are required")
    files: list[ManifestFile] = []
    seen: set[str] = set()
    for raw in raw_files:
        if not isinstance(raw, dict):
            raise ManifestError("invalid file entry")
        path = _validate_path(raw.get("path"))
        length = raw.get("length")
        if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
            raise ManifestError("invalid file length")
        if path in seen:
            raise ManifestError("duplicate file path")
        if raw.get("symlink") or raw.get("hardlink"):
            raise ManifestError("links are not admitted")
        seen.add(path)
        files.append(ManifestFile(path=path, length=length))
    return TorrentManifest(infohash=infohash.lower(), files=tuple(files))
