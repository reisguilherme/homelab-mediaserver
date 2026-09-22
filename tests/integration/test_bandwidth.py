from homeserver_telemetry.bandwidth import SeedBandwidthPolicy


def test_remote_playback_immediately_lowers_seed_limit() -> None:
    policy = SeedBandwidthPolicy()
    assert policy.update(remote_count=1, source_fresh=True, now=0) == 625_000


def test_limit_returns_to_idle_only_after_sixty_fresh_seconds() -> None:
    policy = SeedBandwidthPolicy()
    policy.update(remote_count=1, source_fresh=True, now=0)
    assert policy.update(remote_count=0, source_fresh=True, now=30) == 625_000
    assert policy.update(remote_count=0, source_fresh=True, now=60) == 2_500_000


def test_stale_sessions_keep_conservative_limit() -> None:
    policy = SeedBandwidthPolicy()
    policy.update(remote_count=1, source_fresh=True, now=0)
    assert policy.update(remote_count=0, source_fresh=False, now=600) == 625_000
