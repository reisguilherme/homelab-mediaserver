import pytest

from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.release_quality import ReleasePolicy


def _release(indexer, seeders, *, resolution=1080, inspectable=True):
    return {
        "title": "Film BluRay",
        "indexer": indexer,
        "seeders": seeders,
        "quality": {"quality": {"source": "bluray", "modifier": "none", "resolution": resolution}},
        "inspectable": inspectable,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uindex,other,expected",
    [
        (_release("UIndex", 20), _release("1337x", 100), "1337x"),
        (_release("UIndex", 100), _release("1337x", 20), "UIndex"),
        (_release("UIndex", 100), _release("1337x", 100), "UIndex"),
        (_release("UIndex", 20, resolution=2160), _release("1337x", 100), "UIndex"),
        (_release("UIndex", 20), _release("1337x", 100, inspectable=False), "UIndex"),
    ],
)
async def test_compare_both_indexers_and_keep_quality_and_metadata_gates(uindex, other, expected):
    acquirer = object.__new__(MovieAcquirer)
    acquirer.release_policy = ReleasePolicy()
    inspected = []

    async def eligible(releases):
        for release in releases:
            inspected.append(release["indexer"])
            if release["inspectable"]:
                yield release, ("hash", "digest", ("film.mkv",), 1000), None, b"metadata"

    candidates = acquirer._preferred_indexer_candidates(
        sorted([uindex, other], key=acquirer._rank, reverse=True),
        eligible=eligible,
        rank=acquirer._rank,
    )
    try:
        first = await anext(candidates)
        assert first[0]["indexer"] == expected
        assert inspected == ["UIndex", "1337x"]
    finally:
        await candidates.aclose()


def _audio_release(indexer, seeders, *, languages=("pt-BR", "en"), source="bluray"):
    release = _release(indexer, seeders)
    release["languages"] = list(languages)
    release["quality"]["quality"]["source"] = source
    return release


@pytest.mark.asyncio
@pytest.mark.parametrize(("profile", "priorities", "private", "public", "expected"), [
    ("Dublado", ("uindex", "1337x", "amigosshare"),
     _audio_release("Amigos Share Club", 5), _audio_release("UIndex", 100), "Amigos Share Club"),
    ("Standard", ("uindex", "1337x", "amigosshare"),
     _audio_release("Amigos Share Club", 5), _audio_release("UIndex", 100), "UIndex"),
    ("Dublado", ("uindex", "1337x"),
     _audio_release("Amigos Share Club", 100), _audio_release("UIndex", 5), "UIndex"),
    ("Dublado", ("uindex", "1337x", "amigosshare"),
     _audio_release("Amigos Share Club", 100, languages=("pt-BR",)),
     _audio_release("UIndex", 5), "UIndex"),
    ("Dublado", ("uindex", "1337x", "amigosshare"),
     _audio_release("Amigos Share Club", 100, languages=("en",)),
     _audio_release("UIndex", 5), "UIndex"),
    ("Dublado", ("uindex", "1337x", "amigosshare"),
     _audio_release("Amigos Share Club", 100, source="webdl"),
     _audio_release("UIndex", 5), "UIndex"),
    ("Dublado", ("uindex", "1337x", "amigosshare"),
     _audio_release("Amigos Share Club", 4), _audio_release("UIndex", 5), "UIndex"),
])
async def test_dubbed_asc_preference_follows_audio_quality_opt_in_and_seed_gates(
    profile, priorities, private, public, expected,
):
    acquirer = object.__new__(MovieAcquirer)
    acquirer.release_policy = ReleasePolicy(profile_name=profile, indexer_priority=priorities)

    def rank(release):
        return acquirer._rank(release, original_language="en")

    async def eligible(releases):
        for release in releases:
            if rank(release) is not None:
                yield release, ("hash", "digest", ("film.mkv",), 1000), None, b"metadata"

    offered = [item for item in (private, public) if rank(item) is not None]
    candidates = acquirer._preferred_indexer_candidates(
        sorted(offered, key=rank, reverse=True), eligible=eligible, rank=rank,
    )
    try:
        assert (await anext(candidates))[0]["indexer"] == expected
    finally:
        await candidates.aclose()
