"""Sanitized publication reuses measured evidence and never widens filesystem access."""

import importlib.util
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient


def source_payloads():
    return {
        "generated_at": "2027-01-15T07:59:50+00:00",
        "host": {"cpu_percent": 23, "ram_percent": 40, "cpu_celsius": 51,
                 "uptime_seconds": 123, "hostname": "secret-host"},
        "network": {"rx_bps": 500, "tx_bps": 200, "interface": "secret-interface"},
        "storage": {"path": "secret-path", "filesystem_id": "secret-uuid"},
        "counters": {"rx_bytes": 123}, "pid": 456,
    }, {
        "measured_at": 1_799_999_995,
        "filesystem_id": "secret-uuid", "reason": "secret-error",
        "pools": [{
            "pool_id": "ssd", "filesystem_id": "secret-uuid", "state": "ready",
            "measured_at": 1_799_999_995, "total_bytes": 1000,
            "free_bytes": 600, "used_bytes": 300, "label": "secret-label",
            "path": "secret-path", "reason": "secret-error",
        }],
    }


def publish(directory, host=None, capacity=None):
    from homeserver_common.metrics_snapshot import publish_metrics_snapshots

    default_host, default_capacity = source_payloads()
    return publish_metrics_snapshots(
        directory,
        host=default_host if host is None else host,
        capacity=default_capacity if capacity is None else capacity,
    )


def test_projection_contains_only_metric_fields_and_preserves_measurement_times(tmp_path):
    host, capacity = source_payloads()
    assert publish(tmp_path, host, capacity) is True
    actual_host = json.loads((tmp_path / "host.json").read_text())
    actual_capacity = json.loads((tmp_path / "capacity.json").read_text())

    assert actual_host == {
        "generated_at": "2027-01-15T07:59:50+00:00",
        "host": {"cpu_percent": 23, "ram_percent": 40, "cpu_celsius": 51, "uptime_seconds": 123},
        "network": {"rx_bps": 500, "tx_bps": 200},
    }
    assert set(actual_capacity) == {"measured_at", "pools"}
    assert actual_capacity["measured_at"] == 1_799_999_995
    pool = actual_capacity["pools"][0]
    assert set(pool) == {
        "pool_id", "filesystem_id", "state", "measured_at",
        "total_bytes", "free_bytes", "used_bytes",
    }
    assert pool["measured_at"] == 1_799_999_995
    assert pool["filesystem_id"] != "secret-uuid"
    assert len(pool["filesystem_id"]) == 71  # sha256: plus a 64-character digest.
    assert "secret-" not in (tmp_path / "host.json").read_text()
    assert "secret-" not in (tmp_path / "capacity.json").read_text()

    assert publish(tmp_path, host, capacity) is True
    assert json.loads((tmp_path / "capacity.json").read_text())["pools"][0] == pool
    capacity["pools"][0]["filesystem_id"] = "different-uuid"
    assert publish(tmp_path, host, capacity) is True
    changed = json.loads((tmp_path / "capacity.json").read_text())["pools"][0]
    assert changed["filesystem_id"] != pool["filesystem_id"]


def test_files_are_atomically_replaced_with_restricted_permissions_and_no_temp_files(tmp_path):
    (tmp_path / "host.json").write_text('{"old": true}')
    old_inode = (tmp_path / "host.json").stat().st_ino
    old_umask = os.umask(0o077)
    try:
        with (tmp_path / "host.json").open() as previous:
            assert publish(tmp_path) is True
            assert json.load(previous) == {"old": True}
    finally:
        os.umask(old_umask)
    assert (tmp_path / "host.json").stat().st_ino != old_inode
    assert {path.name for path in tmp_path.iterdir()} == {"host.json", "capacity.json"}
    for name in ("host.json", "capacity.json"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o640


@pytest.mark.parametrize("kind", ["missing", "file", "symlink", "ancestor-symlink"])
def test_destination_must_already_exist_without_any_symlink_component(tmp_path, kind, capsys):
    destination = tmp_path / "destination"
    if kind == "file":
        destination.write_text("private-content")
    elif kind in ("symlink", "ancestor-symlink"):
        actual = tmp_path / "actual"
        actual.mkdir()
        destination.symlink_to(actual, target_is_directory=True)
        if kind == "ancestor-symlink":
            (actual / "snapshots").mkdir()
            destination /= "snapshots"
    assert publish(destination) is False
    assert capsys.readouterr().err == "metrics snapshot publication failed\n"
    if kind == "missing":
        assert not destination.exists()
    assert not list(tmp_path.rglob("host.json"))


def test_existing_symlink_target_is_not_followed_or_overwritten(tmp_path):
    secret = tmp_path / "private.json"
    secret.write_text("private-content")
    (tmp_path / "host.json").symlink_to(secret)
    assert publish(tmp_path) is False
    assert secret.read_text() == "private-content"
    assert (tmp_path / "host.json").is_symlink()


def test_failed_replace_retains_previous_snapshot_and_removes_temporary_file(tmp_path, monkeypatch):
    (tmp_path / "host.json").write_text('{"old": true}')

    def fail_replace(*args, **kwargs):
        raise OSError("private-path-error")

    monkeypatch.setattr(os, "replace", fail_replace)
    assert publish(tmp_path) is False
    assert json.loads((tmp_path / "host.json").read_text()) == {"old": True}
    assert {path.name for path in tmp_path.iterdir()} == {"host.json"}


@pytest.mark.parametrize("value", [True, "secret-value", float("nan"), float("inf"), {"secret": 1}])
def test_invalid_scalars_are_unknown_and_cannot_copy_free_text(tmp_path, value):
    host, capacity = source_payloads()
    host["generated_at"] = value
    host["host"]["cpu_percent"] = value
    host["network"]["rx_bps"] = value
    capacity["measured_at"] = value
    capacity["pools"][0]["free_bytes"] = value
    assert publish(tmp_path, host, capacity) is True
    output = json.loads((tmp_path / "host.json").read_text())
    assert output["generated_at"] is None
    assert output["host"]["cpu_percent"] is None
    assert output["network"]["rx_bps"] is None
    output = json.loads((tmp_path / "capacity.json").read_text())
    assert output["measured_at"] is None
    assert output["pools"][0]["free_bytes"] is None
    assert "secret-value" not in (tmp_path / "host.json").read_text()


def test_published_snapshots_feed_exporter_without_refreshing_old_or_invalid_evidence(tmp_path):
    from homeserver_telemetry.metrics import create_metrics_app

    host, capacity = source_payloads()
    assert publish(tmp_path, host, capacity) is True
    client = TestClient(create_metrics_app(snapshot_dir=tmp_path, clock=lambda: 1_800_000_000))
    text = client.get("/metrics").text
    assert "homeserver_host_cpu_percent 23" in text
    assert 'homeserver_pool_free_bytes{pool="ssd"} 600' in text

    host["generated_at"] = 1_799_999_000
    capacity["measured_at"] = 1_799_999_000
    assert publish(tmp_path, host, capacity) is True
    text = client.get("/metrics").text
    assert 'homeserver_snapshot_fresh{source="host"} 0' in text
    assert 'homeserver_snapshot_fresh{source="capacity"} 0' in text
    assert "homeserver_host_cpu_percent" not in text
    assert "homeserver_pool_free_bytes" not in text

    capacity["measured_at"] = 1_800_000_000
    capacity["pools"][0]["free_bytes"] = True
    assert publish(tmp_path, host, capacity) is True
    assert "homeserver_pool_free_bytes" not in client.get("/metrics").text


def test_missing_network_section_does_not_hide_valid_host_measurements(tmp_path):
    from homeserver_telemetry.metrics import create_metrics_app

    host, capacity = source_payloads()
    host.pop("network")
    assert publish(tmp_path, host, capacity) is True
    client = TestClient(create_metrics_app(snapshot_dir=tmp_path, clock=lambda: 1_800_000_000))
    text = client.get("/metrics").text
    assert "homeserver_host_cpu_percent 23" in text
    assert "homeserver_network_receive_bytes_per_second" not in text


@pytest.mark.parametrize("pool_field,value", [
    ("pool_id", "secret-pool"), ("state", "secret-error"), ("filesystem_id", None),
])
def test_unknown_pool_identity_or_state_cannot_be_published_as_healthy(tmp_path, pool_field, value):
    from homeserver_telemetry.metrics import create_metrics_app

    host, capacity = source_payloads()
    capacity["pools"][0][pool_field] = value
    assert publish(tmp_path, host, capacity) is True
    text = (tmp_path / "capacity.json").read_text()
    assert "secret-pool" not in text and "secret-error" not in text
    client = TestClient(create_metrics_app(snapshot_dir=tmp_path, clock=lambda: 1_800_000_000))
    assert "homeserver_pool_ready" not in client.get("/metrics").text


def load_collector():
    spec = importlib.util.spec_from_file_location(
        "publisher_host_metrics", "scripts/host-metrics.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("publish_enabled", [False, True])
def test_collector_publication_is_opt_in_and_reuses_its_in_memory_sample(
    tmp_path, monkeypatch, publish_enabled,
):
    module = load_collector()
    host, _capacity = source_payloads()
    output = tmp_path / "control" / "host.json"
    destination = tmp_path / "metrics"
    destination.mkdir()
    registry_path = tmp_path / "invalid-registry.json"
    registry_path.write_text("{invalid")
    monkeypatch.setattr(module, "collect", lambda **kwargs: host)
    arguments = [
        "host-metrics.py", "--output", str(output), "--storage-registry", str(registry_path),
    ]
    if publish_enabled:
        arguments.extend(["--metrics-output-dir", str(destination)])
    monkeypatch.setattr("sys.argv", arguments)

    assert module.main() == 4  # Original invalid-registry return code is preserved.
    assert json.loads(output.read_text())["host"]["cpu_percent"] == 23
    if publish_enabled:
        published = json.loads((destination / "host.json").read_text())
        assert published["generated_at"] == "2027-01-15T07:59:50+00:00"
        assert published["host"]["cpu_percent"] == 23
        assert json.loads((destination / "capacity.json").read_text())["pools"][0]["state"] == (
            "unavailable"
        )
    else:
        assert not list(destination.iterdir())


def test_publication_failure_preserves_collector_output_and_exit_status(
    tmp_path, monkeypatch, capsys,
):
    module = load_collector()
    host, _capacity = source_payloads()
    output = tmp_path / "control" / "host.json"
    missing = tmp_path / "secret-unavailable-output"
    monkeypatch.setenv("HOMESERVER_MEDIA_ROOT", str(tmp_path / "missing-media"))
    monkeypatch.setattr(module, "collect", lambda **kwargs: host)
    monkeypatch.setattr("sys.argv", [
        "host-metrics.py", "--output", str(output), "--metrics-output-dir", str(missing),
        "--storage-registry", str(tmp_path / "no-registry"),
    ])

    assert module.main() == 4
    assert output.is_file()
    assert capsys.readouterr().err == "metrics snapshot publication failed\n"
    assert not missing.exists()
