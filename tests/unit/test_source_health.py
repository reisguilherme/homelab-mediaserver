from __future__ import annotations

import pytest

from homeserver_control.worker.source_health import SourceHealthStore, TorrentHealth


def _health(*, downloaded=0, left=100_000_000_000, seeds=0, speed=0,
            state="stalledDL", progress=0.0):
    return TorrentHealth(
        infohash="a" * 40, downloaded=downloaded, amount_left=left,
        num_seeds=seeds, dlspeed=speed, state=state, progress=progress,
    )


def test_zero_peer_stall_retries_after_five_minutes_and_survives_restart(tmp_path):
    path = tmp_path / "control.sqlite"
    store = SourceHealthStore(path)
    assert store.observe("permit-1", _health(), now=1000) is None
    assert SourceHealthStore(path).observe("permit-1", _health(), now=1299) is None
    assert SourceHealthStore(path).observe("permit-1", _health(), now=1300) == "stalled"


def test_progress_and_queue_pause_reset_stall_window(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite", slow_seconds=3600)
    assert store.observe("permit-1", _health(), now=1000) is None
    assert store.observe("permit-1", _health(downloaded=10, left=99_999_990_000), now=2700) is None
    assert store.observe("permit-1", _health(downloaded=10, left=99_999_990_000), now=2999) is None
    assert store.observe("permit-1", _health(downloaded=10, left=99_999_990_000,
                                              state="queuedDL"), now=4600) is None
    assert store.observe("permit-1", _health(downloaded=10, left=99_999_990_000), now=4700) is None
    assert store.observe(
        "permit-1", _health(downloaded=10, left=99_999_990_000), now=6500
    ) == "stalled"


def test_slow_progress_uses_hour_average_not_instantaneous_speed(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite", slow_seconds=3600)
    assert store.observe("permit-1", _health(seeds=2, state="downloading"), now=1000) is None
    assert store.observe("permit-1", _health(downloaded=2_000_000_000,
                                              left=98_000_000_000,
                                              seeds=2, state="downloading",
                                              speed=10_000_000), now=4599) is None
    assert store.observe("permit-1", _health(downloaded=2_000_000_000,
                                              left=98_000_000_000,
                                              seeds=2, state="downloading",
                                              speed=0), now=4600) == "slow"


def test_default_slow_window_is_five_minutes_and_uses_progress(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite")
    assert store.observe("p", _health(seeds=5, state="downloading"), now=1000) is None
    assert store.observe("p", _health(downloaded=29_900_000, left=99_970_100_000,
                                     seeds=5, speed=5_000_000, state="downloading"),
                         now=1299) is None
    assert store.observe("p", _health(downloaded=30_000_000, left=99_970_000_000,
                                     seeds=5, speed=5_000_000, state="downloading"),
                         now=1300) == "slow"


def test_disabled_slow_replacement_keeps_progressing_source_but_recovers_dead_swarm(tmp_path):
    path = tmp_path / "control.sqlite"
    store = SourceHealthStore(path, slow_replacement_enabled=False)
    assert store.observe("slow", _health(seeds=5, state="downloading"), now=1000) is None
    assert SourceHealthStore(path, slow_replacement_enabled=False).observe(
        "slow", _health(downloaded=30_000_000, left=99_970_000_000,
                        seeds=5, speed=100_000, state="downloading"), now=1300,
    ) is None
    assert store.observe("dead", _health(), now=1000) is None
    assert store.observe("dead", _health(), now=1300) == "stalled"


def test_disabled_slow_replacement_rejects_trial_of_progressing_source(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite", slow_replacement_enabled=False)
    old = _health(seeds=5, speed=100_000, state="downloading")
    new = _health(downloaded=1, left=90_000_000_000, seeds=10,
                  speed=5_000_000, state="downloading")
    assert store.probe_decision("old", "trial", old, new, now=1000) == "reject"


@pytest.mark.parametrize("state", ["downloading", "stalledDL", "metaDL", "forcedMetaDL"])
def test_probe_without_payload_expires_after_active_startup_grace(tmp_path, state):
    path = tmp_path / "control.sqlite"
    old, new = _health(), _health(state=state)
    store = SourceHealthStore(path, slow_replacement_enabled=False, stalled_seconds=300)
    assert store.probe_decision("old", "trial", old, new, now=1000) == "observing"
    assert store.probe_decision("old", "trial", old, new, now=1299) == "observing"
    restarted = SourceHealthStore(path, slow_replacement_enabled=False, stalled_seconds=300)
    assert restarted.probe_decision("old", "trial", old, new, now=1300) == "reject"


@pytest.mark.parametrize("state", ["queuedDL", "pausedDL", "stoppedDL"])
def test_probe_queue_or_pause_does_not_consume_active_startup_grace(tmp_path, state):
    store = SourceHealthStore(tmp_path / "control.sqlite", stalled_seconds=300)
    old, active, waiting = _health(), _health(), _health(state=state)
    assert store.probe_decision("old", "trial", old, active, now=1000) == "observing"
    assert store.probe_decision("old", "trial", old, waiting, now=1299) == "observing"
    assert store.probe_decision("old", "trial", old, waiting, now=2000) == "observing"
    assert store.probe_decision("old", "trial", old, active, now=2100) == "observing"
    assert store.probe_decision("old", "trial", old, active, now=2399) == "observing"
    assert store.probe_decision("old", "trial", old, active, now=2400) == "reject"


def test_probe_startup_grace_allows_progress_but_retains_recovered_original(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite", slow_replacement_enabled=False)
    old = _health()
    assert store.probe_decision("old", "trial", old, _health(), now=1000) == "observing"
    moving = _health(downloaded=1000, left=99_999_999_000, speed=100, state="downloading")
    assert store.probe_decision("old", "trial", old, moving, now=1010) == "observing"
    assert store.probe_decision("old", "trial", _health(seeds=1), moving, now=1011) == "reject"


def test_probe_startup_uses_configured_stall_limit_and_reject_is_durable(tmp_path):
    path = tmp_path / "control.sqlite"
    store = SourceHealthStore(path, stalled_seconds=45)
    assert store.probe_decision("old", "trial", _health(), _health(), now=1000) == "observing"
    assert store.probe_decision("old", "trial", _health(), _health(), now=1044) == "observing"
    assert store.probe_decision("old", "trial", _health(), _health(), now=1045) == "reject"
    assert SourceHealthStore(path).probe_decision(
        "old", "trial", _health(), _health(), now=1400,
    ) == "reject"


def test_probe_payload_progress_during_startup_can_promote_after_measured_window(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite", slow_replacement_enabled=False)
    assert store.probe_decision("old", "trial", _health(), _health(), now=1000) == "observing"
    moving = _health(downloaded=1000, left=99_999_999_000, speed=100, state="downloading")
    assert store.probe_decision("old", "trial", _health(), moving, now=1100) == "promote"


def test_fast_progress_and_completed_torrent_never_trigger_failover(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite")
    assert store.observe("permit-1", _health(seeds=3, state="downloading"), now=1000) is None
    assert store.observe("permit-1", _health(downloaded=8_000_000_000,
                                              left=92_000_000_000,
                                              seeds=3, state="downloading"), now=4600) is None
    assert store.observe(
        "permit-1", _health(downloaded=8_000_000_000, left=0,
                            progress=1, state="uploading"), now=10000
    ) is None


def test_replacement_intent_survives_stop_but_can_be_cleared(tmp_path):
    path = tmp_path / "control.sqlite"
    store = SourceHealthStore(path)
    store.observe("permit-1", _health(), now=1000)
    store.mark_replacing("permit-1")
    assert SourceHealthStore(path).observe(
        "permit-1", _health(state="stoppedDL", left=99_999_999_000), now=1010
    ) == "replacing"
    store.clear_replacing("permit-1")
    assert store.observe("permit-1", _health(state="stoppedDL"), now=1020) is None


def test_retransmitted_bytes_do_not_hide_a_stalled_download(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite")
    assert store.observe("permit-1", _health(), now=1000) is None
    assert store.observe("permit-1", _health(downloaded=30_000_000), now=2800) == "stalled"


@pytest.mark.parametrize("payload", [
    {"hash": "a" * 40, "downloaded": True, "amount_left": 4,
     "num_seeds": 0, "dlspeed": 0, "state": "stalledDL", "progress": 0.2},
    {"hash": "a" * 40, "downloaded": 4, "amount_left": -1,
     "num_seeds": 0, "dlspeed": 0, "state": "stalledDL", "progress": 0.2},
])
def test_health_rejects_invalid_gateway_evidence(payload):
    with pytest.raises(ValueError):
        TorrentHealth.from_mapping(payload)
