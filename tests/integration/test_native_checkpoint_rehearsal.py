"""Explicit opt-in: these tests start only fresh synthetic local Docker clones."""

import os

import pytest

from homeserver_control.native_rehearsal import run_native_rehearsal


@pytest.mark.skipif(
    os.environ.get("HOMESERVER_NATIVE_REHEARSAL") != "1",
    reason="explicit isolated Docker rehearsal opt-in required",
)
@pytest.mark.parametrize("scenario", ["success", "unexpected_restart"])
def test_native_checkpoint_rehearsal(scenario):
    report = run_native_rehearsal(scenario)
    assert report.resources_retained is False
    assert report.production_ready is False
    assert report.partial_restart_safe is False
    assert report.native_scan_baseline_verified
    if scenario == "success":
        assert report.completed
        assert report.checkpoint_restore_verified
        assert report.restored_application_state_verified
        assert not report.blocked_reasons
    else:
        assert not report.completed
        assert report.blocked_reasons == ("containment_changed",)
        assert not report.checkpoint_restore_verified
        assert report.unexpected_restart_rejected_before_capture
