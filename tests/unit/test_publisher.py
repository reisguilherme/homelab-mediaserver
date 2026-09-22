from datetime import UTC, datetime

import pytest

from homeserver_telemetry.publisher import SnapshotPublisher


def _snapshot() -> dict:
    return {
        "schema_version": 1,
        "sequence": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "sources": {},
        "host": {
            "cpu_percent": None,
            "ram_percent": None,
            "cpu_celsius": None,
            "uptime_seconds": None,
        },
        "capacity": {
            "total_bytes": 0,
            "free_bytes": 0,
            "reserved_unallocated_bytes": 0,
            "admissible_bytes": 0,
        },
        "transfers": {
            "download_bps": 0,
            "upload_bps": 0,
            "active_count": 0,
            "queue_count": 0,
        },
        "downloads": [],
        "playback": {"active_count": None, "remote_count": None, "items": []},
        "alerts": [],
    }


def test_publisher_restricts_topic_and_enforces_small_payload() -> None:
    published: list[tuple[str, bytes]] = []
    publisher = SnapshotPublisher(lambda topic, payload: published.append((topic, payload)))
    encoded = publisher.publish_snapshot("homeserver/v1/server/snapshot", _snapshot())
    assert published == [("homeserver/v1/server/snapshot", encoded)]
    with pytest.raises(ValueError, match="topic"):
        publisher.publish_snapshot("homeserver/v1/server/command", _snapshot())
