"""Preview/confirmation primitives for destructive media operations."""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


class DeletionPlanError(ValueError):
    """Raised when a deletion preview or confirmation is unsafe."""


@dataclass(frozen=True)
class DeletionPreview:
    media_key: str
    paths: tuple[Path, ...]
    bytes_estimated: int
    version: str
    token: str
    expires_at: float


@dataclass(frozen=True)
class DeletionConfirmation:
    operation_id: str
    media_key: str
    paths: tuple[Path, ...]
    bytes_estimated: int


@dataclass(frozen=True)
class _FileSnapshot:
    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass
class _PendingPreview:
    preview: DeletionPreview
    snapshots: tuple[_FileSnapshot, ...]


class DeletionPlanner:
    """Builds expiring previews and verifies them immediately before mutation.

    The planner does not unlink files itself.  The caller must use the
    confirmed, revalidated path list in its transactional operation handler.
    """

    def __init__(self, *, roots: Iterable[str | Path], token_ttl_seconds: int = 300) -> None:
        if token_ttl_seconds <= 0:
            raise ValueError("token TTL must be positive")
        resolved_roots = tuple(Path(root).resolve(strict=True) for root in roots)
        if not resolved_roots or any(not root.is_dir() for root in resolved_roots):
            raise ValueError("deletion roots must be existing directories")
        self._roots = resolved_roots
        self._token_ttl_seconds = token_ttl_seconds
        self._pending: dict[str, _PendingPreview] = {}
        self._confirmed: dict[str, DeletionConfirmation] = {}

    def _resolve_safe_file(self, candidate: str | Path) -> Path:
        original = Path(candidate)
        cursor = original
        while cursor != cursor.parent:
            if cursor.is_symlink():
                raise DeletionPlanError(f"symlink is not allowed: {original}")
            cursor = cursor.parent
        try:
            resolved = original.resolve(strict=True)
        except FileNotFoundError as exc:
            raise DeletionPlanError(f"file does not exist: {original}") from exc
        if not resolved.is_file():
            raise DeletionPlanError(f"deletion target is not a regular file: {original}")
        if any(root == resolved or root in resolved.parents for root in self._roots):
            return resolved
        raise DeletionPlanError(f"path is outside configured deletion root: {original}")

    @staticmethod
    def _snapshot(path: Path) -> _FileSnapshot:
        stat = path.stat()
        return _FileSnapshot(
            path=path,
            device=stat.st_dev,
            inode=stat.st_ino,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )

    @staticmethod
    def _version(snapshots: tuple[_FileSnapshot, ...]) -> str:
        digest = hashlib.sha256()
        for snapshot in snapshots:
            digest.update(str(snapshot.path).encode("utf-8"))
            digest.update(
                f"{snapshot.device}:{snapshot.inode}:{snapshot.size}:{snapshot.mtime_ns}".encode(
                    "ascii"
                )
            )
        return digest.hexdigest()

    def preview(self, media_key: str, paths: Iterable[str | Path]) -> DeletionPreview:
        if not media_key.strip():
            raise DeletionPlanError("media key is required")
        candidates = tuple(paths)
        if not candidates:
            raise DeletionPlanError("at least one deletion path is required")
        resolved_list: list[Path] = []
        for candidate in candidates:
            resolved = self._resolve_safe_file(candidate)
            if resolved not in resolved_list:
                resolved_list.append(resolved)
        resolved_paths = tuple(resolved_list)
        snapshots = tuple(self._snapshot(path) for path in resolved_paths)
        version = self._version(snapshots)
        unique_payloads = {
            (snapshot.device, snapshot.inode): snapshot.size for snapshot in snapshots
        }
        token = secrets.token_urlsafe(24)
        expires_at = time.time() + self._token_ttl_seconds
        preview = DeletionPreview(
            media_key=media_key,
            paths=resolved_paths,
            bytes_estimated=sum(unique_payloads.values()),
            version=version,
            token=token,
            expires_at=expires_at,
        )
        self._pending[token] = _PendingPreview(preview=preview, snapshots=snapshots)
        return preview

    def confirm(self, token: str, version: str, operation_id: str) -> DeletionConfirmation:
        if not operation_id.strip():
            raise DeletionPlanError("operation id is required")
        previous = self._confirmed.get(operation_id)
        if previous is not None:
            return previous
        pending = self._pending.get(token)
        if pending is None:
            raise DeletionPlanError("unknown deletion confirmation token")
        if pending.preview.expires_at < time.time():
            self._pending.pop(token, None)
            raise DeletionPlanError("deletion confirmation token expired")
        if version != pending.preview.version:
            raise DeletionPlanError("deletion preview version changed")

        try:
            current = tuple(self._snapshot(path) for path in pending.preview.paths)
        except (FileNotFoundError, OSError) as exc:
            raise DeletionPlanError("deletion preview changed") from exc
        if current != pending.snapshots or self._version(current) != pending.preview.version:
            raise DeletionPlanError("deletion preview changed")

        confirmation = DeletionConfirmation(
            operation_id=operation_id,
            media_key=pending.preview.media_key,
            paths=pending.preview.paths,
            bytes_estimated=pending.preview.bytes_estimated,
        )
        self._confirmed[operation_id] = confirmation
        return confirmation
