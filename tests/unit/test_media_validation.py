import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.worker.validation import ValidationError, validate_media


@pytest.mark.parametrize(
    ("width", "height", "accepted"),
    [
        (1920, 1080, True),
        (1920, 800, True),
        (1920, 799, True),
        (1918, 814, True),  # Pulp Fiction: narrow border crop.
        (1912, 1080, True),  # The Godfather Part III: narrow border crop.
        (1920, 792, True),  # Kill Bill: cinematic crop plus narrow border crop.
        (1904, 784, True),  # Inclusive 16-pixel tolerance on each default minimum.
        (1903, 1080, True),
        (1620, 1080, True),  # Native 3:2 frame: Obsession.
        (1440, 1080, True),  # Native 4:3 frame, without pillarbox padding.
        (1080, 1080, True),
        (1064, 1080, True),
        (1056, 1080, False),
        (1920, 783, True),
        (1800, 1080, True),
        (1920, 768, True),
        (1920, 720, True),
        (1920, 696, True),  # The Hateful Eight: 2.76:1 frame without black bars.
        (1918, 694, True),  # The same cinematic frame with a narrow border crop.
        (3840, 1392, True),  # A UHD cinematic crop remains above the FHD minimum.
        (640, 352, False),  # Mislabeled 1080p Lorax source.
        (1280, 720, False),
        (1280, 464, False),  # An actual 720p-class cinematic crop remains excluded.
    ],
)
def test_1080p_class_accepts_cinematic_crop(tmp_path, monkeypatch, width, height, accepted):
    movie = tmp_path / "movie.mp4"
    movie.write_bytes(b"fixture")
    monkeypatch.setattr(
        "homeserver_control.worker.validation.probe_media",
        lambda _path, **_kwargs: MediaProbe(width, height, ("eng",), (), {}),
    )

    if accepted:
        assert validate_media(movie, maximum_bytes=50_000_000_000).probe.width == width
    else:
        with pytest.raises(ValidationError, match="resolution"):
            validate_media(movie, maximum_bytes=50_000_000_000)


@pytest.mark.parametrize(
    ("width", "height", "minimums", "accepted"),
    [
        (1918, 814, {"minimum_width": 1920}, False),
        (1920, 792, {"minimum_height": 800}, False),
        (1918, 814, {"minimum_height": 800}, True),
        (1920, 792, {"minimum_width": 1920}, True),
        (1280, 720, {"minimum_width": 1280, "minimum_height": 720}, True),
        (1279, 720, {"minimum_width": 1280, "minimum_height": 720}, False),
        (3840, 2159, {"minimum_width": 3840, "minimum_height": 2160}, False),
        (1620, 1080, {"minimum_width": 1920}, False),
        (1620, 1080, {"minimum_height": 1080}, False),
    ],
)
def test_explicit_resolution_minimums_remain_strict(
    tmp_path, monkeypatch, width, height, minimums, accepted,
):
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"fixture")
    monkeypatch.setattr(
        "homeserver_control.worker.validation.probe_media",
        lambda _path, **_kwargs: MediaProbe(width, height, ("eng",), (), {}),
    )
    if accepted:
        assert validate_media(movie, maximum_bytes=100, **minimums).probe.height == height
    else:
        with pytest.raises(ValidationError, match="resolution"):
            validate_media(movie, maximum_bytes=100, **minimums)
