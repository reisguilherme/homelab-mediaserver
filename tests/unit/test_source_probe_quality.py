import pytest

from homeserver_control.worker.source_probe import canonical_quality


def _release(source, resolution, *, name="", modifier="none", title="Fixture"):
    return {
        "title": title,
        "quality": {
            "quality": {
                "source": source,
                "resolution": resolution,
                "name": name,
                "modifier": modifier,
            }
        },
    }


@pytest.mark.parametrize("resolution", [720, 1080, 2160])
def test_sonarr_raw_bluray_preserves_remux_source_floor(resolution):
    assert canonical_quality(
        _release("blurayRaw", resolution, name=f"Bluray-{resolution}p Remux")
    ) == (3, resolution, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "release,expected",
    [
        (_release("bluray", 1080, modifier="remux"), (3, 1080, 0, 0, 0, 0)),
        (_release("bluray", 2160, title="Fixture 2160p REMUX"), (3, 2160, 0, 0, 0, 0)),
        (_release("bluray", 1080), (2, 1080, 0, 0, 0, 0)),
        (_release("web", 1080, name="WEBDL-1080p"), (1, 1080, 0, 0, 0, 0)),
        (_release("web", 1080, name="WEBRip-1080p"), None),
        (_release("web", 1080, name="WEB-Unknown"), None),
        (_release("television", 1080), None),
        (_release("blurayRaw", "1080"), None),
        (_release("blurayRaw", True), None),
        (_release("blurayRaw", 480), None),
    ],
)
def test_canonical_quality_keeps_existing_source_and_resolution_rejections(release, expected):
    assert canonical_quality(release) == expected
