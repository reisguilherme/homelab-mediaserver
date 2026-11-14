from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from homeserver_control.domain.media_probe import MediaProbe, MediaProbeError, probe_media

_DEFAULT_CROP_TOLERANCE_PIXELS = 16


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
    minimum_width: int | None = None,
    minimum_height: int | None = None,
    ffprobe_binary: str | list[str] = "ffprobe",
) -> ValidationResult:
    """Validate 1080p-class media, allowing border crops by default.

    Full-height frames (including 4:3 and square) need no 1920-pixel padding.
    Cinematic frames retain their horizontal resolution when black bars are
    removed. Default dimensions allow 16 pixels of border crop; explicit
    minimums remain strict on their own axis.
    """
    candidate = Path(path)
    size = validate_size(candidate, maximum_bytes)
    try:
        result = probe_media(candidate, ffprobe_binary=ffprobe_binary)
    except MediaProbeError as error:
        raise ValidationError(str(error)) from error
    width_floor = 1920 - _DEFAULT_CROP_TOLERANCE_PIXELS if minimum_width is None else minimum_width
    height_floor = (
        800 - _DEFAULT_CROP_TOLERANCE_PIXELS if minimum_height is None else minimum_height
    )
    default_dimensions = minimum_width is None and minimum_height is None
    nominal_full_hd = default_dimensions and (
        result.width >= width_floor
        or min(result.width, result.height) >= 1080 - _DEFAULT_CROP_TOLERANCE_PIXELS
    )
    if not nominal_full_hd and (result.width < width_floor or result.height < height_floor):
        raise ValidationError("video resolution is below the configured minimum")
    return ValidationResult(path=candidate, size_bytes=size, probe=result)
