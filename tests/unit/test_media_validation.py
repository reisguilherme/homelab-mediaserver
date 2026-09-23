import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.worker.validation import ValidationError, validate_media


@pytest.mark.parametrize(
    ("width", "height", "accepted"),
    [(1920, 1080, True), (1920, 800, True), (1920, 799, False), (1280, 720, False)],
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
