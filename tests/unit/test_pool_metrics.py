"""Physical storage samples stay independent from host counters and each other."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _script(name):
    spec = importlib.util.spec_from_file_location(name, Path("scripts") / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RegistryFixture:
    pool_ids = ("ssd", "hdd")

    def __init__(self, root, *, failed=()):
        self.failed = failed
        self.pools = {
            pool_id: SimpleNamespace(
                pool_id=pool_id, label=label, filesystem_id=f"uuid-{pool_id}", root=root / pool_id
            )
            for pool_id, label in [("ssd", "SSD"), ("hdd", "HD USB")]
        }
        for pool in self.pools.values():
            pool.root.mkdir()

    def inspect(self, pool_id, *, writable=False):
        from homeserver_common.storage import StorageUnavailable

        assert writable is False
        if pool_id in self.failed:
            raise StorageUnavailable("mount_missing")
        dto = dict(
            pool_id=pool_id,
            label=self.pools[pool_id].label,
            filesystem_id=f"uuid-{pool_id}",
            state="ready",
            reason=None,
            measured_at=100.0,
            total_bytes=100_000,
            used_bytes=30_000,
            free_bytes=60_000,
        )
        return SimpleNamespace(**dto, as_dict=lambda: dto.copy())


def test_capacity_pools_keep_ssd_available_when_hdd_missing(tmp_path):
    registry = RegistryFixture(tmp_path, failed=("hdd",))
    result = _script("capacity-snapshot").collect_pools(registry)
    assert result["free_bytes"] == 60_000
    assert result["filesystem_id"] == "uuid-ssd"
    ssd, hdd = result["pools"]
    assert ssd["state"] == "ready"
    assert hdd["state"] == "unavailable"
    assert hdd["total_bytes"] is hdd["used_bytes"] is hdd["free_bytes"] is None


def test_host_collects_physical_hardlinks_once_per_pool(tmp_path):
    import os

    registry = RegistryFixture(tmp_path)
    for pool in registry.pools.values():
        movies = pool.root / "media/movies"
        torrents = pool.root / "torrents"
        movies.mkdir(parents=True)
        torrents.mkdir()
        movie = movies / "film.mkv"
        movie.write_bytes(b"x" * 4096)
        os.link(movie, torrents / "film.mkv")
    result = _script("host-metrics").collect(registry=registry, sampled_at=100.0)
    for pool in result["pools"]:
        assert pool["storage"]["movies_bytes"] == 4096
        assert pool["storage"]["torrents_bytes"] == 0
        assert pool["storage"]["other_bytes"] == 25_904


def test_host_snapshot_updates_even_when_legacy_media_disappears(tmp_path, monkeypatch):
    metrics = _script("host-metrics")
    output = tmp_path / "run/host.json"
    monkeypatch.setenv("HOMESERVER_MEDIA_ROOT", str(tmp_path / "missing"))
    monkeypatch.setattr("sys.argv", ["host-metrics.py", "--output", str(output)])
    assert metrics.main() == 4
    payload = json.loads(output.read_text())
    assert payload["host"]["ram_percent"] is not None
    assert payload["generated_at"]


def test_host_pool_failures_do_not_block_other_storage_or_host(tmp_path):
    registry = RegistryFixture(tmp_path, failed=("ssd",))
    result = _script("host-metrics").collect(registry=registry)
    assert result["host"]["ram_percent"] is not None
    assert result["pools"][0]["used_bytes"] is None
    assert result["pools"][1]["free_bytes"] == 60_000


def test_invalid_registry_does_not_measure_legacy_fallback_or_stop_host(tmp_path, monkeypatch):
    metrics = _script("host-metrics")
    registry = tmp_path / "storage.json"
    registry.write_text("{invalid")
    output = tmp_path / "run/host.json"
    monkeypatch.setenv("HOMESERVER_MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        ["host-metrics.py", "--output", str(output), "--storage-registry", str(registry)],
    )
    assert metrics.main() == 4
    host = json.loads(output.read_text())
    assert host["host"]["ram_percent"] is not None
    assert host["storage"] is None
    capacity = json.loads((output.parent / "capacity.json").read_text())
    assert capacity["free_bytes"] is None
    assert all(pool["state"] == "unavailable" for pool in capacity["pools"])


@pytest.mark.parametrize("missing", ["ssd", "hdd"])
def test_cpu_and_network_continue_sampling_when_either_pool_fails(tmp_path, missing):
    registry = RegistryFixture(tmp_path, failed=(missing,))
    proc = tmp_path / "proc"
    counters = tmp_path / "net/fixture/statistics"
    proc.mkdir()
    counters.mkdir(parents=True)
    (proc / "stat").write_text("cpu  30 0 20 100 0 0 0 0\n")
    (counters / "rx_bytes").write_text("1000")
    (counters / "tx_bytes").write_text("2000")
    metrics = _script("host-metrics")
    first = metrics.collect(
        registry=registry,
        proc_root=proc,
        network_root=tmp_path / "net",
        network_interface="fixture",
        sampled_at=100.0,
    )
    (proc / "stat").write_text("cpu  80 0 20 150 0 0 0 0\n")
    (counters / "rx_bytes").write_text("2000")
    (counters / "tx_bytes").write_text("2400")
    second = metrics.collect(
        registry=registry,
        proc_root=proc,
        network_root=tmp_path / "net",
        network_interface="fixture",
        sampled_at=110.0,
        previous=first,
    )
    assert second["host"]["cpu_percent"] == 50.0
    assert second["network"]["rx_bps"] == 100
    assert second["network"]["tx_bps"] == 40
