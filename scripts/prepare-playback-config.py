#!/usr/bin/env python3
"""Contain an inactive, restored Jellyfin 12.2 Docker configuration once.

Run only on a disposable working copy, with every consumer stopped. This tool
does not stop Jellyfin, validate its image, mount storage, or modify its database.
All scheduled tasks are held during partial-library migration; external plugins
are moved outside /config into a private sibling backup. Restoring that directory
and the saved originals is a separate, offline operator action.
Encoding uses software acceleration and the bounded /transcode mount.

The paths and task IDs follow upstream v12.2 CollectionFolder,
ScheduledTaskWorker, and MediaBrowser.Common.Extensions.BaseExtensions.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

MARKER = ".playback-preparation.json"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
TASK_NAMESPACE = "Emby.Server.Implementations.ScheduledTasks.Tasks."
TASK_CLASSES = tuple(TASK_NAMESPACE + name for name in (
    "AudioNormalizationTask", "ChapterImagesTask", "CleanActivityLogTask",
    "CleanupUserDataTask", "DeleteCacheFileTask", "DeleteLogFileTask",
    "DeleteTranscodeFileTask", "MediaSegmentExtractionTask", "OptimizeDatabaseTask",
    "PeopleValidationTask", "PluginUpdateTask", "RefreshMediaLibraryTask",
)) + (
    "MediaBrowser.Providers.Lyric.LyricScheduledTask",
    "MediaBrowser.Providers.MediaInfo.SubtitleScheduledTask",
    "MediaBrowser.Providers.Plugins.Tmdb.TV.TmdbUpcomingEpisodesTask",
    "MediaBrowser.Providers.Trickplay.TrickplayImagesTask",
    "MediaBrowser.Providers.Trickplay.TrickplayMoveImagesTask",
    "Jellyfin.LiveTv.Channels.RefreshChannelsScheduledTask",
    "Jellyfin.LiveTv.Guide.RefreshGuideScheduledTask",
    "Jellyfin.MediaEncoding.Hls.ScheduledTasks.KeyframeExtractionScheduledTask",
)
TRIGGER_TYPES = {"StartupTrigger", "IntervalTrigger", "DailyTrigger", "WeeklyTrigger"}
ENCODING_OPTIONS = {
    "HardwareAccelerationType": "none",
    "EnableHardwareEncoding": "false",
    "TranscodingTempPath": "/transcode",
    "EnableThrottling": "true",
    "EnableSegmentDeletion": "true",
}


class PreparationError(Exception):
    """A public error code which never includes file contents or private paths."""


def safe_path(path, *, directory=False):
    """Reject ancestor links and non-regular inputs; never resolve through links."""
    for ancestor in (*reversed(path.parents), path):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PreparationError("unsafe_path")
    info = path.lstat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or (not directory and info.st_nlink != 1):
        raise PreparationError("unsafe_path")
    return info


def inspect_tree(path):
    safe_path(path, directory=True)
    for child in path.iterdir():
        info = child.lstat()
        if stat.S_ISDIR(info.st_mode):
            inspect_tree(child)
        else:
            safe_path(child)


def read_config(path):
    info = safe_path(path)
    if info.st_size > MAX_CONFIG_BYTES:
        raise PreparationError("configuration_too_large")
    data = path.read_bytes()
    if len(data) > MAX_CONFIG_BYTES:
        raise PreparationError("configuration_too_large")
    return data, info


def xml_document(data, tag):
    text = data.decode("utf-8-sig")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, flags=re.IGNORECASE):
        raise PreparationError("invalid_xml")
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    document = ET.fromstring(text, parser=parser)
    if document.tag != tag:
        raise PreparationError("invalid_xml")
    return document


def task_filename(class_name):
    digest = hashlib.md5(class_name.encode("utf-16le"), usedforsecurity=False).digest()
    return f"{uuid.UUID(bytes_le=digest)}.js"


def reject_nonfinite(_value):
    raise PreparationError("invalid_task_configuration")


def check_task(data):
    value = json.loads(data.decode("utf-8-sig"), parse_constant=reject_nonfinite)
    if not isinstance(value, list) or any(
        not isinstance(trigger, dict)
        or not isinstance(trigger.get("Type"), str)
        or trigger["Type"] not in TRIGGER_TYPES
        for trigger in value
    ):
        raise PreparationError("invalid_task_configuration")


def preflight(root):
    safe_path(root, directory=True)
    safe_path(root.parent, directory=True)
    if os.path.lexists(root / MARKER):
        raise PreparationError("already_prepared_or_incomplete")
    # These are the only writable/configuration trees used by this preparer.
    # Database, metadata, cache and media content are not traversed.
    for relative in ("config", "root/default"):
        inspect_tree(root / relative)
    system_path = root / "config/system.xml"
    system, system_info = read_config(system_path)
    xml = xml_document(system, "ServerConfiguration")
    wizard = xml.findall("IsStartupWizardCompleted")
    if len(wizard) != 1 or wizard[0].text != "true":
        raise PreparationError("startup_wizard_not_complete")

    originals = {system_path: (system, system_info)}
    changes = {}
    encoding_path = root / "config/encoding.xml"
    data, info = read_config(encoding_path)
    xml = xml_document(data, "EncodingOptions")
    for name, value in ENCODING_OPTIONS.items():
        matches = xml.findall(name)
        if len(matches) > 1:
            raise PreparationError("invalid_xml")
        option = matches[0] if matches else ET.SubElement(xml, name)
        option.text = value
    originals[encoding_path] = (data, info)
    changes[encoding_path] = ET.tostring(xml, encoding="utf-8", xml_declaration=True) + b"\n"
    libraries = sorted(path for path in (root / "root/default").iterdir() if path.is_dir())
    if not libraries:
        raise PreparationError("libraries_missing")
    for folder in libraries:
        path = folder / "options.xml"
        data, info = read_config(path)
        xml = xml_document(data, "LibraryOptions")
        flags = xml.findall("EnableRealtimeMonitor")
        if len(flags) > 1:
            raise PreparationError("invalid_xml")
        flag = flags[0] if flags else ET.SubElement(xml, "EnableRealtimeMonitor")
        flag.text = "false"
        originals[path] = (data, info)
        changes[path] = ET.tostring(xml, encoding="utf-8", xml_declaration=True) + b"\n"

    task_root = root / "config/ScheduledTasks"
    if task_root.exists():
        safe_path(task_root, directory=True)
        for path in sorted(task_root.iterdir()):
            if not re.fullmatch(r"[0-9a-fA-F-]{36}\.js", path.name):
                raise PreparationError("unexpected_task_configuration")
            uuid.UUID(path.stem)
            data, info = read_config(path)
            check_task(data)
            originals[path] = (data, info)
            changes[path] = b"[]\n"
    for class_name in TASK_CLASSES:
        changes[task_root / task_filename(class_name)] = b"[]\n"

    plugins = root / "plugins"
    plugin_info = None
    if os.path.lexists(plugins):
        inspect_tree(plugins)
        plugin_info = plugins.stat()
        if plugin_info.st_dev != root.parent.stat().st_dev:
            raise PreparationError("plugin_quarantine_cross_device")
    return originals, changes, len(libraries), plugin_info


def own_file(path, info):
    os.chown(path, info.st_uid, info.st_gid)
    os.chmod(path, stat.S_IMODE(info.st_mode))


def sync_path(path, *, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sync_backup_directories(backup):
    directories = [path for path in backup.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        sync_path(directory, directory=True)
    sync_path(backup, directory=True)
    sync_path(backup.parent, directory=True)


def replace_file(path, data, info, expected):
    """Atomic replacement after rechecking the inactive source, preserving ownership."""
    safe_path(path.parent, directory=True)
    if expected is None:
        if os.path.lexists(path):
            raise PreparationError("configuration_changed")
    else:
        actual, current = read_config(path)
        if actual != expected or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise PreparationError("configuration_changed")
    handle, name = tempfile.mkstemp(prefix=".playback-write-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            own_file(temporary, info)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_path(path.parent, directory=True)
    finally:
        temporary.unlink(missing_ok=True)


def write_marker(path, document, *, exclusive=False):
    data = (json.dumps(document, sort_keys=True) + "\n").encode()
    flags = os.O_WRONLY | os.O_NOFOLLOW | (os.O_CREAT | os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    sync_path(path.parent, directory=True)


def prepare(config_root):
    """One-shot offline preparation; interrupted runs require private inspection."""
    root = Path(os.path.abspath(config_root))
    try:
        originals, changes, library_count, plugin_info = preflight(root)
    except PreparationError:
        raise
    except (OSError, ValueError, ET.ParseError) as error:
        raise PreparationError("invalid_configuration") from error

    try:
        backup = Path(tempfile.mkdtemp(prefix=".playback-config-backup-", dir=root.parent))
    except OSError as error:
        raise PreparationError("backup_unavailable") from error
    marker = {"state": "incomplete", "backup_directory": backup.name, "format": 1}
    try:
        for path, (data, info) in originals.items():
            saved = backup / "originals" / path.relative_to(root)
            saved.parent.mkdir(parents=True, exist_ok=True)
            with saved.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            own_file(saved, info)
            shutil.copystat(path, saved, follow_symlinks=False)
            sync_path(saved)
        sync_backup_directories(backup)
        write_marker(root / MARKER, marker, exclusive=True)
        if plugin_info is not None:
            plugins = root / "plugins"
            inspect_tree(plugins)
            os.rename(plugins, backup / "originals/plugins")
            plugins.mkdir(mode=stat.S_IMODE(plugin_info.st_mode))
            own_file(plugins, plugin_info)
            sync_path(plugins, directory=True)
            sync_path(backup / "originals", directory=True)
            sync_path(root, directory=True)

        tasks = root / "config/ScheduledTasks"
        defaults = safe_path(root / "config", directory=True)
        if not tasks.exists():
            tasks.mkdir(mode=0o700)
            os.chown(tasks, defaults.st_uid, defaults.st_gid)
            sync_path(tasks, directory=True)
            sync_path(tasks.parent, directory=True)
        default_file_info = originals[root / "config/system.xml"][1]
        for path, data in changes.items():
            original = originals.get(path)
            expected, info = original if original else (None, default_file_info)
            replace_file(path, data, info, expected)
        marker["state"] = "complete"
        write_marker(root / MARKER, marker)
    except (OSError, ValueError, PreparationError) as error:
        # Never erase evidence or reapply automatically after partial mutation.
        # A marker left incomplete (or unreadable after a power loss) blocks retry.
        raise PreparationError("preparation_incomplete") from error
    return {
        "prepared": True,
        "libraries": library_count,
        "scheduled_tasks_held": sum(path.parent == tasks for path in changes),
        "plugins_quarantined": plugin_info is not None,
        "backup_directory": backup.name,
        "runtime_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.config_root)
    except PreparationError as error:
        print(json.dumps({"prepared": False, "error": str(error)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
