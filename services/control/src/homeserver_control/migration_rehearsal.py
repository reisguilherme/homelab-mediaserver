"""Executable Linux fixture only; no runtime configuration or production I/O."""

import hashlib
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory

from homeserver_control.adapters.migration_inventory import (
    ContentDeclaration,
    GroupDependencies,
    InventoryFile,
    InventoryGroup,
    inspect_inventory,
    plan_inventory_batch,
)
from homeserver_control.domain.migration_batches import MigrationGeneration


class RehearsalScenario(StrEnum):
    SUCCESS = "success"
    STAGING_CORRUPTION = "staging_corruption"
    PUBLICATION_CORRUPTION = "publication_corruption"
    DESTINATION_COLLISION = "destination_collision"
    SOURCE_MUTATION = "source_mutation"
    UNACCOUNTED_HARDLINK = "unaccounted_hardlink"
    SYMLINK = "symlink"
    INTERRUPTION = "interruption"
    CONTAINMENT_LOSS = "containment_loss"


@dataclass(frozen=True)
class RehearsalReport:
    scenario: str
    completed: bool
    blocked_reasons: tuple[str, ...]
    selected_groups: int
    selected_paths: int
    published_paths: int
    staging_verified: bool
    publication_verified: bool
    fixture_readback_verified: bool
    sources_preserved: bool
    copied_inode_separation: bool
    alias_topology_verified: bool
    equal_content_inode_separation: bool
    unselected_absent: bool
    restricted_scan_simulated: bool
    rejected_scan_requests: int
    partial_publication_retained: bool
    scope: str = field(default="synthetic_fixture", init=False)
    containment_kind: str = field(default="simulated", init=False)
    production_ready: bool = field(default=False, init=False)
    execution_allowed: bool = field(default=False, init=False)
    source_cleanup_allowed: bool = field(default=False, init=False)
    global_scan_allowed: bool = field(default=False, init=False)
    deletion_allowed: bool = field(default=False, init=False)


class _Held(RuntimeError):
    pass


_ACTORS = ("jellyfin", "sonarr", "radarr")


@dataclass
class _FixtureConsumers:
    """In-memory stand-ins only, with an explicit gate for all three consumers."""

    contained: set[str] = field(default_factory=lambda: set(_ACTORS))

    def require_containment(self) -> None:
        if self.contained != set(_ACTORS):
            raise _Held("simulated_containment_lost")

    def scan(
        self,
        actor: str,
        paths: tuple[str, ...],
        expected: tuple[str, ...],
        *,
        verified: bool,
        global_scan: bool = False,
        destructive: bool = False,
    ) -> bool:
        return (
            self.contained == set(_ACTORS)
            and actor in _ACTORS
            and verified
            and not global_scan
            and not destructive
            and paths == expected
        )


def _fingerprint(info: os.stat_result) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
    )


def _hash(path: Path) -> str:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise _Held("non_regular_fixture_file")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        if _fingerprint(os.fstat(stream.fileno())) != _fingerprint(before):
            raise _Held("fixture_changed_during_hash")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if _fingerprint(os.fstat(stream.fileno())) != _fingerprint(before) or _fingerprint(
            path.lstat()
        ) != _fingerprint(before):
            raise _Held("fixture_changed_during_hash")
    return digest


def _corrupt_same_size(path: Path) -> None:
    info = path.lstat()
    content = path.read_bytes()
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))


def _metadata(pool: str, relative: str, info: os.stat_result) -> dict:
    return dict(
        pool=pool,
        relative=relative,
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        allocated=info.st_blocks * 512,
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
        uid=info.st_uid,
        gid=info.st_gid,
        nlink=info.st_nlink,
    )


def _collect(roots: dict[str, Path]) -> dict:
    inventory = dict(
        version=1,
        inventory_generation="fixture-1",
        complete=True,
        files=[],
        directories=[],
        special=[],
        collisions=[],
    )
    for pool, root in roots.items():
        for current, directories, files in os.walk(root, followlinks=False):
            for name in sorted([*directories, *files]):
                path = Path(current) / name
                info = path.lstat()
                record = _metadata(pool, path.relative_to(root).as_posix(), info)
                if stat.S_ISREG(info.st_mode):
                    inventory["files"].append(record)
                elif stat.S_ISDIR(info.st_mode):
                    inventory["directories"].append(record)
                else:
                    inventory["special"].append(record)
    return inventory


def _snapshot(root: Path) -> tuple:
    result = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            content = _hash(path)
        elif stat.S_ISLNK(info.st_mode):
            content = os.readlink(path)
        else:
            continue
        result.append(
            (
                path.relative_to(root).as_posix(),
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_mode,
                info.st_uid,
                info.st_gid,
                info.st_nlink,
                content,
            )
        )
    return tuple(result)


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _Held("non_regular_fixture_file")
    return info.st_dev, info.st_ino


def _source_matches(item: InventoryFile, path: Path, expected_hash: str) -> bool:
    info = path.lstat()
    return (
        stat.S_ISREG(info.st_mode)
        and _metadata(item.pool, item.relative, info)
        == dict(
            pool=item.pool,
            relative=item.relative,
            device=item.device,
            inode=item.inode,
            size=item.size,
            allocated=item.allocated,
            mtime_ns=item.mtime_ns,
            mode=item.mode,
            uid=item.uid,
            gid=item.gid,
            nlink=item.nlink,
        )
        and _hash(path) == expected_hash
    )


def _verify_tree(
    root: Path,
    groups: tuple[InventoryGroup, ...],
    hashes: dict[str, str],
    source: Path,
    *,
    link_multiplier: int,
) -> tuple[bool, bool, bool]:
    identities = {}
    separated = True
    topology = True
    for group in groups:
        alias_ids = set()
        for item in group.files:
            path = root / item.relative
            info = path.lstat()
            alias_ids.add(_identity(path))
            if _hash(path) != hashes[group.group_id] or info.st_size != item.size:
                raise _Held("fixture_content_mismatch")
            if stat.S_IMODE(info.st_mode) != item.mode or info.st_mtime_ns != item.mtime_ns:
                raise _Held("fixture_metadata_mismatch")
            topology &= info.st_nlink == len(group.files) * link_multiplier
            separated &= _identity(path) != _identity(source / item.relative)
        topology &= len(alias_ids) == 1
        identities[group.group_id] = next(iter(alias_ids))
    distinct = len(set(identities.values())) == len(groups)
    if not (topology and separated and distinct):
        raise _Held("fixture_topology_mismatch")
    return separated, topology, distinct


def run_rehearsal(scenario: RehearsalScenario = RehearsalScenario.SUCCESS) -> RehearsalReport:
    """Run one named scenario inside a fresh private Linux /tmp directory.

    Accepts an enum only, no paths/configuration/resume or apply controls. Hashes,
    lstat identities, staging/publication checks and journal facts belong solely
    to this run. Publication uses exclusive hardlinks after verified staging.
    Scan containment is a simulated exact-list gate; no application is contacted.
    Returns aggregate facts after source recheck, then removes the entire fixture.
    """
    if not isinstance(scenario, RehearsalScenario):
        raise ValueError("a named RehearsalScenario is required")
    if sys.platform != "linux":
        raise ValueError("the synthetic rehearsal requires Linux")
    with TemporaryDirectory(prefix="homeserver-rehearsal-", dir="/tmp") as directory:
        base = Path(directory)
        source, native, staging = (base / name for name in ("source", "native", "staging"))
        for root in (source, native, staging):
            root.mkdir()
        (source / "library").mkdir()
        (source / "incoming").mkdir()
        video = source / "library/video.mkv"
        video.write_bytes(b"synthetic video\n")
        os.link(video, source / "incoming/video.mkv")
        (source / "library/video.srt").write_bytes(b"synthetic subtitle\n")
        (source / "library/equal.mkv").write_bytes(b"synthetic video\n")
        (source / "unselected.mkv").write_bytes(b"unselected content\n")
        (native / "native-only.mkv").write_bytes(b"native fixture\n")
        if scenario == RehearsalScenario.UNACCOUNTED_HARDLINK:
            os.link(video, base / "unaccounted-alias")
        if scenario == RehearsalScenario.SYMLINK:
            (source / "symbolic-link").symlink_to("library/video.mkv")
        original = _snapshot(source)
        inventory = _collect({"old": source, "native": native})
        inspection = inspect_inventory(
            inventory,
            pool_roots={"old": "source-root", "native": "native-root"},
            destination_pool="native",
        )
        source_groups = tuple(group for group in inspection.groups if not group.native)
        hashes = {
            group.group_id: _hash(source / group.files[0].relative) for group in source_groups
        }
        content = tuple(
            ContentDeclaration(group.group_id, inspection.inventory_digest, hashes[group.group_id])
            for group in source_groups
        )
        video_group = next(g for g in source_groups if "library/video.mkv" in g.paths)
        subtitle_group = next(g for g in source_groups if "library/video.srt" in g.paths)
        result = plan_inventory_batch(
            inspection,
            MigrationGeneration(
                inspection.inventory_generation, inspection.source_id, inspection.destination_id
            ),
            ("library/video.mkv", "library/equal.mkv"),
            content,
            (GroupDependencies(video_group.group_id, (subtitle_group.group_id,)),),
        )
        plan = result.plan
        groups = tuple(g for g in source_groups if g.group_id in plan.selected_group_ids)
        reasons = list(plan.blocked_reasons)
        published: list[str] = []
        staging_verified = publication_verified = readback = False
        separated = topology = distinct = absent = scan = retained = False
        rejected = 0
        consumers = _FixtureConsumers()
        try:
            if reasons:
                raise _Held("inventory_held")
            if scenario == RehearsalScenario.SOURCE_MUTATION:
                video.write_bytes(b"mutated synthetic content\n")
            for group in source_groups:
                for item in group.files:
                    if not _source_matches(item, source / item.relative, hashes[group.group_id]):
                        raise _Held("source_changed")
            for group in groups:
                first = group.files[0]
                target = staging / first.relative
                target.parent.mkdir(parents=True, exist_ok=True)
                # copyfile creates an independent inode; never hardlink from source.
                with target.open("xb") as output, (source / first.relative).open("rb") as input_:
                    shutil.copyfileobj(input_, output)
                target.chmod(first.mode)
                os.utime(target, ns=(first.mtime_ns, first.mtime_ns))
                for item in group.files[1:]:
                    alias = staging / item.relative
                    alias.parent.mkdir(parents=True, exist_ok=True)
                    os.link(target, alias)
            if scenario == RehearsalScenario.STAGING_CORRUPTION:
                _corrupt_same_size(staging / plan.selected_paths[0])
            separated, topology, distinct = _verify_tree(
                staging, groups, hashes, source, link_multiplier=1
            )
            if _snapshot(source) != original:
                raise _Held("source_changed")
            staging_verified = True
            if scenario == RehearsalScenario.DESTINATION_COLLISION:
                collision = native / plan.selected_paths[0]
                collision.parent.mkdir(parents=True, exist_ok=True)
                collision.write_bytes(b"existing destination\n")
            for relative in plan.selected_paths:
                consumers.require_containment()
                target = native / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(staging / relative, target)
                published.append(relative)
                if scenario == RehearsalScenario.CONTAINMENT_LOSS:
                    consumers.contained.remove("jellyfin")
                if scenario == RehearsalScenario.INTERRUPTION:
                    raise _Held("interrupted_publication")
            consumers.require_containment()
            if scenario == RehearsalScenario.PUBLICATION_CORRUPTION:
                _corrupt_same_size(native / plan.selected_paths[0])
            separated, topology, distinct = _verify_tree(
                native, groups, hashes, source, link_multiplier=2
            )
            publication_verified = True
            # Separate reread of the precise publication list, never native app proof.
            for group in groups:
                for item in group.files:
                    if _hash(native / item.relative) != hashes[group.group_id] or _identity(
                        native / item.relative
                    ) != _identity(staging / item.relative):
                        raise _Held("fixture_readback_mismatch")
            readback = True
            absent = not (native / "unselected.mkv").exists()
            if not absent:
                raise _Held("unselected_publication")
            consumers.require_containment()

            def simulated_scan(paths: tuple[str, ...], *, actor="jellyfin", **options):
                verified = (
                    readback
                    and publication_verified
                    and absent
                    and len(published) == len(plan.selected_paths)
                )
                return consumers.scan(
                    actor, paths, plan.selected_paths, verified=verified, **options
                )

            rejected = sum(
                not allowed
                for allowed in (
                    simulated_scan(plan.selected_paths, global_scan=True),
                    simulated_scan(plan.selected_paths, destructive=True),
                    simulated_scan((*plan.selected_paths, "unselected.mkv")),
                    simulated_scan(plan.selected_paths, actor="unknown"),
                )
            )
            scan = all(simulated_scan(plan.selected_paths, actor=actor) for actor in _ACTORS)
            if not scan:
                raise _Held("simulated_containment_lost")
        except _Held as exc:
            if str(exc) not in reasons:
                reasons.append(str(exc))
        except OSError as exc:
            reasons.append(
                "destination_collision"
                if isinstance(exc, FileExistsError)
                else "fixture_io_failure"
            )
        retained = 0 < len(published) < len(plan.selected_paths) and all(
            (native / path).exists() and (staging / path).exists() for path in published
        )
        preserved = _snapshot(source) == original
        if not preserved:
            reasons.append("source_not_preserved")
            scan = False
        return RehearsalReport(
            scenario.value,
            not reasons and scan,
            tuple(sorted(set(reasons))),
            len(groups),
            len(plan.selected_paths),
            len(published),
            staging_verified,
            publication_verified,
            readback,
            preserved,
            separated,
            topology,
            distinct,
            absent,
            scan,
            rejected,
            retained,
        )
