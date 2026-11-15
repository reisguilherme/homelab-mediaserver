from copy import deepcopy
from dataclasses import FrozenInstanceError, replace

import pytest

from homeserver_control.adapters.migration_inventory import (
    ContentDeclaration,
    GroupDependencies,
    inspect_inventory,
    plan_inventory_batch,
)
from homeserver_control.domain.migration_batches import MigrationGeneration

ROOTS = {"old": "source-root", "native": "destination-root"}


def generation(inspection):
    return MigrationGeneration("fixture-1", inspection.source_id, "destination-root")


def file(relative="video", *, pool="old", inode=10, nlink=1, **changes):
    return dict(
        pool=pool,
        relative=relative,
        device=1,
        inode=inode,
        size=5,
        allocated=4096,
        mtime_ns=10,
        mode=0o640,
        uid=1000,
        gid=1000,
        nlink=nlink,
        **changes,
    )


def inventory(*files, **changes):
    return dict(
        version=1,
        inventory_generation="fixture-1",
        complete=True,
        files=list(files or (file(),)),
        directories=[],
        special=[],
        collisions=[],
        **changes,
    )


def inspect(data):
    return inspect_inventory(data, pool_roots=ROOTS, destination_pool="native")


def declarations(inspection):
    return tuple(
        ContentDeclaration(g.group_id, inspection.inventory_digest, "a" * 64)
        for g in inspection.groups
        if not g.native
    )


def test_inode_groups_expand_aliases_dependencies_and_keep_equal_content_separate():
    data = inventory(
        file("video", nlink=2),
        file("incoming/video", nlink=2),
        file("subtitle", inode=11),
        file("equal", inode=12),
        file("untouched", inode=13),
        file("native-only", pool="native", inode=14),
    )
    inspection = inspect(data)
    by_path = {g.paths[0]: g for g in inspection.groups}
    video = next(g for g in inspection.groups if "video" in g.paths)
    result = plan_inventory_batch(
        inspection,
        generation(inspection),
        ("video", "equal"),
        declarations(inspection),
        (GroupDependencies(video.group_id, (by_path["subtitle"].group_id,)),),
    )
    assert inspection.blocked_reasons == ()
    assert result.plan.selected_paths == ("equal", "incoming/video", "subtitle", "video")
    assert result.plan.total_logical_bytes == 15
    assert result.coverage.required_groups == 4
    assert result.coverage.declared_groups == 4
    assert result.coverage.missing_group_ids == ()
    assert len(result.plan.selected_group_ids) == 3
    assert not any(
        (
            result.plan.execution_allowed,
            result.plan.source_cleanup_allowed,
            result.plan.global_scan_allowed,
            result.plan.deletion_allowed,
        )
    )
    assert all(
        not e.staging_declared and not e.publication_declared and not e.native_readback_declared
        for e in result.plan.evidence_states
    )
    data["files"][0]["relative"] = "changed"
    assert "video" in video.paths
    with pytest.raises(FrozenInstanceError):
        inspection.groups = ()


def test_partial_hash_coverage_blocks_even_when_missing_group_is_unselected():
    inspection = inspect(inventory(file(), file("other", inode=11)))
    result = plan_inventory_batch(
        inspection, generation(inspection), ("video",), declarations(inspection)[:1]
    )
    assert result.coverage.required_groups == 2
    assert result.coverage.declared_groups == 1
    assert len(result.coverage.missing_group_ids) == 1
    assert "incomplete_hash_coverage" in result.plan.blocked_reasons
    assert result.plan.selected_paths == ()


@pytest.mark.parametrize(
    "kind",
    [
        "incomplete",
        "duplicate",
        "ancestor",
        "native_collision",
        "file_directory",
        "omitted_alias",
        "signature",
        "special",
        "collision",
        "cross_role_inode",
    ],
)
def test_whole_inventory_conflicts_block_before_hash_coverage(kind):
    data = inventory(file())
    if kind == "incomplete":
        data["complete"] = False
    elif kind == "duplicate":
        data["files"].append(file())
    elif kind == "ancestor":
        data["files"].extend([file("bad", inode=11), file("bad/child", inode=12)])
    elif kind == "native_collision":
        data["files"].append(file(pool="native", inode=12))
    elif kind == "file_directory":
        data["directories"] = [{"pool": "native", "relative": "video"}]
    elif kind == "omitted_alias":
        data["files"][0]["nlink"] = 2
    elif kind == "signature":
        data["files"][0]["nlink"] = 2
        second = file("alias", nlink=2)
        second["mtime_ns"] = 11
        data["files"].append(second)
    elif kind == "special":
        data["special"] = [{"pool": "old", "relative": "link", "mode": 0o777}]
    elif kind == "collision":
        data["collisions"] = [{"relative": "claimed", "pools": ["old", "native"]}]
    else:
        data["files"][0]["nlink"] = 2
        data["files"].append(file("native-alias", pool="native", nlink=2))
    inspection = inspect(data)
    assert inspection.blocked_reasons
    result = plan_inventory_batch(inspection, generation(inspection), ("video",), ())
    assert set(inspection.blocked_reasons) <= set(result.plan.blocked_reasons)
    assert result.plan.selected_paths == ()


def test_shared_directories_are_allowed_and_digest_binds_all_metadata_and_mapping():
    data = inventory(file())
    data["directories"] = [{"pool": p, "relative": "shared"} for p in ROOTS]
    inspection = inspect(data)
    assert not inspection.blocked_reasons
    for key in ("size", "allocated", "mtime_ns", "mode", "uid", "gid", "nlink"):
        changed = deepcopy(data)
        changed["files"][0][key] += 1
        assert inspect(changed).inventory_digest != inspection.inventory_digest
    changed = deepcopy(data)
    changed["inventory_generation"] = "fixture-2"
    assert inspect(changed).inventory_digest != inspection.inventory_digest
    assert (
        inspect_inventory(
            data,
            pool_roots={"old": "different", "native": "destination-root"},
            destination_pool="native",
        ).inventory_digest
        != inspection.inventory_digest
    )


def test_legacy_incident_label_is_opaque_but_bound_metadata():
    data = inventory(file())
    data["incident"] = "/opaque/historical/label"
    inspection = inspect(data)
    assert not inspection.blocked_reasons
    changed = deepcopy(data)
    changed["incident"] = "different"
    assert inspect(changed).inventory_digest != inspection.inventory_digest
    data["incident"] = True
    with pytest.raises(ValueError):
        inspect(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("size", True),
        ("inode", "1"),
        ("nlink", 0),
        ("mtime_ns", -1),
        ("pool", "unknown"),
        ("relative", "../escape"),
        ("relative", "/absolute"),
        ("relative", "a\\b"),
        ("relative", "a//b"),
    ],
)
def test_invalid_file_fields_are_rejected(field, value):
    data = inventory(file())
    data["files"][0][field] = value
    with pytest.raises(ValueError):
        inspect(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"version": 2},
        {"complete": 1},
        {"files": {}},
        {"directories": [{}]},
        {"special": [{}]},
        {"collisions": [True]},
    ],
)
def test_invalid_inventory_schema_is_rejected(changes):
    data = inventory(file())
    data.update(changes)
    with pytest.raises(ValueError):
        inspect(data)


def test_unknown_duplicate_or_unbound_declarations_and_dependencies_are_rejected():
    inspection = inspect(inventory(file()))
    (declaration,) = declarations(inspection)
    invalid = [
        (declaration, declaration),
        (replace(declaration, group_id="unknown"),),
        (replace(declaration, inventory_digest="0" * 64),),
    ]
    for values in invalid:
        with pytest.raises(ValueError):
            plan_inventory_batch(inspection, generation(inspection), ("video",), values)
    for changed_generation in (
        replace(generation(inspection), generation_id="old"),
        replace(generation(inspection), destination_id="other"),
    ):
        with pytest.raises(ValueError):
            plan_inventory_batch(inspection, changed_generation, ("video",), (declaration,))
    for dependencies in (
        (GroupDependencies("unknown", ()),),
        (GroupDependencies(declaration.group_id, ("unknown",)),),
        (GroupDependencies(declaration.group_id, ()),) * 2,
    ):
        with pytest.raises(ValueError):
            plan_inventory_batch(
                inspection, generation(inspection), ("video",), (declaration,), dependencies
            )


def test_root_mapping_must_be_explicit_unique_and_complete():
    for roots, destination in (
        ({"old": "same", "native": "same"}, "native"),
        ({"old": "source-root"}, "native"),
        ({"old": "bad/path", "native": "destination-root"}, "native"),
    ):
        with pytest.raises(ValueError):
            inspect_inventory(inventory(file()), pool_roots=roots, destination_pool=destination)


@pytest.mark.parametrize("values", [("bad", "a" * 64, "b" * 63), ("good", "bad", "b" * 64)])
def test_invalid_content_declaration_hashes_are_rejected(values):
    with pytest.raises(ValueError):
        ContentDeclaration(*values)


def test_directory_metadata_is_bound_and_input_order_is_irrelevant():
    data = inventory(file(), file("other", inode=11))
    data["directories"] = [file("shared", inode=20)]
    inspection = inspect(data)
    changed = deepcopy(data)
    changed["directories"][0]["mtime_ns"] += 1
    assert inspect(changed).inventory_digest != inspection.inventory_digest
    data["files"].reverse()
    assert inspect(data) == inspection


def test_native_hashes_and_tampered_inspection_are_rejected():
    inspection = inspect(inventory(file(), file("native", pool="native", inode=20)))
    native = next(g for g in inspection.groups if g.native)
    content = (
        *declarations(inspection),
        ContentDeclaration(native.group_id, inspection.inventory_digest, "b" * 64),
    )
    with pytest.raises(ValueError):
        plan_inventory_batch(inspection, generation(inspection), ("video",), content)
    with pytest.raises(ValueError):
        plan_inventory_batch(
            replace(inspection, groups=()),
            generation(inspection),
            ("video",),
            declarations(inspection),
        )
    with pytest.raises(ValueError):
        plan_inventory_batch(
            inspection,
            replace(generation(inspection), source_id="wrong"),
            ("video",),
            declarations(inspection),
        )


def test_adapter_inspects_and_plans_without_external_io(monkeypatch):
    import builtins
    import os
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("offline adapter attempted external I/O")

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(os, "stat", forbidden)
        guard.setattr(socket, "socket", forbidden)
        inspection = inspect(inventory(file()))
        result = plan_inventory_batch(
            inspection, generation(inspection), ("video",), declarations(inspection)
        )
    assert result.plan.selected_paths == ("video",)


@pytest.mark.parametrize(
    "collision",
    [
        {"relative": "../escape"},
        {"pools": ["unknown"]},
        {"value": float("nan")},
        {1: "not a string key"},
    ],
)
def test_collision_metadata_rejects_malformed_paths_pools_or_json(collision):
    data = inventory(file())
    data["collisions"] = [collision]
    with pytest.raises(ValueError):
        inspect(data)
