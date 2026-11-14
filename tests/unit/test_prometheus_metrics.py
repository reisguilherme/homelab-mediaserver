"""Snapshot metrics reject unusable evidence without manufacturing healthy values."""

import json
import os
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

NOW = 1_800_000_000.0


def host_snapshot(**overrides):
    return {
        "generated_at": datetime.fromtimestamp(NOW - 10, UTC).isoformat(),
        "host": {
            "cpu_percent": 25.5,
            "ram_percent": 60,
            "cpu_celsius": 54.25,
            "uptime_seconds": 12345,
        },
        "network": {"interface": "private-interface", "rx_bps": 1200, "tx_bps": 300},
        **overrides,
    }


def pool_snapshot(**overrides):
    return {
        "pool_id": "ssd",
        "filesystem_id": "private-uuid",
        "state": "ready",
        "measured_at": NOW - 5,
        "total_bytes": 1000,
        "free_bytes": 600,
        "used_bytes": 300,
        **overrides,
    }


def write_snapshot(root, name, payload):
    (root / name).write_text(json.dumps(payload), encoding="utf-8")


def client_for(root, clock=lambda: NOW):
    from homeserver_telemetry.metrics import create_metrics_app

    return TestClient(create_metrics_app(snapshot_dir=root, clock=clock))


def samples(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    return {
        name: float(value)
        for line in response.text.splitlines()
        if line and not line.startswith("#")
        for name, value in [line.split(" ")]
    }


def test_fresh_snapshots_expose_host_network_and_independent_capacity(tmp_path):
    write_snapshot(tmp_path, "host.json", host_snapshot())
    write_snapshot(tmp_path, "capacity.json", {
        "measured_at": NOW - 5,
        "pools": [pool_snapshot(), pool_snapshot(pool_id="hdd", filesystem_id="other-uuid")],
    })

    result = samples(client_for(tmp_path))

    assert result == {
        'homeserver_snapshot_valid{source="host"}': 1,
        'homeserver_snapshot_fresh{source="host"}': 1,
        'homeserver_snapshot_age_seconds{source="host"}': 10,
        'homeserver_snapshot_valid{source="capacity"}': 1,
        'homeserver_snapshot_fresh{source="capacity"}': 1,
        'homeserver_snapshot_age_seconds{source="capacity"}': 5,
        "homeserver_host_cpu_percent": 25.5,
        "homeserver_host_ram_percent": 60,
        "homeserver_host_cpu_celsius": 54.25,
        "homeserver_host_uptime_seconds": 12345,
        "homeserver_network_receive_bytes_per_second": 1200,
        "homeserver_network_transmit_bytes_per_second": 300,
        'homeserver_pool_ready{pool="ssd"}': 1,
        'homeserver_pool_total_bytes{pool="ssd"}': 1000,
        'homeserver_pool_free_bytes{pool="ssd"}': 600,
        'homeserver_pool_used_bytes{pool="ssd"}': 300,
        'homeserver_pool_ready{pool="hdd"}': 1,
        'homeserver_pool_total_bytes{pool="hdd"}': 1000,
        'homeserver_pool_free_bytes{pool="hdd"}': 600,
        'homeserver_pool_used_bytes{pool="hdd"}': 300,
    }


@pytest.mark.parametrize("source,limit", [("host", 90), ("capacity", 30)])
def test_each_scrape_rechecks_age_and_removes_expired_operational_values(tmp_path, source, limit):
    instant = [NOW]
    payload = host_snapshot(generated_at=NOW) if source == "host" else {
        "measured_at": NOW, "pools": [pool_snapshot(measured_at=NOW)],
    }
    write_snapshot(tmp_path, f"{source}.json", payload)
    client = client_for(tmp_path, lambda: instant[0])
    instant[0] = NOW + limit
    fresh = samples(client)
    assert fresh[f'homeserver_snapshot_fresh{{source="{source}"}}'] == 1
    assert any(name.startswith("homeserver_host_" if source == "host" else "homeserver_pool_")
               for name in fresh)

    instant[0] += 0.001
    stale = samples(client)
    assert stale[f'homeserver_snapshot_fresh{{source="{source}"}}'] == 0
    assert stale[f'homeserver_snapshot_valid{{source="{source}"}}'] == 1
    assert stale[f'homeserver_snapshot_age_seconds{{source="{source}"}}'] > limit
    assert all(name.startswith("homeserver_snapshot_") for name in stale)


@pytest.mark.parametrize("timestamp", [None, True, "bad", "2027-01-15T08:00:00", -1, NOW + 5.001])
@pytest.mark.parametrize("source", ["host", "capacity"])
def test_invalid_timestamps_do_not_publish_age_or_operational_values(tmp_path, source, timestamp):
    payload = host_snapshot(generated_at=timestamp) if source == "host" else {
        "measured_at": timestamp, "pools": [pool_snapshot()],
    }
    write_snapshot(tmp_path, f"{source}.json", payload)

    result = samples(client_for(tmp_path))

    assert result[f'homeserver_snapshot_valid{{source="{source}"}}'] == 0
    assert result[f'homeserver_snapshot_fresh{{source="{source}"}}'] == 0
    assert f'homeserver_snapshot_age_seconds{{source="{source}"}}' not in result
    assert all(name.startswith("homeserver_snapshot_") for name in result)


def test_small_clock_skew_is_tolerated_without_negative_age(tmp_path):
    write_snapshot(tmp_path, "host.json", host_snapshot(generated_at=NOW + 5))
    result = samples(client_for(tmp_path))
    assert result['homeserver_snapshot_fresh{source="host"}'] == 1
    assert result['homeserver_snapshot_age_seconds{source="host"}'] == 0


@pytest.mark.parametrize("field,value", [
    ("cpu_percent", -1), ("cpu_percent", 100.01), ("ram_percent", 101),
    ("ram_percent", True), ("uptime_seconds", -1), ("uptime_seconds", "123"),
    ("cpu_celsius", None), ("cpu_celsius", -274), ("cpu_celsius", False),
])
def test_invalid_host_values_are_omitted_without_hiding_other_valid_fields(tmp_path, field, value):
    payload = host_snapshot()
    payload["host"][field] = value
    write_snapshot(tmp_path, "host.json", payload)

    result = samples(client_for(tmp_path))

    assert f"homeserver_host_{field}" not in result
    assert result["homeserver_network_receive_bytes_per_second"] == 1200


@pytest.mark.parametrize("value", [None, True, -1, "200"])
def test_invalid_network_values_are_not_zeroed(tmp_path, value):
    payload = host_snapshot()
    payload["network"]["rx_bps"] = value
    write_snapshot(tmp_path, "host.json", payload)
    result = samples(client_for(tmp_path))
    assert "homeserver_network_receive_bytes_per_second" not in result
    assert result["homeserver_network_transmit_bytes_per_second"] == 300


@pytest.mark.parametrize("raw", [
    "{broken", "[]", "null", '{"generated_at": NaN}', '{"generated_at": Infinity}',
    '{"generated_at": 1800000000, "generated_at": 0}',
    '{"generated_at": 1800000000, "host": {"cpu_percent": 1e999}}',
    pytest.param("[" * 1200 + "]" * 1200, id="excessive-nesting"),
])
def test_corrupt_or_ambiguous_json_is_rejected_without_disclosing_input(tmp_path, raw):
    (tmp_path / "host.json").write_text(raw, encoding="utf-8")
    result = samples(client_for(tmp_path))
    assert result['homeserver_snapshot_valid{source="host"}'] == 0
    assert all(name.startswith("homeserver_snapshot_") for name in result)


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "fifo", "oversize", "utf8"])
def test_nonregular_missing_and_oversized_files_remain_unavailable(tmp_path, kind):
    target = tmp_path / "host.json"
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        outside = tmp_path / "private.json"
        write_snapshot(tmp_path, "private.json", host_snapshot())
        target.symlink_to(outside)
    elif kind == "fifo":
        os.mkfifo(target)
    elif kind == "oversize":
        target.write_text(json.dumps(host_snapshot()) + " " * (1024 * 1024), encoding="utf-8")
    elif kind == "utf8":
        target.write_bytes(b"\xff\xfe")

    client = client_for(tmp_path)
    assert client.get("/health/live").status_code == 200
    result = samples(client)
    assert result['homeserver_snapshot_valid{source="host"}'] == 0
    assert all(name.startswith("homeserver_snapshot_") for name in result)


def test_snapshot_directory_symlink_is_not_followed(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    write_snapshot(actual, "host.json", host_snapshot())
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    assert samples(client_for(linked))['homeserver_snapshot_valid{source="host"}'] == 0


def test_intermediate_snapshot_directory_symlink_is_not_followed(tmp_path):
    actual = tmp_path / "actual" / "snapshots"
    actual.mkdir(parents=True)
    write_snapshot(actual, "host.json", host_snapshot())
    linked = tmp_path / "linked"
    linked.symlink_to(actual.parent, target_is_directory=True)
    result = samples(client_for(linked / "snapshots"))
    assert result['homeserver_snapshot_valid{source="host"}'] == 0


@pytest.mark.parametrize("overrides", [
    {"measured_at": NOW - 30.001}, {"measured_at": NOW + 5.001}, {"measured_at": True},
    {"filesystem_id": ""}, {"filesystem_id": " "}, {"filesystem_id": None},
    {"state": "mystery"}, {"state": True}, {"total_bytes": 0}, {"total_bytes": True},
    {"total_bytes": 1000.0}, {"free_bytes": -1}, {"free_bytes": 1001},
    {"free_bytes": "600"}, {"used_bytes": 401}, {"used_bytes": None},
])
def test_invalid_or_stale_pool_does_not_claim_readiness_or_capacity(tmp_path, overrides):
    write_snapshot(tmp_path, "capacity.json", {
        "measured_at": NOW, "pools": [pool_snapshot(**overrides)],
    })
    result = samples(client_for(tmp_path))
    assert all(not name.startswith("homeserver_pool_") for name in result)


def test_known_unavailable_pool_reports_not_ready_without_capacity(tmp_path):
    write_snapshot(tmp_path, "capacity.json", {
        "measured_at": NOW,
        "pools": [pool_snapshot(state="unavailable", total_bytes=None, used_bytes=None,
                                free_bytes=None, reason="private /srv/media error")],
    })
    result = samples(client_for(tmp_path))
    assert result['homeserver_pool_ready{pool="ssd"}'] == 0
    assert all(not name.startswith("homeserver_pool_total_bytes") for name in result)
    assert all('pool="hdd"' not in name for name in result)


@pytest.mark.parametrize("pools", [
    [pool_snapshot(), pool_snapshot()],
    [pool_snapshot(), pool_snapshot(pool_id="hdd")],
    [pool_snapshot(), pool_snapshot(pool_id='secret"\nmetric 1')],
    [pool_snapshot(), {"pool_id": ["ssd"]}],
    [pool_snapshot(), None],
    {"ssd": pool_snapshot()},
])
def test_duplicate_unknown_or_malformed_pools_reject_ambiguous_capacity(tmp_path, pools):
    write_snapshot(tmp_path, "capacity.json", {"measured_at": NOW, "pools": pools})
    result = samples(client_for(tmp_path))
    assert all(not name.startswith("homeserver_pool_") for name in result)
    assert result['homeserver_snapshot_valid{source="capacity"}'] == 0


def test_legacy_capacity_without_explicit_pool_identity_is_not_relabelled(tmp_path):
    write_snapshot(tmp_path, "capacity.json", {
        "measured_at": NOW, "total_bytes": 1000, "free_bytes": 600,
        "filesystem_id": "private-uuid",
    })
    assert all(not name.startswith("homeserver_pool_") for name in samples(client_for(tmp_path)))


def test_missing_fields_are_unknown_but_real_zero_measurements_are_preserved(tmp_path):
    write_snapshot(tmp_path, "host.json", {
        "generated_at": NOW,
        "host": {"cpu_percent": 0, "ram_percent": 0, "uptime_seconds": 0},
        "network": {"rx_bps": 0},
    })
    result = samples(client_for(tmp_path))
    assert result["homeserver_host_cpu_percent"] == 0
    assert result["homeserver_host_ram_percent"] == 0
    assert result["homeserver_host_uptime_seconds"] == 0
    assert result["homeserver_network_receive_bytes_per_second"] == 0
    assert "homeserver_host_cpu_celsius" not in result
    assert "homeserver_network_transmit_bytes_per_second" not in result
