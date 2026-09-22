import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path("scripts/capacity-snapshot.py").resolve()
    spec = importlib.util.spec_from_file_location("capacity_snapshot", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_capacity_snapshot_requires_guard_before_and_after_measurement() -> None:
    module = _module()
    calls: list[str] = []

    def guard() -> None:
        calls.append("guard")

    def usage() -> tuple[int, int]:
        calls.append("usage")
        return (1000, 400)

    result = module.collect("uuid-test", guard=guard, usage=usage, clock=lambda: 123.0)
    assert calls == ["guard", "usage", "guard"]
    assert result == {
        "filesystem_id": "uuid-test",
        "total_bytes": 1000,
        "free_bytes": 400,
        "measured_at": 123.0,
    }


def test_capacity_snapshot_does_not_publish_failed_guard(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "capacity.json"
    output.write_text("stale", encoding="utf-8")

    def fail() -> None:
        raise ValueError("mount unavailable")

    with pytest.raises(ValueError, match="mount unavailable"):
        module.refresh(output, "uuid-test", guard=fail, usage=lambda: (1000, 400))
    assert not output.exists()
