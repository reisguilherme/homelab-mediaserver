from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "host-metrics.py"
    spec = importlib.util.spec_from_file_location("host_metrics", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_counter_fixture(root: Path, *, cpu: str, rx: int, tx: int) -> None:
    proc = root / "proc"
    network = root / "net" / "enxusb" / "statistics"
    (proc / "net").mkdir(parents=True, exist_ok=True)
    network.mkdir(parents=True, exist_ok=True)
    (proc / "stat").write_text(cpu, encoding="utf-8")
    (proc / "net" / "route").write_text(
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        "enxusb 00000000 0101A8C0 0003 0 0 100 00000000 0 0 0\n",
        encoding="utf-8",
    )
    (network / "rx_bytes").write_text(str(rx), encoding="utf-8")
    (network / "tx_bytes").write_text(str(tx), encoding="utf-8")


def test_host_snapshot_derives_cpu_and_network_rate_from_two_samples(
    tmp_path: Path, monkeypatch
) -> None:
    metrics = _module()
    _write_counter_fixture(
        tmp_path,
        cpu="cpu  30 0 20 100 0 0 0 0 0 0\n",
        rx=1000,
        tx=2000,
    )
    # Other processes can allocate bytes between two statvfs calls. Use one
    # captured filesystem sample while testing CPU/network counter deltas.
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(metrics.shutil, "disk_usage", lambda _path: usage)
    first = metrics.collect(
        media_path=tmp_path,
        proc_root=tmp_path / "proc",
        network_root=tmp_path / "net",
        sampled_at=100.0,
    )
    assert first["host"]["cpu_percent"] is None
    assert first["network"]["rx_bps"] is None
    assert first["network"]["tx_bps"] is None
    assert first["storage"]["used_bytes"] == usage.used

    _write_counter_fixture(
        tmp_path,
        cpu="cpu  80 0 20 150 0 0 0 0 0 0\n",
        rx=2000,
        tx=2400,
    )
    second = metrics.collect(
        media_path=tmp_path,
        proc_root=tmp_path / "proc",
        network_root=tmp_path / "net",
        sampled_at=110.0,
        previous=first,
    )
    assert second["host"]["cpu_percent"] == 50.0
    assert second["network"] == {"interface": "enxusb", "rx_bps": 100, "tx_bps": 40}


def test_network_change_does_not_report_a_false_zero_rate(tmp_path: Path) -> None:
    metrics = _module()
    _write_counter_fixture(tmp_path, cpu="cpu  5 0 5 90 0 0 0 0\n", rx=50, tx=60)
    previous = {
        "counters": {
            "sampled_at": 100.0,
            "cpu_total": 100,
            "cpu_idle": 90,
            "network_interface": "enp8s0",
            "rx_bytes": 1000,
            "tx_bytes": 2000,
        }
    }
    snapshot = metrics.collect(
        media_path=tmp_path,
        proc_root=tmp_path / "proc",
        network_root=tmp_path / "net",
        sampled_at=110.0,
        previous=previous,
    )
    assert snapshot["network"] == {"interface": "enxusb", "rx_bps": None, "tx_bps": None}


def test_cpu_guest_time_is_not_counted_twice(tmp_path: Path) -> None:
    metrics = _module()
    _write_counter_fixture(tmp_path, cpu="cpu  100 0 0 100 0 0 0 0 20 0\n", rx=1, tx=1)
    first = metrics.collect(
        media_path=tmp_path,
        proc_root=tmp_path / "proc",
        network_root=tmp_path / "net",
        sampled_at=100.0,
    )
    _write_counter_fixture(tmp_path, cpu="cpu  150 0 0 150 0 0 0 0 70 0\n", rx=1, tx=1)
    second = metrics.collect(
        media_path=tmp_path,
        proc_root=tmp_path / "proc",
        network_root=tmp_path / "net",
        sampled_at=110.0,
        previous=first,
    )
    assert second["host"]["cpu_percent"] == 50.0


def test_cpu_temperature_uses_cpu_zone_instead_of_cooler_unrelated_zone(
    tmp_path: Path, monkeypatch
) -> None:
    metrics = _module()
    unrelated = tmp_path / "thermal_zone0"
    cpu = tmp_path / "thermal_zone1"
    for zone, sensor_type, temperature in (
        (unrelated, "acpitz", "24000"),
        (cpu, "x86_pkg_temp", "68000"),
    ):
        zone.mkdir()
        (zone / "type").write_text(sensor_type, encoding="utf-8")
        (zone / "temp").write_text(temperature, encoding="utf-8")
    monkeypatch.setattr(
        metrics, "glob", lambda _pattern: [str(unrelated / "temp"), str(cpu / "temp")]
    )

    assert metrics.collect(media_path=tmp_path)["host"]["cpu_celsius"] == 68.0


def test_cpu_temperature_is_unavailable_without_cpu_zone(tmp_path: Path, monkeypatch) -> None:
    metrics = _module()
    zone = tmp_path / "thermal_zone0"
    zone.mkdir()
    (zone / "type").write_text("acpitz", encoding="utf-8")
    (zone / "temp").write_text("24000", encoding="utf-8")
    monkeypatch.setattr(metrics, "glob", lambda _pattern: [str(zone / "temp")])

    assert metrics.collect(media_path=tmp_path)["host"]["cpu_celsius"] is None


def test_docker_collector_writes_live_capacity_without_an_env_file(tmp_path, monkeypatch):
    import json

    metrics = _module()
    output = tmp_path / "run/host.json"
    from types import SimpleNamespace

    monkeypatch.setattr(
        metrics.os,
        "statvfs",
        lambda _path: SimpleNamespace(
            f_bavail=100,
            f_bfree=100,
            f_frsize=4096,
            f_blocks=200,
        ),
    )
    monkeypatch.setenv("HOMESERVER_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["host-metrics.py", "--output", str(output)])
    assert metrics.main() == 0
    capacity = json.loads((output.parent / "capacity.json").read_text())
    assert capacity["filesystem_id"]
    assert capacity["free_bytes"] == 100 * 4096
    assert capacity["total_bytes"] > 0


def test_docker_collector_blocks_admission_when_storage_is_unavailable(tmp_path, monkeypatch):
    import json

    metrics = _module()
    output = tmp_path / "run/host.json"
    monkeypatch.setenv("HOMESERVER_MEDIA_ROOT", str(tmp_path / "unavailable"))
    monkeypatch.setattr("sys.argv", ["host-metrics.py", "--output", str(output)])
    assert metrics.main() == 4
    capacity = json.loads((output.parent / "capacity.json").read_text())
    assert capacity["filesystem_id"] is None
