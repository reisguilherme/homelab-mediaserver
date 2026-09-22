from pathlib import Path

from homeserver_control.recovery import recovery_mode_blocks


def test_missing_recovery_marker_allows_normal_start(tmp_path: Path) -> None:
    assert recovery_mode_blocks(tmp_path / "RECOVERY_MODE") is False


def test_recovery_marker_requires_explicit_enable_to_clear_guard(tmp_path: Path) -> None:
    marker = tmp_path / "RECOVERY_MODE"
    marker.write_text("admission_enabled=false\n", encoding="utf-8")
    assert recovery_mode_blocks(marker) is True

    marker.write_text("admission_enabled=true\n", encoding="utf-8")
    assert recovery_mode_blocks(marker) is False

    marker.write_text("recovery_snapshot=old\n", encoding="utf-8")
    assert recovery_mode_blocks(marker) is True


def test_unreadable_marker_path_is_fail_closed(tmp_path: Path) -> None:
    marker = tmp_path / "RECOVERY_MODE"
    marker.mkdir()
    assert recovery_mode_blocks(marker) is True


def test_dangling_recovery_marker_symlink_is_fail_closed(tmp_path: Path) -> None:
    marker = tmp_path / "RECOVERY_MODE"
    marker.symlink_to(tmp_path / "missing")
    assert recovery_mode_blocks(marker) is True
