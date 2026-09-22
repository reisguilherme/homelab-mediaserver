from homeserver_control.domain.models import EpisodeFile, ReleaseRef
from homeserver_control.domain.selection import select_season


def test_selection_covers_each_episode_without_exceeding_budget() -> None:
    releases = [
        ReleaseRef(
            release_id="large-4k",
            resolution=2160,
            language="original",
            files=tuple(EpisodeFile(f"s01e0{i}", f"4k-{i}.mkv", 40) for i in range(1, 3)),
        ),
        ReleaseRef(
            release_id="small-1080p",
            resolution=1080,
            language="pt-br",
            files=(EpisodeFile("s01e01", "1080p-1.mkv", 10),),
        ),
    ]
    plan = select_season(releases, episode_keys={"s01e01", "s01e02"}, max_work=100)
    assert plan.total_bytes == 50
    assert {file.episode_key for file in plan.files} == {"s01e01", "s01e02"}


def test_valid_existing_episode_is_not_replaced_as_an_upgrade() -> None:
    release = ReleaseRef(
        release_id="replacement",
        resolution=2160,
        language="original",
        files=(EpisodeFile("s01e01", "episode.mkv", 10),),
    )
    plan = select_season([release], episode_keys={"s01e01"}, existing_valid={"s01e01"})
    assert plan.files == ()
    assert plan.reason == "already_available"
