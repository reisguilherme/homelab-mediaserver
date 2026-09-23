from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient


def _write_snapshots(root: Path, *, generated_at: datetime) -> tuple[Path, Path]:
    host = root / "host.json"
    capacity = root / "capacity.json"
    host.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": generated_at.isoformat(),
                "host": {
                    "cpu_percent": 37.5,
                    "ram_percent": 62.0,
                    "cpu_celsius": 48.0,
                    "uptime_seconds": 3600,
                },
                "network": {"interface": "enxusb", "rx_bps": 2_000_000, "tx_bps": 250_000},
                "storage": {"total_bytes": 100_000, "used_bytes": 40_000},
            }
        ),
        encoding="utf-8",
    )
    capacity.write_text(
        json.dumps(
            {
                "filesystem_id": "test-volume",
                "total_bytes": 100_000,
                "free_bytes": 60_000,
                "measured_at": generated_at.timestamp(),
            }
        ),
        encoding="utf-8",
    )
    return host, capacity


def test_status_combines_recent_host_metrics_and_deduplicated_storage(tmp_path: Path) -> None:
    from homeserver_telemetry.status import StatusProvider

    media = tmp_path / "data"
    movies = media / "media" / "movies"
    series = media / "media" / "tv"
    torrents = media / "torrents"
    for path in (movies, series, torrents):
        path.mkdir(parents=True)
    movie = movies / "film.mkv"
    movie.write_bytes(b"a" * 2048)
    (series / "episode.mkv").write_bytes(b"b" * 2048)
    os.link(movie, torrents / "film.mkv")
    (torrents / "pending.mkv").write_bytes(b"c" * 2048)
    (torrents / "loop").symlink_to(media, target_is_directory=True)
    host, capacity = _write_snapshots(tmp_path, generated_at=datetime.now(UTC))
    provider = StatusProvider(host_path=host, capacity_path=capacity, media_root=media)

    first = provider()
    assert first["host"]["state"] == "ok"
    assert first["host"]["cpu_percent"] == 37.5
    assert first["network"]["rx_bps"] == 2_000_000
    assert first["capacity"]["used_bytes"] == 40_000
    assert first["storage"]["state"] == "calculating"

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = provider()
        if status["storage"]["state"] == "ok":
            break
        time.sleep(0.01)
    assert status["storage"]["state"] == "ok"
    expected_movie = movie.stat().st_blocks * 512
    expected_series = (series / "episode.mkv").stat().st_blocks * 512
    expected_torrent = (torrents / "pending.mkv").stat().st_blocks * 512
    assert status["storage"]["movies_bytes"] == expected_movie
    assert status["storage"]["series_bytes"] == expected_series
    assert status["storage"]["torrents_bytes"] == expected_torrent
    assert status["storage"]["other_bytes"] == (
        40_000 - expected_movie - expected_series - expected_torrent
    )


def test_status_marks_old_or_missing_snapshots_without_reporting_false_zeros(
    tmp_path: Path,
) -> None:
    from homeserver_telemetry.status import StatusProvider

    media = tmp_path / "data"
    media.mkdir()
    provider = StatusProvider(
        host_path=tmp_path / "host.json",
        capacity_path=tmp_path / "capacity.json",
        media_root=media,
    )
    missing = provider()
    assert missing["host"]["state"] == "unavailable"
    assert missing["host"]["cpu_percent"] is None
    assert missing["network"]["rx_bps"] is None
    assert missing["capacity"]["free_bytes"] is None

    _write_snapshots(tmp_path, generated_at=datetime.now(UTC) - timedelta(minutes=5))
    old = provider()
    assert old["host"]["state"] == "stale"
    assert old["host"]["cpu_percent"] == 37.5
    assert old["network"]["rx_bps"] == 2_000_000
    assert old["capacity"]["state"] == "stale"
    assert old["capacity"]["free_bytes"] == 60_000
    assert old["capacity"]["used_bytes"] is None
    assert old["storage"]["state"] == "unavailable"


def test_status_reports_real_used_blocks_instead_of_counting_reserved_space(
    tmp_path: Path,
) -> None:
    from homeserver_telemetry.status import StatusProvider

    host, capacity = _write_snapshots(tmp_path, generated_at=datetime.now(UTC))
    payload = json.loads(host.read_text(encoding="utf-8"))
    payload["storage"]["used_bytes"] = 35_000
    host.write_text(json.dumps(payload), encoding="utf-8")
    media = tmp_path / "data"
    media.mkdir()
    provider = StatusProvider(host_path=host, capacity_path=capacity, media_root=media)

    status = provider()
    assert status["capacity"]["total_bytes"] == 100_000
    assert status["capacity"]["free_bytes"] == 60_000
    assert status["capacity"]["used_bytes"] == 35_000


def test_status_hides_used_bytes_when_host_and_guarded_volume_differ(tmp_path: Path) -> None:
    from homeserver_telemetry.status import StatusProvider

    host, capacity = _write_snapshots(tmp_path, generated_at=datetime.now(UTC))
    payload = json.loads(host.read_text(encoding="utf-8"))
    payload["storage"]["total_bytes"] = 200_000
    host.write_text(json.dumps(payload), encoding="utf-8")
    media = tmp_path / "data"
    media.mkdir()
    status = StatusProvider(host_path=host, capacity_path=capacity, media_root=media)()

    assert status["capacity"]["used_bytes"] is None
    assert status["storage"]["state"] in {"calculating", "unavailable"}
    assert status["storage"]["other_bytes"] is None


def test_status_http_route_uses_host_files_without_aggregate_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    from homeserver_telemetry.app import create_app

    host, capacity = _write_snapshots(tmp_path, generated_at=datetime.now(UTC))
    media = tmp_path / "data"
    media.mkdir()
    monkeypatch.setenv("HOMESERVER_HOST_SNAPSHOT", str(host))
    monkeypatch.setenv("HOMESERVER_CAPACITY_SNAPSHOT", str(capacity))
    monkeypatch.setenv("HOMESERVER_MEDIA_ROOT", str(media))
    client = TestClient(create_app(snapshot_provider=lambda: {}))

    response = client.get("/api/v1/status")
    assert response.status_code == 200
    assert response.json()["host"]["cpu_percent"] == 37.5
    assert response.json()["capacity"]["free_bytes"] == 60_000


def test_storage_breakdown_hides_inconsistent_totals_until_capacity_catches_up(
    tmp_path: Path,
) -> None:
    from homeserver_telemetry.status import StatusProvider

    media = tmp_path / "data"
    movies = media / "media" / "movies"
    movies.mkdir(parents=True)
    (movies / "film.mkv").write_bytes(b"a" * 2048)
    host, capacity = _write_snapshots(tmp_path, generated_at=datetime.now(UTC))
    payload = json.loads(capacity.read_text(encoding="utf-8"))
    payload["free_bytes"] = 99_999
    capacity.write_text(json.dumps(payload), encoding="utf-8")
    host_payload = json.loads(host.read_text(encoding="utf-8"))
    host_payload["storage"]["used_bytes"] = 1
    host.write_text(json.dumps(host_payload), encoding="utf-8")
    provider = StatusProvider(host_path=host, capacity_path=capacity, media_root=media)

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        status = provider()
        if status["storage"]["state"] != "calculating":
            break
        time.sleep(0.01)
    assert status["capacity"]["used_bytes"] == 1
    assert status["storage"]["state"] == "unavailable"
    assert status["storage"]["movies_bytes"] is None
