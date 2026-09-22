import pytest

from homeserver_telemetry.models import TelemetrySnapshot, source_status


def test_old_success_is_reported_as_stale() -> None:
    assert source_status(last_success_age=31, last_error=None) == "stale"


def test_missing_source_is_unknown() -> None:
    assert source_status(last_success_age=None, last_error=None) == "unknown"


def test_recent_error_is_error_without_erasing_last_success_age() -> None:
    assert source_status(last_success_age=2, last_error="timeout") == "error"


def test_telemetry_snapshot_rejects_negative_progress() -> None:
    with pytest.raises(ValueError):
        TelemetrySnapshot.model_validate(
            {
                "schema_version": 1,
                "sequence": 1,
                "generated_at": "2026-09-21T15:00:00Z",
                "sources": {},
                "host": {
                    "cpu_percent": None,
                    "ram_percent": None,
                    "cpu_celsius": None,
                    "uptime_seconds": None,
                },
                "capacity": {
                    "total_bytes": 100,
                    "free_bytes": 50,
                    "reserved_unallocated_bytes": 0,
                    "admissible_bytes": 30,
                },
                "transfers": {
                    "download_bps": 0,
                    "upload_bps": 0,
                    "active_count": 0,
                    "queue_count": 0,
                },
                "downloads": [
                    {
                        "id": "job",
                        "title": "x",
                        "progress": 1.2,
                        "download_bps": 0,
                        "eta_seconds": None,
                        "state": "downloading",
                    }
                ],
                "playback": {"active_count": None, "remote_count": None, "items": []},
                "alerts": [],
            }
        )


def test_telemetry_snapshot_allows_unknown_nullable_metrics() -> None:
    snapshot = TelemetrySnapshot.model_validate(
        {
            "schema_version": 1,
            "sequence": 2,
            "generated_at": "2026-09-21T15:00:00Z",
            "sources": {"jellyfin": {"status": "unknown", "age_seconds": 35}},
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
            "transfers": {"download_bps": 0, "upload_bps": 0, "active_count": 0, "queue_count": 0},
            "downloads": [],
            "playback": {"active_count": None, "remote_count": None, "items": []},
            "alerts": [],
        }
    )
    assert snapshot.playback.remote_count is None
