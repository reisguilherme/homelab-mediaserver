"""Filesystem identity shared by download admission and explicit media deletion."""

from pathlib import Path


def filesystem_identity(root: Path, configured: str | None = None) -> str | None:
    if configured:
        return configured
    try:
        return "device:" + str(root.stat().st_dev)
    except OSError:
        return None
