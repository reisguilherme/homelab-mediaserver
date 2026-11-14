import pytest

from homeserver_common.settings import FIELDS
from homeserver_control.worker.release_quality import ReleasePolicy


def webdl():
    return {'quality': {'quality': {
        'source': 'webdl', 'modifier': 'none', 'resolution': 1080,
    }}}


@pytest.mark.parametrize('runtime', [104, None])
@pytest.mark.parametrize('policy', [ReleasePolicy(), ReleasePolicy.from_environment({})])
def test_default_policy_accepts_real_file_size_without_runtime_floor(runtime, policy):
    # The previous 20 MiB/min rule rejected this 2.06 GB file at 104 minutes.
    assert policy.accepts_size(webdl(), video_bytes=2_060_000_000, runtime_minutes=runtime)


def test_zero_floor_still_requires_positive_file_size_and_allowed_quality():
    policy = ReleasePolicy(minimum_mib_per_min=((1080, 0),))
    assert not policy.accepts_size(webdl(), video_bytes=0, runtime_minutes=104)
    assert not policy.accepts_size({}, video_bytes=2_060_000_000, runtime_minutes=104)


def test_explicit_floor_remains_opt_in_and_can_be_disabled():
    env = {'HOMESERVER_QUALITY_MIN_MIB_PER_MIN_1080': '20'}
    policy = ReleasePolicy.from_environment(env)
    assert not policy.accepts_size(webdl(), video_bytes=2_060_000_000, runtime_minutes=104)
    policy = ReleasePolicy.from_environment(env | {'HOMESERVER_QUALITY_MIN_MIB_PER_MIN_1080': '0'})
    assert policy.accepts_size(webdl(), video_bytes=2_060_000_000, runtime_minutes=104)


def test_preferences_default_to_no_size_floor_and_exclusive_audit():
    for resolution in (720, 1080, 2160):
        assert FIELDS[f'quality_min_mib_per_min_{resolution}'].default == 0
    assert FIELDS['audited_import_allow_seerr'].default is False
