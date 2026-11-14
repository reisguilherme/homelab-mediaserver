from dataclasses import FrozenInstanceError, replace
from itertools import combinations

import pytest

from homeserver_control.domain.migration_batches import (
    MigrationGeneration,
    MigrationGroup,
    plan_batch,
)

GENERATION = MigrationGeneration("snapshot-1", "source-a", "destination-b")


def group(group_id="video", paths=("library/example.mkv",), **changes):
    return MigrationGroup(group_id, paths, **{
        "logical_bytes": 100,
        "content_sha256": "a" * 64,
        **changes,
    })


def test_selection_expands_hardlinks_and_transitive_dependencies_without_mutation():
    generation = MigrationGeneration("snapshot-1", "source-a", "destination-b")
    subtitle = MigrationGroup("subtitle", ("library/example.srt",), 5, "b" * 64)
    metadata = MigrationGroup("metadata", ("library/example.nfo",), 3, "c" * 64)
    video = MigrationGroup(
        "video", ("library/example.mkv", "incoming/example.mkv"), 100, "a" * 64,
        ("subtitle",),
    )
    subtitle = MigrationGroup(
        "subtitle", subtitle.paths, subtitle.logical_bytes, subtitle.content_sha256,
        ("metadata",),
    )
    groups = (video, subtitle, metadata)
    selected = ("library/example.mkv", "incoming/example.mkv")
    plan = plan_batch(generation, groups, selected)

    assert plan.selected_group_ids == ("metadata", "subtitle", "video")
    assert plan.selected_paths == (
        "incoming/example.mkv", "library/example.mkv", "library/example.nfo",
        "library/example.srt",
    )
    assert plan.total_logical_bytes == 108
    assert plan.blocked_reasons == ()
    assert [item.group_id for item in plan.evidence_states] == ["metadata", "subtitle", "video"]
    assert all(not item.staging_declared for item in plan.evidence_states)
    assert groups == (video, subtitle, metadata)
    assert selected == ("library/example.mkv", "incoming/example.mkv")
    assert plan == plan_batch(generation, tuple(reversed(groups)), tuple(reversed(selected)))
    with pytest.raises(FrozenInstanceError):
        plan.total_logical_bytes = 0
    with pytest.raises(FrozenInstanceError):
        video.paths = ()


@pytest.mark.parametrize("paths", [
    ("../escape",), ("/absolute",), ("a/../b",), ("a/./b",), ("a//b",),
    ("a/",), ("",), ("C:/absolute",), ("a\\b",), ("a\x00b",), ("a\nb",),
    ("same", "same"), (), ["mutable"], (True,),
])
def test_malformed_or_mutable_paths_are_rejected(paths):
    with pytest.raises(ValueError):
        group(paths=paths)


@pytest.mark.parametrize("changes", [
    {"group_id": ""}, {"group_id": True}, {"group_id": "bad/id"},
    {"logical_bytes": True}, {"logical_bytes": -1}, {"logical_bytes": 1.0},
    {"content_sha256": "a" * 63}, {"content_sha256": "g" * 64},
    {"content_sha256": "A" * 64}, {"content_sha256": True},
    {"dependency_ids": ["mutable"]}, {"dependency_ids": ("",)},
    {"dependency_ids": ("same", "same")},
])
def test_malformed_group_fields_are_rejected(changes):
    with pytest.raises(ValueError):
        group(**changes)


@pytest.mark.parametrize("args", [
    ("", "source", "destination"), ("epoch", "", "destination"),
    ("epoch", "source", ""), ("epoch", "same", "same"),
    (True, "source", "destination"),
])
def test_generation_rejects_missing_or_ambiguous_identity(args):
    with pytest.raises(ValueError):
        MigrationGeneration(*args)


@pytest.mark.parametrize("groups,selected,reason", [
    ((group(), group()), ("library/example.mkv",), "duplicate_group_id:"),
    ((group(), group("other")), ("library/example.mkv",), "path_conflict:"),
    ((group(paths=("library",)), group("other")), ("library",), "path_conflict:"),
    ((group(paths=("library", "library/example.mkv")),), ("library",), "path_conflict:"),
    ((group(dependency_ids=("missing",)),), ("library/example.mkv",), "missing_dependency:"),
    ((group(),), ("absent",), "unknown_selected_path:"),
    ((group(dependency_ids=("video",)),), ("library/example.mkv",), "dependency_cycle:"),
    ((group(dependency_ids=("other",)),
      group("other", ("sidecar",), dependency_ids=("video",))),
     ("library/example.mkv",), "dependency_cycle:"),
])
def test_inventory_ambiguities_and_unknown_selection_block_the_whole_plan(groups, selected, reason):
    plan = plan_batch(GENERATION, groups, selected)
    assert any(item.startswith(reason) for item in plan.blocked_reasons)
    assert plan.selected_group_ids == ()
    assert plan.selected_paths == ()
    assert plan.evidence_states == ()
    assert plan.total_logical_bytes == 0
    assert plan == plan_batch(GENERATION, tuple(reversed(groups)), selected)


def test_unselected_inventory_conflicts_also_block_and_equal_hash_does_not_merge():
    groups = (group(), group("conflict-a", ("other",)), group("conflict-b", ("other",)))
    plan = plan_batch(GENERATION, groups, ("library/example.mkv",))
    assert plan.blocked_reasons
    assert plan.selected_group_ids == ()


def test_prefix_conflict_cannot_hide_behind_a_lexically_intervening_sibling():
    groups = (
        group("parent", ("a/b",)), group("sibling", ("a/b-x",)),
        group("child", ("a/b/c",)),
    )
    plan = plan_batch(GENERATION, groups, ("a/b-x",))
    assert "path_conflict:a/b:a/b/c" in plan.blocked_reasons
    assert plan.selected_group_ids == ()


def test_thousands_of_dependencies_expand_and_cycles_block_without_recursion():
    groups = tuple(group(
        f"group-{index}", (f"path-{index}",), logical_bytes=1,
        dependency_ids=(f"group-{index + 1}",) if index < 1999 else (),
    ) for index in range(2000))
    plan = plan_batch(GENERATION, groups, ("path-0",))
    assert len(plan.selected_group_ids) == 2000
    assert plan.total_logical_bytes == 2000
    assert plan.blocked_reasons == ()
    cycle = (*groups[:-1], replace(groups[-1], dependency_ids=("group-0",)))
    assert "dependency_cycle:inventory" in plan_batch(
        GENERATION, cycle, ("path-0",),
    ).blocked_reasons


def test_equal_content_at_distinct_inodes_is_counted_separately_and_zero_bytes_are_valid():
    groups = (group(), group("other", ("other",)), group("empty", ("empty",), logical_bytes=0))
    plan = plan_batch(GENERATION, groups, ("library/example.mkv", "other", "empty"))
    assert plan.total_logical_bytes == 200
    assert plan.blocked_reasons == ()


def test_manifest_digest_is_canonical_and_binds_every_manifest_field():
    from homeserver_control.domain.migration_batches import manifest_digest

    original = group(paths=("b", "a"), dependency_ids=("sidecar-b", "sidecar-a"))
    digest = manifest_digest(original)
    assert len(digest) == 64
    assert digest == manifest_digest(replace(
        original, paths=("a", "b"), dependency_ids=("sidecar-a", "sidecar-b"),
    ))
    for changes in (
        {"group_id": "other"}, {"paths": ("a", "c")}, {"logical_bytes": 101},
        {"content_sha256": "b" * 64}, {"dependency_ids": ("sidecar-c",)},
    ):
        assert digest != manifest_digest(replace(original, **changes))


def receipt_for(item, declared_phase, **changes):
    from homeserver_control.domain.migration_batches import DeclaredReceipt, manifest_digest

    return DeclaredReceipt(**{
        "generation_id": GENERATION.generation_id,
        "source_id": GENERATION.source_id,
        "destination_id": GENERATION.destination_id,
        "group_id": item.group_id,
        "manifest_digest": manifest_digest(item),
        "phase": declared_phase,
        **changes,
    })


def test_each_receipt_phase_requires_every_preceding_phase_and_never_grants_authority():
    from homeserver_control.domain.migration_batches import EvidencePhase

    item = group()
    phases = tuple(EvidencePhase)
    for size in range(4):
        for present in combinations(phases, size):
            receipts = tuple(receipt_for(item, phase) for phase in present)
            plan = plan_batch(GENERATION, (item,), item.paths, receipts)
            evidence, = plan.evidence_states
            staging = EvidencePhase.STAGING_VERIFIED in present
            publication = staging and EvidencePhase.PUBLICATION_VERIFIED in present
            readback = publication and EvidencePhase.NATIVE_READBACK_VERIFIED in present
            assert (evidence.staging_declared, evidence.publication_declared,
                    evidence.native_readback_declared) == (staging, publication, readback)
            assert not any((plan.execution_allowed, plan.source_cleanup_allowed,
                            plan.global_scan_allowed, plan.deletion_allowed))
            assert plan == plan_batch(GENERATION, (item,), item.paths, tuple(reversed(receipts)))
            assert plan == plan_batch(GENERATION, (item,), item.paths, receipts + receipts)


@pytest.mark.parametrize("changes", [
    {"generation_id": "older"}, {"source_id": "other-source"},
    {"destination_id": "other-destination"}, {"group_id": "other-group"},
    {"manifest_digest": "0" * 64},
])
def test_receipts_from_another_binding_cannot_promote_even_with_later_phases(changes):
    from homeserver_control.domain.migration_batches import EvidencePhase

    item = group()
    receipts = tuple(receipt_for(item, phase, **changes) for phase in EvidencePhase)
    evidence, = plan_batch(GENERATION, (item,), item.paths, receipts).evidence_states
    assert (evidence.staging_declared, evidence.publication_declared,
            evidence.native_readback_declared) == (False, False, False)


def test_complete_declarations_for_one_group_cannot_fill_another_groups_missing_staging():
    from homeserver_control.domain.migration_batches import EvidencePhase

    first, second = group(), group("second", ("second",))
    receipts = (
        *(receipt_for(first, phase) for phase in EvidencePhase),
        receipt_for(second, EvidencePhase.STAGING_VERIFIED, generation_id="older"),
        receipt_for(second, EvidencePhase.PUBLICATION_VERIFIED),
        receipt_for(second, EvidencePhase.NATIVE_READBACK_VERIFIED),
    )
    plan = plan_batch(GENERATION, (first, second), (*first.paths, *second.paths), receipts)
    states = {state.group_id: state for state in plan.evidence_states}
    assert states["video"].native_readback_declared
    assert not states["second"].staging_declared
    assert not states["second"].publication_declared
    assert not states["second"].native_readback_declared
    assert not any((plan.execution_allowed, plan.source_cleanup_allowed,
                    plan.global_scan_allowed, plan.deletion_allowed))


def test_changed_manifest_and_generation_invalidate_previously_complete_declarations():
    from homeserver_control.domain.migration_batches import EvidencePhase

    original = group()
    receipts = tuple(receipt_for(original, phase) for phase in EvidencePhase)
    for generation, item in (
        (replace(GENERATION, generation_id="snapshot-2"), original),
        (GENERATION, replace(original, paths=("renamed",))),
        (GENERATION, replace(original, logical_bytes=101)),
        (GENERATION, replace(original, content_sha256="b" * 64)),
        (GENERATION, replace(original, dependency_ids=("sidecar",))),
    ):
        groups = (item, group("sidecar", ("sidecar",)))
        evidence = next(state for state in plan_batch(
            generation, groups, item.paths, receipts,
        ).evidence_states if state.group_id == "video")
        assert not evidence.staging_declared
        assert not evidence.publication_declared
        assert not evidence.native_readback_declared


@pytest.mark.parametrize("changes", [
    {"phase": "invented"}, {"manifest_digest": "bad"}, {"group_id": ""},
    {"generation_id": False}, {"source_id": ""}, {"destination_id": ""},
])
def test_malformed_receipts_are_rejected(changes):
    from homeserver_control.domain.migration_batches import EvidencePhase

    with pytest.raises(ValueError):
        receipt_for(group(), EvidencePhase.STAGING_VERIFIED, **changes)


@pytest.mark.parametrize("groups,selected,receipts", [
    ([group()], ("library/example.mkv",), ()),
    ((group(),), ["library/example.mkv"], ()),
    ((group(),), ("../escape",), ()),
    ((group(),), (True,), ()),
    ((group(),), ("library/example.mkv",), []),
    (("not-a-group",), ("library/example.mkv",), ()),
    ((group(),), ("library/example.mkv",), ("not-a-receipt",)),
])
def test_plan_rejects_untyped_or_mutable_boundary_inputs(groups, selected, receipts):
    with pytest.raises(ValueError):
        plan_batch(GENERATION, groups, selected, receipts)


def test_planning_needs_no_filesystem_network_or_clock(monkeypatch):
    import builtins
    import os
    import socket
    import time

    from homeserver_control.domain.migration_batches import EvidencePhase

    item = group()
    receipts = tuple(receipt_for(item, phase) for phase in EvidencePhase)

    def forbidden(*args, **kwargs):
        pytest.fail("planner performed external I/O")

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(os, "stat", forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(time, "time", forbidden)
        plan = plan_batch(GENERATION, (item,), item.paths, receipts)
    assert plan.evidence_states[0].native_readback_declared
    assert not plan.execution_allowed
