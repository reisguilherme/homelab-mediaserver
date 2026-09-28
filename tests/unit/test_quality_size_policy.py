import pytest

from homeserver_common.env import load_settings
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.series_acquisition import _series_rank


def release(resolution=1080):
    return {
        "quality": {"quality": {"source": "webdl", "modifier": "none", "resolution": resolution}}
    }


def test_small_1080p_encode_is_rejected_for_full_length_episode():
    policy = ReleasePolicy()
    assert not policy.accepts_size(release(), video_bytes=400_000_000, runtime_minutes=45)
    assert policy.accepts_size(release(), video_bytes=1_800_000_000, runtime_minutes=45)


def test_runtime_scales_floor_instead_of_fixed_episode_size():
    policy = ReleasePolicy()
    assert policy.accepts_size(release(), video_bytes=400_000_000, runtime_minutes=10)
    assert not policy.accepts_size(release(), video_bytes=400_000_000, runtime_minutes=45)
    assert policy.accepts_size(release(2160), video_bytes=100_000_000_000, runtime_minutes=120)


@pytest.mark.parametrize("runtime", [None, 0, -1, True, "45", float("nan"), float("inf")])
def test_unknown_duration_cannot_prove_quality_floor(runtime):
    assert not ReleasePolicy().accepts_size(
        release(), video_bytes=1_800_000_000, runtime_minutes=runtime
    )


def test_canonical_env_controls_minimum_and_rejects_invalid_values(tmp_path):
    path = tmp_path / ".env"
    path.write_text("HOMESERVER_QUALITY_MIN_MIB_PER_MIN_1080=40\n")
    policy = ReleasePolicy.from_environment(load_settings(path, mode="dev").as_environment())
    assert not policy.accepts_size(release(), video_bytes=1_800_000_000, runtime_minutes=45)
    path.write_text("HOMESERVER_QUALITY_MIN_MIB_PER_MIN_1080=-1\n")
    with pytest.raises(ValueError, match="QUALITY_MIN_MIB_PER_MIN_1080"):
        load_settings(path, mode="dev")


def test_zero_explicitly_disables_floor_for_one_resolution():
    policy = ReleasePolicy.from_environment({"HOMESERVER_QUALITY_MIN_MIB_PER_MIN_1080": "0"})
    assert policy.accepts_size(release(), video_bytes=400_000_000, runtime_minutes=None)


def test_native_sonarr_bluray_raw_is_allowed_remux():
    offered = release()
    offered["title"] = "Fixture.S02E04.1080p.BluRay.REMUX"
    offered["quality"]["quality"].update(source="blurayRaw", name="Bluray-1080p Remux")
    assert _series_rank(offered, ReleasePolicy()) is not None
