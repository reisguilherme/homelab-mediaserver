from __future__ import annotations

import pytest

from homeserver_control.worker.source_health import SourceHealthStore, TorrentHealth


def _health(*, downloaded=0, left=100_000_000_000, seeds=0, speed=0,
            state="stalledDL", progress=0.0):
    return TorrentHealth(
        infohash="a" * 40, downloaded=downloaded, amount_left=left,
        num_seeds=seeds, dlspeed=speed, state=state, progress=progress,
    )


def test_zero_peer_stall_requires_30_minutes_without_progress_and_survives_restart(tmp_path):
    path = tmp_path / "control.sqlite"
    store = SourceHealthStore(path)
    assert store.observe("permit-1", _health(), now=1000) is None
    assert SourceHealthStore(path).observe("permit-1", _health(), now=2799) is None
    assert SourceHealthStore(path).observe("permit-1", _health(), now=2800) == "stalled"


def test_progress_and_queue_pause_reset_stall_window(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite")
    assert store.observe("permit-1", _health(), now=1000) is None
    assert store.observe("permit-1", _health(downloaded=10, left=99_999_990_000), now=2700) is None
    assert store.observe("permit-1", _health(downloaded=10, left=99_999_990_000), now=4400) is None
    assert store.observe("permit-1", _health(downloaded=10, left=99_999_990_000,
                                              state="queuedDL"), now=4600) is None
    assert store.observe("permit-1", _health(downloaded=10, left=99_999_990_000), now=4700) is None
    assert store.observe(
        "permit-1", _health(downloaded=10, left=99_999_990_000), now=6500
    ) == "stalled"


def test_slow_progress_uses_hour_average_not_instantaneous_speed(tmp_path):
    store = SourceHealthStore(tmp_path / "control.sqlite")
    assert store.observe("permit-1", _health(seeds=2, state="downloading"), now=1000) is None
    assert store.observe("permit-1", _health(downloaded=2_000_000_000,
                                              left=98_000_000_000,
                                              seeds=2, state="downloading",
                                              speed=10_000_000), now=4599) is None
    assert store.observe("permit-1", _health(downloaded=2_000_000_000,
                                              left=98_000_000_000,
                                              seeds=2, state="downloading",
                                              speed=0), now=4600) == "slow"


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
