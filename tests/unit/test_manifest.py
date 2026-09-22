import pytest

from homeserver_control.domain.torrent_manifest import ManifestError, parse_torrent_metadata


def test_manifest_rejects_absolute_and_parent_paths() -> None:
    with pytest.raises(ManifestError, match="path"):
        parse_torrent_metadata(
            {
                "infohash": "a" * 40,
                "files": [{"path": "/media/movie.mkv", "length": 1}],
            }
        )
    with pytest.raises(ManifestError, match="path"):
        parse_torrent_metadata(
            {
                "infohash": "a" * 40,
                "files": [{"path": "../movie.mkv", "length": 1}],
            }
        )


def test_manifest_rejects_duplicate_files_and_invalid_sizes() -> None:
    base = {"infohash": "b" * 40, "files": [{"path": "movie.mkv", "length": 2}]}
    with pytest.raises(ManifestError, match="duplicate"):
        parse_torrent_metadata(base | {"files": base["files"] * 2})
    with pytest.raises(ManifestError, match="length"):
        parse_torrent_metadata(
            {"infohash": "c" * 40, "files": [{"path": "movie.mkv", "length": -1}]}
        )


def test_manifest_returns_verified_infohash_and_files() -> None:
    manifest = parse_torrent_metadata(
        {
            "infohash": "d" * 40,
            "files": [
                {"path": "Season 01/Episode 01.mkv", "length": 5},
                {"path": "Season 01/Episode 02.mkv", "length": 6},
            ],
        }
    )
    assert manifest.infohash == "d" * 40
    assert [entry.path for entry in manifest.files] == [
        "Season 01/Episode 01.mkv",
        "Season 01/Episode 02.mkv",
    ]
