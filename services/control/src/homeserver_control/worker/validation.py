from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from homeserver_control.domain.media_probe import MediaProbe, MediaProbeError, probe_media


class ValidationError(ValueError):
    """A downloaded file is not eligible for import."""


@dataclass(frozen=True)
class ValidationResult:
    path: Path
    size_bytes: int
    probe: MediaProbe


def validate_size(path: str | Path, maximum_bytes: int) -> int:
    candidate = Path(path)
    if not candidate.is_file():
        raise ValidationError(f"media file does not exist: {candidate}")
    if maximum_bytes <= 0:
        raise ValidationError("media size limit must be positive")
    size = candidate.stat().st_size
    if size <= 0 or size > maximum_bytes:
        raise ValidationError(f"media size exceeds limit: {size} > {maximum_bytes}")
    return size


def validate_media(
    path: str | Path,
    *,
    maximum_bytes: int,
    minimum_width: int = 1920,
    minimum_height: int = 1080,
    ffprobe_binary: str | list[str] = "ffprobe",
) -> ValidationResult:
    candidate = Path(path)
    size = validate_size(candidate, maximum_bytes)
    try:
        result = probe_media(candidate, ffprobe_binary=ffprobe_binary)
    except MediaProbeError as error:
        raise ValidationError(str(error)) from error
    if result.width < minimum_width or result.height < minimum_height:
        raise ValidationError("video resolution is below the configured minimum")
    return ValidationResult(path=candidate, size_bytes=size, probe=result)
