"""The private exporter exposes bounded metrics and never touches media or control state."""

import json
import os
import re
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient


def test_exporter_has_only_read_only_metrics_and_liveness_routes(tmp_path):
    from homeserver_telemetry.metrics import create_metrics_app

    app = create_metrics_app(snapshot_dir=tmp_path, clock=lambda: 100)
    client = TestClient(app)
    assert {(route.path, frozenset(route.methods)) for route in app.routes} == {
        ("/metrics", frozenset({"GET"})), ("/health/live", frozenset({"GET"})),
    }
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/health/live").json() == {"status": "ok"}


def test_environment_selects_only_the_dedicated_snapshot_directory(tmp_path, monkeypatch):
    from homeserver_telemetry.metrics import create_metrics_app

    (tmp_path / "host.json").write_text(json.dumps({
        "generated_at": 99, "host": {"cpu_percent": 42},
    }))
    monkeypatch.setenv("HOMESERVER_METRICS_SNAPSHOT_DIR", str(tmp_path))
    client = TestClient(create_metrics_app(clock=lambda: 100))
    assert "homeserver_host_cpu_percent 42" in client.get("/metrics").text


def test_labels_and_metrics_never_include_arbitrary_snapshot_metadata(tmp_path):
    from homeserver_telemetry.metrics import create_metrics_app

    secret = 'private-path-uuid-title-token-infohash"\nattacker 1'
    (tmp_path / "host.json").write_text(json.dumps({
        "generated_at": 100,
        "host": {"cpu_percent": 20, "hostname": secret},
        "network": {"interface": secret, "rx_bps": 10},
        "storage": {"path": secret},
        secret: secret,
    }))
    (tmp_path / "capacity.json").write_text(json.dumps({
        "measured_at": 100,
        "pools": [{
            "pool_id": name, "filesystem_id": secret + name, "label": secret,
            "reason": secret, "state": "ready", "measured_at": 100,
            "total_bytes": 100, "free_bytes": 40, "used_bytes": 60,
        } for name in ("ssd", "hdd", "hdd2", "hdd3")],
    }))
    client = TestClient(create_metrics_app(snapshot_dir=tmp_path, clock=lambda: 100))
    response = client.get("/metrics")
    assert "private-path" not in response.text
    assert "attacker" not in response.text
    assert set(re.findall(r"\{([^}]+)\}", response.text)) == {
        'source="host"', 'source="capacity"',
        'pool="ssd"', 'pool="hdd"', 'pool="hdd2"', 'pool="hdd3"',
    }
    rows = [line for line in response.text.splitlines() if line and not line.startswith("#")]
    assert len(rows) == len({line.split(" ")[0] for line in rows})


def test_scrape_reads_only_fixed_snapshots_without_media_or_database_io(tmp_path, monkeypatch):
    from homeserver_telemetry.metrics import create_metrics_app

    (tmp_path / "host.json").write_text(json.dumps({
        "generated_at": 100, "host": {"cpu_percent": 12},
        "storage": {"path": "/private-media/film.mkv"},
    }))
    (tmp_path / "capacity.json").write_text(json.dumps({"measured_at": 100, "pools": []}))
    client = TestClient(create_metrics_app(snapshot_dir=tmp_path, clock=lambda: 100))
    real_open = os.open

    def confined_open(path, *args, **kwargs):
        allowed = set(tmp_path.absolute().parts) if args[0] & os.O_DIRECTORY else {
            "host.json", "capacity.json",
        }
        assert os.fspath(path) in allowed
        assert args[0] & os.O_NOFOLLOW
        return real_open(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("exporter attempted media, environment, or database I/O")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", confined_open)
        patch.setattr(os, "scandir", forbidden)
        patch.setattr(os, "walk", forbidden)
        patch.setattr(os, "statvfs", forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        patch.setattr(Path, "read_text", forbidden)
        response = client.get("/metrics")
    assert response.status_code == 200
    assert "homeserver_host_cpu_percent 12" in response.text


def test_liveness_does_not_read_any_snapshot(tmp_path, monkeypatch):
    from homeserver_telemetry.metrics import create_metrics_app

    client = TestClient(create_metrics_app(snapshot_dir=tmp_path))

    def forbidden(*args, **kwargs):
        raise AssertionError("liveness must not depend on snapshot I/O")

    monkeypatch.setattr(os, "open", forbidden)
    assert client.get("/health/live").json() == {"status": "ok"}


def test_prometheus_families_are_contiguous_with_type_before_their_samples(tmp_path):
    from homeserver_telemetry.metrics import create_metrics_app

    text = TestClient(create_metrics_app(snapshot_dir=tmp_path)).get("/metrics").text
    finished = set()
    current = None
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            if current is not None:
                finished.add(current)
            current = line.split(" ")[2]
            assert line.endswith(" gauge")
        else:
            name = line.split("{", 1)[0].split(" ", 1)[0]
            assert name == current
            assert name not in finished
