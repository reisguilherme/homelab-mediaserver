"""Offline Jellyfin containment operates only on fresh, inactive fixture copies."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/prepare-playback-config.py"
REFRESH = "7738148f-fcd0-7979-c7ce-b148e06b3aed.js"
EXISTING = "11111111-1111-1111-1111-111111111111.js"


@pytest.fixture
def preparer():
    assert SCRIPT.is_file(), "offline playback configuration preparer is missing"
    spec = importlib.util.spec_from_file_location("playback_config", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "jellyfin"
    (root / "config/ScheduledTasks").mkdir(parents=True)
    (root / "config/system.xml").write_text(
        "<ServerConfiguration><IsStartupWizardCompleted>true</IsStartupWizardCompleted>"
        "<Unrelated>private-fixture-value</Unrelated></ServerConfiguration>"
    )
    (root / "config/encoding.xml").write_text(
        "<EncodingOptions><HardwareAccelerationType>qsv</HardwareAccelerationType>"
        "<EnableHardwareEncoding>true</EnableHardwareEncoding><TranscodingTempPath />"
        "<EnableThrottling>false</EnableThrottling>"
        "<EnableSegmentDeletion>false</EnableSegmentDeletion>"
        "<H264Crf>23</H264Crf></EncodingOptions>"
    )
    for name, flag in (("Movies", "true"), ("TV", "false")):
        folder = root / "root/default" / name
        folder.mkdir(parents=True)
        (folder / "options.xml").write_text(
            "<LibraryOptions>"
            f"<EnableRealtimeMonitor>{flag}</EnableRealtimeMonitor>"
            f"<PathInfos><MediaPathInfo><Path>/data/media/{name}</Path>"
            "</MediaPathInfo></PathInfos><Custom>preserved</Custom></LibraryOptions>"
        )
    (root / "config/ScheduledTasks" / EXISTING).write_text('[{"Type":"StartupTrigger"}]')
    (root / "plugins/Plugin_1.0").mkdir(parents=True)
    (root / "plugins/Plugin_1.0/plugin.dll").write_bytes(b"fixture, never executable")
    (root / "plugins/configurations").mkdir()
    (root / "plugins/configurations/plugin.xml").write_text("<key>private-fixture-value</key>")
    return root


def snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    }


def test_disables_before_startup_preserving_originals_and_plugins(preparer, config):
    before = snapshot(config)
    summary = preparer.prepare(config)
    assert summary["prepared"] is True
    assert summary["libraries"] == 2
    assert summary["scheduled_tasks_held"] == 21
    assert summary["plugins_quarantined"] is True
    backup = config.parent / summary["backup_directory"]
    assert backup.is_dir() and backup.stat().st_mode & 0o777 == 0o700
    for name, content in before.items():
        assert (backup / "originals" / name).read_bytes() == content
    assert not list((config / "plugins").iterdir())
    assert (config / "config/system.xml").read_bytes() == before["config/system.xml"]
    for path in (config / "root/default").glob("*/options.xml"):
        xml = ET.parse(path)
        assert xml.findtext("EnableRealtimeMonitor") == "false"
        assert xml.findtext("Custom") == "preserved"
        assert xml.findtext("PathInfos/MediaPathInfo/Path").startswith("/data/media/")
    tasks = config / "config/ScheduledTasks"
    assert (tasks / REFRESH).read_text() == "[]\n"
    assert (tasks / EXISTING).read_text() == "[]\n"
    assert len(list(tasks.glob("*.js"))) == 21
    assert all(json.loads(path.read_text()) == [] for path in tasks.glob("*.js"))
    assert "private-fixture-value" not in json.dumps(summary)
    assert json.loads((config / preparer.MARKER).read_text())["state"] == "complete"


def test_software_transcoding_uses_bounded_mount_and_preserves_original(preparer, config):
    path = config / "config/encoding.xml"
    original = path.read_bytes()
    summary = preparer.prepare(config)
    assert (config.parent / summary["backup_directory"] / "originals/config/encoding.xml"
            ).read_bytes() == original
    encoding = ET.parse(path)
    assert encoding.findtext("HardwareAccelerationType") == "none"
    assert encoding.findtext("EnableHardwareEncoding") == "false"
    assert encoding.findtext("TranscodingTempPath") == "/transcode"
    assert encoding.findtext("EnableThrottling") == "true"
    assert encoding.findtext("EnableSegmentDeletion") == "true"
    assert encoding.findtext("H264Crf") == "23"


@pytest.mark.parametrize("invalid", ["false", "", "TRUE", "true</IsStartupWizardCompleted>"
                         "<IsStartupWizardCompleted>true"])
def test_wizard_must_be_unambiguously_complete_before_any_write(preparer, config, invalid):
    (config / "config/system.xml").write_text(
        f"<ServerConfiguration><IsStartupWizardCompleted>{invalid}"
        "</IsStartupWizardCompleted></ServerConfiguration>"
    )
    before = snapshot(config)
    with pytest.raises(preparer.PreparationError):
        preparer.prepare(config)
    assert snapshot(config) == before
    assert list(config.parent.iterdir()) == [config]


@pytest.mark.parametrize("target,content", [
    ("config/system.xml", "<not-closed>"),
    ("config/encoding.xml", "<WrongOptions />"),
    ("config/encoding.xml", "<EncodingOptions><broken></EncodingOptions>"),
    ("config/encoding.xml", "<EncodingOptions><TranscodingTempPath>/cache/transcodes"
     "</TranscodingTempPath><TranscodingTempPath>/other</TranscodingTempPath></EncodingOptions>"),
    ("root/default/TV/options.xml", "<LibraryOptions><broken></LibraryOptions>"),
    ("root/default/TV/options.xml", "<Other />"),
    ("root/default/TV/options.xml", '<!DOCTYPE x [<!ENTITY a "true">]><LibraryOptions/>'),
    (f"config/ScheduledTasks/{EXISTING}", "not json"),
    (f"config/ScheduledTasks/{EXISTING}", "null"),
    (f"config/ScheduledTasks/{EXISTING}", '[{"Type":"unknown"}]'),
    (f"config/ScheduledTasks/{EXISTING}", '[{"Type":[]}]'),
])
def test_invalid_late_input_causes_no_partial_preparation(preparer, config, target, content):
    (config / target).write_text(content)
    before = snapshot(config)
    with pytest.raises(preparer.PreparationError):
        preparer.prepare(config)
    assert snapshot(config) == before
    assert list(config.parent.iterdir()) == [config]


@pytest.mark.parametrize("target", ["config/system.xml", "root/default/TV/options.xml",
                                   f"config/ScheduledTasks/{EXISTING}",
                                   "plugins/Plugin_1.0/plugin.dll"])
def test_symlink_input_cannot_escape_or_mutate_target(preparer, config, target):
    path = config / target
    outside = config.parent / "outside"
    outside.write_bytes(path.read_bytes())
    original = outside.read_bytes()
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(preparer.PreparationError, match="unsafe_path"):
        preparer.prepare(config)
    assert outside.read_bytes() == original
    assert path.is_symlink()
    assert not (config / preparer.MARKER).exists()


def test_symlink_ancestor_rejected(preparer, config):
    alias = config.parent / "alias"
    alias.symlink_to(config.parent, target_is_directory=True)
    with pytest.raises(preparer.PreparationError, match="unsafe_path"):
        preparer.prepare(alias / config.name)
    assert not (config / preparer.MARKER).exists()


def test_special_file_and_hardlinked_mutation_input_rejected(preparer, config):
    pipe = config / "plugins/pipe"
    os.mkfifo(pipe)
    with pytest.raises(preparer.PreparationError, match="unsafe_path"):
        preparer.prepare(config)
    pipe.unlink()
    path = config / "root/default/Movies/options.xml"
    os.link(path, config.parent / "hardlinked-options")
    with pytest.raises(preparer.PreparationError, match="unsafe_path"):
        preparer.prepare(config)
    assert not (config / preparer.MARKER).exists()


def test_missing_schedule_directory_and_no_plugins_are_supported(preparer, config):
    import shutil

    shutil.rmtree(config / "config/ScheduledTasks")
    shutil.rmtree(config / "plugins")
    summary = preparer.prepare(config)
    assert summary["plugins_quarantined"] is False
    assert len(list((config / "config/ScheduledTasks").glob("*.js"))) == 20


def test_mode_and_owner_of_existing_options_are_preserved(preparer, config):
    path = config / "root/default/Movies/options.xml"
    path.chmod(0o640)
    before = path.stat()
    preparer.prepare(config)
    after = path.stat()
    assert after.st_mode & 0o777 == 0o640
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)


def test_partial_failure_retains_backup_and_blocks_blind_retry(preparer, config, monkeypatch):
    before = snapshot(config)
    original = preparer.replace_file
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("private-fixture-value")
        return original(*args, **kwargs)

    monkeypatch.setattr(preparer, "replace_file", fail_second)
    with pytest.raises(preparer.PreparationError, match="preparation_incomplete"):
        preparer.prepare(config)
    marker = json.loads((config / preparer.MARKER).read_text())
    assert marker["state"] == "incomplete"
    backup = config.parent / marker["backup_directory"]
    for name, content in before.items():
        assert (backup / "originals" / name).read_bytes() == content
    partial = snapshot(config)
    with pytest.raises(preparer.PreparationError, match="already_prepared_or_incomplete"):
        preparer.prepare(config)
    assert snapshot(config) == partial


def test_cli_outputs_only_sanitized_json(config):
    result = subprocess.run([sys.executable, str(SCRIPT), "--config-root", str(config)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["prepared"] is True
    assert result.stderr == ""
    assert "private-fixture-value" not in result.stdout
    second = subprocess.run([sys.executable, str(SCRIPT), "--config-root", str(config)],
                            capture_output=True, text=True, check=False)
    assert second.returncode == 2
    assert json.loads(second.stdout)["error"] == "already_prepared_or_incomplete"
    assert second.stderr == ""


def test_backup_creation_error_is_sanitized_before_mutation(preparer, config, monkeypatch):
    before = snapshot(config)

    def no_backup(*args, **kwargs):
        raise OSError("private-fixture-value")

    monkeypatch.setattr(preparer.tempfile, "mkdtemp", no_backup)
    with pytest.raises(preparer.PreparationError, match="backup_unavailable"):
        preparer.prepare(config)
    assert snapshot(config) == before
    assert list(config.parent.iterdir()) == [config]


def test_missing_encoding_file_fails_before_any_write(preparer, config):
    (config / "config/encoding.xml").unlink()
    before = snapshot(config)
    with pytest.raises(preparer.PreparationError):
        preparer.prepare(config)
    assert snapshot(config) == before
    assert list(config.parent.iterdir()) == [config]


def test_replacement_metadata_and_directory_are_synced_before_completion(
    preparer, config, monkeypatch,
):
    events = []
    actual_sync = preparer.os.fsync
    actual_own = preparer.own_file
    actual_replace = preparer.os.replace
    actual_rename = preparer.os.rename
    actual_marker = preparer.write_marker

    def sync(descriptor):
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        kind = "sync_directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "sync_file"
        events.append((kind, path))
        return actual_sync(descriptor)

    def own(path, info):
        events.append(("metadata", path))
        return actual_own(path, info)

    def replace(source, target):
        events.append(("replace", source, target))
        return actual_replace(source, target)

    def marker(path, document, **kwargs):
        events.append(("marker", document["state"]))
        return actual_marker(path, document, **kwargs)

    def rename(source, target):
        events.append(("rename", source, target))
        return actual_rename(source, target)

    monkeypatch.setattr(preparer.os, "fsync", sync)
    monkeypatch.setattr(preparer, "own_file", own)
    monkeypatch.setattr(preparer.os, "replace", replace)
    monkeypatch.setattr(preparer.os, "rename", rename)
    monkeypatch.setattr(preparer, "write_marker", marker)
    summary = preparer.prepare(config)
    complete = events.index(("marker", "complete"))
    for index, event in enumerate(events):
        if event[0] != "replace":
            continue
        _, source, target = event
        metadata = events.index(("metadata", source))
        assert ("sync_file", source) in events[metadata + 1:index]
        assert ("sync_directory", target.parent) in events[index + 1:complete]
    backup = config.parent / summary["backup_directory"]
    move = events.index(("rename", config / "plugins", backup / "originals/plugins"))
    assert ("sync_directory", backup / "originals") in events[move + 1:complete]
    assert ("sync_directory", config / "plugins") in events[move + 1:complete]
    assert ("sync_directory", config) in events[move + 1:complete]
    assert ("sync_directory", config.parent) in events[:complete]
    assert ("sync_directory", config) in events[complete + 1:]
