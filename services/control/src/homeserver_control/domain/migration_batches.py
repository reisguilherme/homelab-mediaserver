"""Pure batch descriptions; declared evidence never grants operational authority."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum


def _validate_id(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("invalid identity")


def _validate_path(value: object) -> None:
    if (
        not isinstance(value, str) or not value or "\\" in value or ":" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("invalid relative path")


def _validate_sha256(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid lowercase SHA-256")


def _validate_tuple(value: object) -> None:
    if not isinstance(value, tuple):
        raise ValueError("an immutable tuple is required")


@dataclass(frozen=True)
class MigrationGeneration:
    generation_id: str
    source_id: str
    destination_id: str

    def __post_init__(self) -> None:
        for value in (self.generation_id, self.source_id, self.destination_id):
            _validate_id(value)
        if self.source_id == self.destination_id:
            raise ValueError("source and destination identities must differ")


@dataclass(frozen=True)
class MigrationGroup:
    """One declared physical inode and its complete set of relative hardlink paths."""

    group_id: str
    paths: tuple[str, ...]
    logical_bytes: int
    content_sha256: str
    dependency_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_id(self.group_id)
        _validate_tuple(self.paths)
        _validate_tuple(self.dependency_ids)
        for path in self.paths:
            _validate_path(path)
        for dependency_id in self.dependency_ids:
            _validate_id(dependency_id)
        if not self.paths or len(set(self.paths)) != len(self.paths):
            raise ValueError("paths must be nonempty and unique")
        if len(set(self.dependency_ids)) != len(self.dependency_ids):
            raise ValueError("dependency identities must be unique")
        if type(self.logical_bytes) is not int or self.logical_bytes < 0:
            raise ValueError("logical bytes must be a nonnegative integer")
        _validate_sha256(self.content_sha256)


class EvidencePhase(StrEnum):
    STAGING_VERIFIED = "staging_verified"
    PUBLICATION_VERIFIED = "publication_verified"
    NATIVE_READBACK_VERIFIED = "native_readback_verified"


@dataclass(frozen=True)
class DeclaredReceipt:
    """Unauthenticated declaration; a trusted future adapter must establish truth."""

    generation_id: str
    source_id: str
    destination_id: str
    group_id: str
    manifest_digest: str
    phase: EvidencePhase

    def __post_init__(self) -> None:
        for value in (self.generation_id, self.source_id, self.destination_id, self.group_id):
            _validate_id(value)
        _validate_sha256(self.manifest_digest)
        if not isinstance(self.phase, EvidencePhase):
            raise ValueError("invalid evidence phase")


@dataclass(frozen=True)
class GroupEvidence:
    group_id: str
    manifest_digest: str
    staging_declared: bool = False
    publication_declared: bool = False
    native_readback_declared: bool = False


@dataclass(frozen=True)
class BatchPlan:
    selected_group_ids: tuple[str, ...]
    selected_paths: tuple[str, ...]
    blocked_reasons: tuple[str, ...]
    evidence_states: tuple[GroupEvidence, ...]
    total_logical_bytes: int
    execution_allowed: bool = field(default=False, init=False)
    source_cleanup_allowed: bool = field(default=False, init=False)
    global_scan_allowed: bool = field(default=False, init=False)
    deletion_allowed: bool = field(default=False, init=False)


def manifest_digest(group: MigrationGroup) -> str:
    """SHA-256 of versioned canonical JSON, binding all declared inode fields."""
    if not isinstance(group, MigrationGroup):
        raise ValueError("a migration group is required")
    payload = {
        "schema_version": 1,
        "group_id": group.group_id,
        "paths": sorted(group.paths),
        "logical_bytes": group.logical_bytes,
        "content_sha256": group.content_sha256,
        "dependency_ids": sorted(group.dependency_ids),
    }
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _inventory_reasons(groups: tuple[MigrationGroup, ...]) -> set[str]:
    reasons: set[str] = set()
    by_id: dict[str, MigrationGroup] = {}
    owners: dict[str, set[str]] = {}
    duplicate_ids = False
    for group in groups:
        if group.group_id in by_id:
            reasons.add(f"duplicate_group_id:{group.group_id}")
            duplicate_ids = True
        by_id[group.group_id] = group
        for path in group.paths:
            owners.setdefault(path, set()).add(group.group_id)
    for group in groups:
        for dependency_id in group.dependency_ids:
            if dependency_id not in by_id:
                reasons.add(f"missing_dependency:{group.group_id}:{dependency_id}")
    for path, group_ids in owners.items():
        if len(group_ids) > 1:
            reasons.add(f"path_conflict:{path}")
        parts = path.split("/")
        for depth in range(1, len(parts)):
            ancestor = "/".join(parts[:depth])
            if ancestor in owners:
                reasons.add(f"path_conflict:{ancestor}:{path}")

    # Kahn's algorithm checks the entire inventory without recursive stack limits.
    if not duplicate_ids:
        pending_counts = {group_id: 0 for group_id in by_id}
        dependents: dict[str, list[str]] = {group_id: [] for group_id in by_id}
        for group in groups:
            for dependency_id in group.dependency_ids:
                if dependency_id in by_id:
                    pending_counts[group.group_id] += 1
                    dependents[dependency_id].append(group.group_id)
        pending = [group_id for group_id, count in pending_counts.items() if count == 0]
        visited = 0
        while pending:
            visited += 1
            for dependent in dependents[pending.pop()]:
                pending_counts[dependent] -= 1
                if pending_counts[dependent] == 0:
                    pending.append(dependent)
        if visited != len(by_id):
            reasons.add("dependency_cycle:inventory")
    return reasons


def _group_evidence(
    group: MigrationGroup,
    phases_by_binding: dict[tuple[str, str], set[EvidencePhase]],
) -> GroupEvidence:
    digest = manifest_digest(group)
    phases = phases_by_binding.get((group.group_id, digest), set())
    staging = EvidencePhase.STAGING_VERIFIED in phases
    publication = staging and EvidencePhase.PUBLICATION_VERIFIED in phases
    readback = publication and EvidencePhase.NATIVE_READBACK_VERIFIED in phases
    return GroupEvidence(group.group_id, digest, staging, publication, readback)


def plan_batch(
    generation: MigrationGeneration,
    groups: tuple[MigrationGroup, ...],
    selected_paths: tuple[str, ...],
    receipts: tuple[DeclaredReceipt, ...] = (),
) -> BatchPlan:
    """Describe a batch, never execution/readiness/capacity or permission to mutate.

    Structural errors raise ValueError. Any inventory or selection conflict blocks
    the whole batch. Receipt bindings only describe declarations, not real proof.
    """
    if not isinstance(generation, MigrationGeneration):
        raise ValueError("a migration generation is required")
    for values, element_type in ((groups, MigrationGroup), (receipts, DeclaredReceipt)):
        _validate_tuple(values)
        if any(not isinstance(value, element_type) for value in values):
            raise ValueError("invalid typed input")
    _validate_tuple(selected_paths)
    for path in selected_paths:
        _validate_path(path)
    reasons = _inventory_reasons(groups)
    all_paths = {path for group in groups for path in group.paths}
    for path in selected_paths:
        if path not in all_paths:
            reasons.add(f"unknown_selected_path:{path}")
    if reasons:
        return BatchPlan((), (), tuple(sorted(reasons)), (), 0)

    by_id = {group.group_id: group for group in groups}
    requested = set(selected_paths)
    selected_ids = {group.group_id for group in groups if requested.intersection(group.paths)}
    pending = list(selected_ids)
    while pending:
        for dependency_id in by_id[pending.pop()].dependency_ids:
            if dependency_id not in selected_ids:
                selected_ids.add(dependency_id)
                pending.append(dependency_id)
    selected_groups = tuple(by_id[group_id] for group_id in sorted(selected_ids))
    phases_by_binding: dict[tuple[str, str], set[EvidencePhase]] = {}
    for receipt in receipts:
        if (receipt.generation_id, receipt.source_id, receipt.destination_id) == (
            generation.generation_id, generation.source_id, generation.destination_id,
        ):
            phases_by_binding.setdefault((receipt.group_id, receipt.manifest_digest), set()).add(
                receipt.phase,
            )
    return BatchPlan(
        selected_group_ids=tuple(sorted(selected_ids)),
        selected_paths=tuple(sorted(path for group in selected_groups for path in group.paths)),
        blocked_reasons=(),
        evidence_states=tuple(
            _group_evidence(group, phases_by_binding) for group in selected_groups
        ),
        total_logical_bytes=sum(group.logical_bytes for group in selected_groups),
    )
