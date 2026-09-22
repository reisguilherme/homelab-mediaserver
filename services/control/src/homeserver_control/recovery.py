"""Shared fail-closed handling for isolated restore markers."""

from __future__ import annotations

from pathlib import Path


def recovery_mode_blocks(path: str | Path | None) -> bool:
    """Return whether a recovery marker requires admission to stay disabled.

    A missing marker means normal operation. Once a marker exists, malformed
    or unreadable content is treated as blocked; only an explicit
    ``admission_enabled=true`` value can clear the guard.
    """

    if path is None:
        return False
    if not str(path).strip():
        return False
    marker = Path(path)
    try:
        if not marker.exists():
            return False
        if marker.is_symlink():
            return True
        if not marker.is_file():
            return True
        values: dict[str, str] = {}
        for line in marker.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key.strip()] = value.strip().lower()
        return values.get("admission_enabled") != "true"
    except (OSError, UnicodeError):
        return True
