"""A replacement may ignore only Radarr's existing-queue veto."""

import pytest

from homeserver_control.worker.acquisition import _queue_only_rejection


@pytest.mark.parametrize(
    "reason",
    [
        "Release in queue already meets cutoff: WEBDL-2160p v1",
        "Quality for release in queue already meets cutoff: WEBDL-2160p v1",
    ],
)
def test_queue_cutoff_is_the_only_ignorable_radarr_rejection(reason: str) -> None:
    assert _queue_only_rejection({"rejected": True, "rejections": [reason]})
    assert not _queue_only_rejection(
        {"rejected": True, "rejections": [reason, "Not wanted in quality profile"]}
    )
    assert not _queue_only_rejection({"rejected": False, "rejections": [reason]})
