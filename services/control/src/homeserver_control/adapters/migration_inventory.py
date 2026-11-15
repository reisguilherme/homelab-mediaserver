"""Offline metadata declarations, never filesystem evidence or authority to act."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from homeserver_control.domain.migration_batches import (
    BatchPlan,
    MigrationGeneration,
    MigrationGroup,
    plan_batch,
)


def _identity(value: object) -> None:
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("invalid logical identity")


def _path(value: object) -> None:
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("invalid relative path")


def _sha(value: object) -> None:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid lowercase SHA-256")


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    )


def _json_keys(value: object) -> None:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ValueError("JSON metadata keys must be strings")
        for child in value.values():
            _json_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _json_keys(child)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class InventoryFile:
    pool: str
    relative: str
    device: int
    inode: int
    size: int
    allocated: int
    mtime_ns: int
    mode: int
    uid: int
    gid: int
    nlink: int


@dataclass(frozen=True)
class InventoryGroup:
    group_id: str
    files: tuple[InventoryFile, ...]
    native: bool

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.relative for item in self.files)


@dataclass(frozen=True)
class InventoryInspection:
    """Immutable snapshot; identities, devices and completeness remain declarations."""

    inventory_generation: str
    source_id: str
    destination_id: str
    inventory_digest: str
    groups: tuple[InventoryGroup, ...]
    blocked_reasons: tuple[str, ...]
    pool_roots: tuple[tuple[str, str], ...]
    destination_pool: str
    metadata_json: str


@dataclass(frozen=True)
class ContentDeclaration:
    """A content hash bound to the whole inspection, with no receipt semantics."""

    group_id: str
    inventory_digest: str
    content_sha256: str

    def __post_init__(self) -> None:
        _identity(self.group_id)
        _sha(self.inventory_digest)
        _sha(self.content_sha256)


@dataclass(frozen=True)
class GroupDependencies:
    group_id: str
    dependency_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identity(self.group_id)
        if type(self.dependency_ids) is not tuple:
            raise ValueError("dependencies must be an immutable tuple")
        for value in self.dependency_ids:
            _identity(value)
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("duplicate dependency")


@dataclass(frozen=True)
class HashCoverage:
    required_groups: int
    declared_groups: int
    missing_group_ids: tuple[str, ...]


@dataclass(frozen=True)
class InventoryBatchPlan:
    coverage: HashCoverage
    plan: BatchPlan


_NUMBERS = ("device", "inode", "size", "allocated", "mtime_ns", "mode", "uid", "gid", "nlink")


def _record(record: object, roots: Mapping[str, str], *, complete: bool) -> dict:
    if not isinstance(record, Mapping) or not {"pool", "relative"} <= record.keys():
        raise ValueError("inventory record needs pool and relative")
    if set(record) - {"pool", "relative", *_NUMBERS, "kind", "target"}:
        raise ValueError("unknown inventory record field")
    if complete and set(record) != {"pool", "relative", *_NUMBERS}:
        raise ValueError("file metadata is incomplete")
    if type(record["pool"]) is not str or record["pool"] not in roots:
        raise ValueError("unknown inventory pool")
    _path(record["relative"])
    result = dict(record)
    for name in _NUMBERS:
        if name in result:
            value = result[name]
            if type(value) is not int or value < (1 if name == "nlink" else 0):
                raise ValueError(f"invalid integer metadata: {name}")
    if "mode" in result and result["mode"] > 0o7777:
        raise ValueError("mode must contain permission bits only")
    for name in ("kind", "target"):
        if name in result and type(result[name]) is not str:
            raise ValueError("invalid special metadata")
    return result


def inspect_inventory(
    inventory: Mapping[str, object],
    *,
    pool_roots: Mapping[str, str],
    destination_pool: str,
) -> InventoryInspection:
    """Inspect v1 in-memory metadata, including native and unselected files.

    pool_roots maps every pool name to a unique declared logical root identity;
    destination_pool selects the native root. No path is read. source_id is
    ``source-`` plus SHA-256 of the canonical source pool/root mapping. Neither
    these IDs nor device/inode declarations authenticate physical storage.
    File mode is permission-only, as in the inventory format. Structural/schema
    errors raise ValueError; whole-inventory ambiguities become blocking reasons.
    Optional incident is an opaque string label included in the metadata digest.
    metadata_json is canonical copied metadata, never a content hash or receipt.
    """
    if not isinstance(pool_roots, Mapping) or not pool_roots:
        raise ValueError("explicit pool roots required")
    roots = dict(pool_roots)
    for pool, root in roots.items():
        _identity(pool)
        _identity(root)
    if (
        type(destination_pool) is not str
        or destination_pool not in roots
        or len(set(roots.values())) != len(roots)
        or len(roots) < 2
    ):
        raise ValueError("destination and unique source roots required")
    fields = {
        "version",
        "inventory_generation",
        "complete",
        "files",
        "directories",
        "special",
        "collisions",
    }
    if (
        not isinstance(inventory, Mapping)
        or not fields <= inventory.keys()
        or set(inventory) - fields - {"incident"}
    ):
        raise ValueError("invalid inventory fields")
    if "incident" in inventory and type(inventory["incident"]) is not str:
        raise ValueError("incident must be an opaque string")
    if type(inventory["version"]) is not int or inventory["version"] != 1:
        raise ValueError("unsupported inventory version")
    _identity(inventory["inventory_generation"])
    if type(inventory["complete"]) is not bool:
        raise ValueError("complete must be a boolean")
    normalized = dict(inventory)
    for name in ("files", "directories", "special", "collisions"):
        if type(inventory[name]) not in (list, tuple):
            raise ValueError("inventory collections must be sequences")
        if name == "collisions":
            entries = []
            for entry in inventory[name]:
                if not isinstance(entry, Mapping) or not entry:
                    raise ValueError("invalid declared collision")
                _json_keys(entry)
                if "relative" in entry:
                    _path(entry["relative"])
                if "pools" in entry and (
                    type(entry["pools"]) not in (list, tuple)
                    or any(type(pool) is not str or pool not in roots for pool in entry["pools"])
                ):
                    raise ValueError("invalid collision pools")
                # Collision detail is opaque; only JSON primitive data is accepted.
                try:
                    encoded = _canonical(dict(entry))
                    entries.append(json.loads(encoded))
                except (TypeError, ValueError) as exc:
                    raise ValueError("invalid collision metadata") from exc
        else:
            entries = [_record(entry, roots, complete=name == "files") for entry in inventory[name]]
        normalized[name] = sorted(entries, key=_canonical)
    reasons = set()
    if not normalized["complete"]:
        reasons.add("incomplete_inventory")
    if normalized["special"]:
        reasons.add("special_entries")
    if normalized["collisions"]:
        reasons.add("declared_collisions")
    files = tuple(InventoryFile(**item) for item in normalized["files"])
    physical: dict[tuple[int, int], list[InventoryFile]] = {}
    path_counts: dict[str, int] = {}
    for item in files:
        physical.setdefault((item.device, item.inode), []).append(item)
        path_counts[item.relative] = path_counts.get(item.relative, 0) + 1
    directories = set()
    directory_entries = set()
    for entry in normalized["directories"]:
        key = (entry["pool"], entry["relative"])
        if key in directory_entries:
            reasons.add(f"duplicate_directory:{entry['relative']}")
        directory_entries.add(key)
        directories.add(entry["relative"])
    for path, count in path_counts.items():
        if count > 1:
            reasons.add(f"path_conflict:{path}")
        if path in directories:
            reasons.add(f"file_directory_conflict:{path}")
    for path in {*path_counts, *directories}:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            if ancestor in path_counts:
                reasons.add(f"ancestor_file:{ancestor}:{path}")
    groups = []
    for (device, inode), members in sorted(physical.items()):
        members.sort(key=lambda item: (item.relative, item.pool))
        first = members[0]
        group_id = f"inode-{device}-{inode}"

        def signature(item):
            return tuple(getattr(item, name) for name in _NUMBERS)

        if any(signature(item) != signature(first) for item in members):
            reasons.add(f"inode_signature_mismatch:{group_id}")
        if any(item.nlink != len(members) for item in members):
            reasons.add(f"unaccounted_hardlinks:{group_id}")
        roles = {item.pool == destination_pool for item in members}
        if len(roles) > 1:
            reasons.add(f"source_native_inode_overlap:{group_id}")
        groups.append(InventoryGroup(group_id, tuple(members), first.pool == destination_pool))
    source_roots = {pool: root for pool, root in roots.items() if pool != destination_pool}
    payload = {"inventory": normalized, "pool_roots": roots, "destination_pool": destination_pool}
    return InventoryInspection(
        normalized["inventory_generation"],
        "source-" + _digest(source_roots),
        roots[destination_pool],
        _digest(payload),
        tuple(groups),
        tuple(sorted(reasons)),
        tuple(sorted(roots.items())),
        destination_pool,
        _canonical(normalized),
    )


def plan_inventory_batch(
    inspection: InventoryInspection,
    generation: MigrationGeneration,
    selected_paths: tuple[str, ...],
    content_declarations: tuple[ContentDeclaration, ...],
    dependencies: tuple[GroupDependencies, ...] = (),
) -> InventoryBatchPlan:
    """Plan source aliases/dependencies with complete v1 content-hash coverage.

    generation must match inspection inventory_generation/source_id/destination_id.
    Each declaration binds a unique source group to inventory_digest and its actual
    declared content SHA-256. Native hashes are unnecessary and rejected. Optional
    dependencies refer only to source group IDs; cycles block in the pure planner.
    Malformed, duplicate, unknown or mismatched declarations raise ValueError.
    No receipt is accepted or synthesized; all planner operational flags stay false.
    """
    if not isinstance(inspection, InventoryInspection):
        raise ValueError("inspection required")
    # Reconstruct the immutable metadata binding rather than trusting hand-built outputs.
    expected = inspect_inventory(
        json.loads(inspection.metadata_json),
        pool_roots=dict(inspection.pool_roots),
        destination_pool=inspection.destination_pool,
    )
    if inspection != expected:
        raise ValueError("inspection metadata binding mismatch")
    if not isinstance(generation, MigrationGeneration) or (
        generation.generation_id,
        generation.source_id,
        generation.destination_id,
    ) != (inspection.inventory_generation, inspection.source_id, inspection.destination_id):
        raise ValueError("generation binding mismatch")
    for values, kind in (
        (content_declarations, ContentDeclaration),
        (dependencies, GroupDependencies),
    ):
        if type(values) is not tuple or any(not isinstance(item, kind) for item in values):
            raise ValueError("immutable typed declarations required")
    source = {group.group_id: group for group in inspection.groups if not group.native}
    hashes = {}
    for declaration in content_declarations:
        if (
            declaration.group_id not in source
            or declaration.group_id in hashes
            or declaration.inventory_digest != inspection.inventory_digest
        ):
            raise ValueError("unknown, duplicate or unbound content declaration")
        hashes[declaration.group_id] = declaration.content_sha256
    deps = {}
    for declaration in dependencies:
        if (
            declaration.group_id not in source
            or declaration.group_id in deps
            or any(group_id not in source for group_id in declaration.dependency_ids)
        ):
            raise ValueError("unknown or duplicate group dependencies")
        deps[declaration.group_id] = declaration.dependency_ids
    if type(selected_paths) is not tuple:
        raise ValueError("immutable selected paths required")
    for path in selected_paths:
        _path(path)
    missing = tuple(sorted(set(source) - hashes.keys()))
    coverage = HashCoverage(len(source), len(hashes), missing)
    reasons = set(inspection.blocked_reasons)
    if missing:
        reasons.add("incomplete_hash_coverage")
    if reasons:
        return InventoryBatchPlan(coverage, BatchPlan((), (), tuple(sorted(reasons)), (), 0))
    groups = tuple(
        MigrationGroup(
            group.group_id,
            group.paths,
            group.files[0].size,
            hashes[group.group_id],
            deps.get(group.group_id, ()),
        )
        for group in source.values()
    )
    return InventoryBatchPlan(coverage, plan_batch(generation, groups, selected_paths))
