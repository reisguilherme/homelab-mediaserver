from homeserver_control.worker.release_quality import release_rank


def _release(*, source="bluray", modifier="remux", resolution=2160,
             title="Film 2160p BluRay REMUX DV Atmos", size=10_000,
             seeders=None):
    release = {
        "title": title, "size": size,
        "quality": {"quality": {
            "source": source, "modifier": modifier, "resolution": resolution,
        }},
    }
    if seeders is not None:
        release["seeders"] = seeders
    return release


def test_more_seeders_beat_larger_file_at_equal_quality() -> None:
    assert release_rank(_release(size=1_000, seeders=50)) > release_rank(
        _release(size=10_000, seeders=2)
    )


def test_remux_dolby_vision_and_atmos_still_beat_seed_count() -> None:
    remux = _release(source="bluray", modifier="remux", seeders=1)
    webdl = _release(source="webdl", modifier="none", seeders=100)
    assert release_rank(remux) > release_rank(webdl)
    assert release_rank(_release(seeders=1)) > release_rank(
        _release(title="Film 2160p BluRay REMUX", seeders=100)
    )


def test_invalid_seed_count_does_not_outweigh_known_zero() -> None:
    known_zero = release_rank(_release(seeders=0))
    for invalid in (True, -1, 1.5, "100", None):
        assert known_zero > release_rank(_release(seeders=invalid))
