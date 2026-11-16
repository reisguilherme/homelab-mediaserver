#!/usr/bin/env python3
"""Inspect explicit stopped core copies; never mutate native state or admit apps.

This is evidence collection, not a restore verifier or containment receipt.
Only the listed database/config files are opened. Private values are not emitted.
Requires Linux/Python 3.12; SQLite sidecars are inspected in private temporary copies.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

APPLICATIONS = {
    "radarr": ("radarr.db", ("config.xml",)),
    "sonarr": ("sonarr.db", ("config.xml",)),
    "prowlarr": ("prowlarr.db", ("config.xml",)),
    "bazarr": ("db/bazarr.db", ("config/config.yaml",)),
    "seerr": ("db/db.sqlite3", ("settings.json",)),
    "control": ("control.sqlite", ()),
    "qbittorrent": (None, ("qBittorrent/qBittorrent.conf", "qBittorrent/qBittorrent-data.conf")),
}
SIDECARS = ("-wal", "-shm", "-journal")
MAX_DATABASE_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 8 * 1024**3
MAX_CONFIG_BYTES = 10 * 1024**2
MAX_SCHEMA_TABLES = 512
MAX_OBSERVATION_ROWS = 10_000
FLAGS = {
    "Enable",
    "Enabled",
    "EnableRss",
    "EnableAutomaticSearch",
    "EnableInteractiveSearch",
    "OnGrab",
    "OnImport",
    "OnUpgrade",
    "OnRename",
    "OnHealthIssue",
    "OnDownloadFailure",
    "OnMovieAdded",
    "OnMovieDelete",
    "OnMovieFileDelete",
    "OnMovieFileDeleteForUpgrade",
    "OnSeriesAdd",
    "OnSeriesDelete",
    "OnEpisodeFileDelete",
    "OnEpisodeFileDeleteForUpgrade",
    "OnApplicationUpdate",
    "OnManualInteractionRequired",
}
OBSERVATION_TABLES = {
    "scheduledtasks",
    "downloadclients",
    "notifications",
    "rootfolders",
    "versioninfo",
    "indexers",
    "config",
    "migrations",
    "schema_migrations",
    "alembic_version",
}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
# Exact public task names only. A namespace-shaped private string is not safe.
# The upstream TaskManager sources are linked in docs/runbooks/core-migration.md.
# This display allowlist establishes no version or native containment contract.
TASK_TYPES = {
    "NzbDrone.Core.Applications.ApplicationIndexerSyncCommand",
    "NzbDrone.Core.Backup.BackupCommand",
    "NzbDrone.Core.Caching.ClearExpiredCacheCommand",
    "NzbDrone.Core.DataAugmentation.Scene.UpdateSceneMappingCommand",
    "NzbDrone.Core.Download.RefreshMonitoredDownloadsCommand",
    "NzbDrone.Core.HealthCheck.CheckHealthCommand",
    "NzbDrone.Core.History.CleanUpHistoryCommand",
    "NzbDrone.Core.Housekeeping.HousekeepingCommand",
    "NzbDrone.Core.ImportLists.ImportListSyncCommand",
    "NzbDrone.Core.Indexers.RssSyncCommand",
    "NzbDrone.Core.IndexerVersions.IndexerDefinitionUpdateCommand",
    "NzbDrone.Core.MediaFiles.Commands.CleanUpRecycleBinCommand",
    "NzbDrone.Core.Messaging.Commands.MessagingCleanupCommand",
    "NzbDrone.Core.Movies.Commands.RefreshCollectionsCommand",
    "NzbDrone.Core.Movies.Commands.RefreshMovieCommand",
    "NzbDrone.Core.Tv.Commands.RefreshSeriesCommand",
    "NzbDrone.Core.Update.Commands.ApplicationCheckUpdateCommand",
    "NzbDrone.Core.Update.Commands.ApplicationUpdateCheckCommand",
}


class AuditError(RuntimeError):
    """Fixed, non-sensitive failure codes only."""


def require(condition, code):
    if not condition:
        raise AuditError(code)


def absolute_path(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "absolute_path_required")
    return path


@contextmanager
def directory(path):
    """Resolve each component with O_NOFOLLOW; never follow a source/output alias."""
    path = absolute_path(path)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = new
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def source_file(root_fd, relative, *, optional=False):
    descriptor = os.dup(root_fd)
    try:
        parts = relative.split("/")
        for part in parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = new
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
    except FileNotFoundError:
        if not optional:
            raise AuditError("required_source_missing") from None
        yield None
    else:
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "unsafe_source_type")
            yield stream
    finally:
        os.close(descriptor)


def identity(stream):
    info = os.fstat(stream.fileno())
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_nlink,
    )


def copy_and_hash(stream, limit, destination=None):
    before = identity(stream)
    require(before[2] <= limit, "source_size_limit")
    digest = hashlib.sha256()
    count = 0
    while chunk := stream.read(1024**2):
        count += len(chunk)
        require(count <= limit, "source_size_limit")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    require(count == before[2] and identity(stream) == before, "source_changed")
    return {"bytes": count, "sha256": digest.hexdigest()}


def quoted(name):
    require(isinstance(name, str) and IDENTIFIER.fullmatch(name), "unsupported_schema_identifier")
    return '"' + name + '"'


def path_class(value):
    if not isinstance(value, str) or any(part in {".", ".."} for part in value.split("/")):
        return "other"
    for prefix, classification in (
        ("/data/media", "container_media"),
        ("/srv/data/media", "legacy_media"),
        ("/srv/external3/homeserver/media", "direct_ext4_media"),
    ):
        if value == prefix or value.startswith(prefix + "/"):
            return classification
    return "other"


def observe(connection, table, columns):
    """Describe actual columns; never infer that any observed flag is sufficient containment."""
    name = quoted(table)
    rows = connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
    result = {"rows": rows, "flags": {}}
    require(rows <= MAX_OBSERVATION_ROWS, "observation_row_limit")
    for column in sorted(set(columns) & FLAGS):
        field = quoted(column)
        zero, one = connection.execute(
            f"SELECT coalesce(sum(typeof({field})='integer' AND {field}=0), 0), "
            f"coalesce(sum(typeof({field})='integer' AND {field}=1), 0) FROM {name}"
        ).fetchone()
        result["flags"][column] = {"zero": zero, "one": one, "other": rows - zero - one}
    if table.casefold() == "rootfolders" and "Path" in columns:
        counts = {}
        for (value,) in connection.execute(f'SELECT "Path" FROM {name}'):
            key = path_class(value)
            counts[key] = counts.get(key, 0) + 1
        result["path_classes"] = counts
    if table.casefold() == "scheduledtasks":
        if "TypeName" in columns:
            recognized, unknown = set(), 0
            for (value,) in connection.execute(f'SELECT "TypeName" FROM {name}'):
                if isinstance(value, str) and value in TASK_TYPES:
                    recognized.add(value)
                else:
                    unknown += 1
            result["task_types"] = sorted(recognized)
            result["unrecognized_task_types"] = unknown
        if "Interval" in columns:
            result["positive_interval_rows"] = connection.execute(
                f'SELECT count(*) FROM {name} WHERE typeof("Interval")=\'integer\' AND "Interval">0'
            ).fetchone()[0]
    if table.casefold() == "versioninfo" and "Version" in columns:
        result["numeric_versions"] = [
            row[0]
            for row in connection.execute(
                f'SELECT DISTINCT "Version" FROM {name} WHERE typeof("Version")=\'integer\' '
                'ORDER BY "Version"'
            )
        ]
    return result


def inspect_database(path):
    deadline = time.monotonic() + 120
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=1)) as connection:
        connection.enable_load_extension(False)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 16 * 1024**2)
        connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 512)
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        require(
            connection.execute("PRAGMA quick_check").fetchall() == [("ok",)], "sqlite_integrity"
        )
        tables = connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name LIMIT ?",
            (MAX_SCHEMA_TABLES + 1,),
        ).fetchall()
        require(len(tables) <= MAX_SCHEMA_TABLES, "schema_size_limit")
        schema, observations = {}, {}
        for (table,) in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_xinfo({quoted(table)})")]
            for column in columns:
                quoted(column)
            schema[table] = columns
            if table.casefold() in OBSERVATION_TABLES:
                observations[table] = observe(connection, table, columns)
        return {
            "integrity": "ok",
            "check": "quick_check",
            "schema": schema,
            "observations": observations,
        }


def snapshot_database(root_fd, relative, scratch, budget):
    """Hold every selected source open and detect changed, added, replaced sidecars."""
    with ExitStack() as stack:
        sources, identities, fingerprints = {}, {}, {}
        for suffix in ("", *SIDECARS):
            stream = stack.enter_context(
                source_file(root_fd, relative + suffix, optional=bool(suffix))
            )
            sources[suffix] = stream
            identities[suffix] = identity(stream) if stream else None
        for suffix, stream in sources.items():
            if stream is None:
                continue
            target = scratch / ("database.sqlite" + suffix)
            with target.open("xb") as output:
                os.chmod(target, 0o600)
                fingerprints[suffix] = copy_and_hash(stream, MAX_DATABASE_BYTES, output)
            budget[0] += fingerprints[suffix]["bytes"]
            require(budget[0] <= MAX_TOTAL_BYTES, "total_size_limit")
        for suffix, original in sources.items():
            with source_file(root_fd, relative + suffix, optional=bool(suffix)) as current:
                require(
                    (identity(current) if current else None) == identities[suffix], "source_changed"
                )
            if original:
                require(identity(original) == identities[suffix], "source_changed")
        result = inspect_database(scratch / "database.sqlite")
    result.update(
        relative_path=relative,
        source=fingerprints[""],
        sidecars={key: value for key, value in fingerprints.items() if key},
    )
    return result


def inspect(state_root, applications):
    require(
        applications
        and len(set(applications)) == len(applications)
        and all(app in APPLICATIONS for app in applications),
        "application_selection",
    )
    report = {
        "version": 1,
        "purpose": "offline_core_state_inspection",
        "startup_allowed": False,
        "native_containment_verified": False,
        "restore_provenance_verified": False,
        "image_versions_verified": False,
        "source_scope": "explicit_restored_copy_files_only",
        "applications": {},
    }
    budget = [0]
    try:
        with directory(state_root) as root_fd:
            for app in applications:
                db, configs = APPLICATIONS[app]
                with source_directory(root_fd, app) as app_fd:
                    item = {"database": None, "configs": {}, "native_config_review": "pending"}
                    if db:
                        with tempfile.TemporaryDirectory(
                            prefix="homeserver-core-audit-"
                        ) as temporary:
                            item["database"] = snapshot_database(
                                app_fd, db, Path(temporary), budget
                            )
                    for relative in configs:
                        with source_file(app_fd, relative, optional=True) as source:
                            evidence = {"present": source is not None}
                            if source:
                                evidence.update(copy_and_hash(source, MAX_CONFIG_BYTES))
                            item["configs"][relative] = evidence
                    if not db:
                        require(
                            any(value["present"] for value in item["configs"].values()),
                            "required_config_missing",
                        )
                    report["applications"][app] = item
    except (OSError, sqlite3.Error, UnicodeError, ValueError):
        raise AuditError("source_or_sqlite_inspection_failed") from None
    return report


@contextmanager
def source_directory(root_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def write_report(path, report, state_root):
    path, state_root = absolute_path(path), absolute_path(state_root)
    require(path != state_root and state_root not in path.parents, "output_inside_source")
    try:
        with directory(path.parent) as parent_fd:
            info = os.fstat(parent_fd)
            require(
                info.st_uid == os.geteuid() and not info.st_mode & 0o077,
                "output_parent_not_private",
            )
            fd = os.open(
                path.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(report, output, indent=2, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.fsync(parent_fd)
            except BaseException:
                os.unlink(path.name, dir_fd=parent_fd)
                raise
    except OSError:
        raise AuditError("output_unavailable") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument(
        "--application", action="append", choices=tuple(APPLICATIONS), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = inspect(args.state_root, args.application)
        write_report(args.output, report, args.state_root)
    except AuditError as error:
        print(f"core_state_audit: {error}", file=sys.stderr)
        return 2
    print("core_state_audit: inspected; startup remains denied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
