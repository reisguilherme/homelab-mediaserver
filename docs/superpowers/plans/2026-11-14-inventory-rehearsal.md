# Inventory and Publication Rehearsal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute without another confirmation inside the authorized preparation scope.

**Goal:** Import declared inventory metadata and rehearse batch publication without touching the server.

**Architecture:** A read-only, in-memory adapter validates the complete inventory before invoking the pure planner. A fixture-only runner observes actual Linux files, calls the adapter, copies and verifies a batch, and models consumer containment.

**Tech Stack:** Python 3.12, standard library, pytest, Ruff, WSL Linux temporary filesystems.

**Spec:** `docs/superpowers/specs/2026-11-14-inventory-rehearsal.md`

## Global Constraints

- No production I/O, SSH, services, mounts, credentials, private imports or media operations.
- Python 3.12, no new dependencies or runtime hooks. Existing planner unchanged.
- All operational permissions remain false. No source cleanup.
- The CLI accepts named scenarios only and owns its temporary paths.
- Existing `refactor` branch is the authorized workspace and publication destination.

## Review Focus

- Missing hashes must not conceal unselected collisions or aliases.
- Inventory digest and content SHA must never be interchangeable.
- Same content in different inodes must remain separate groups.
- Interrupted multi-path publication must not permit simulated scans.
- Fixture success must not imply production readiness or native API containment.

### Task 1: Inventory adapter and executable fixture

Files:
- Create `services/control/src/homeserver_control/adapters/migration_inventory.py`.
- Create `services/control/src/homeserver_control/migration_rehearsal.py`.
- Create `scripts/rehearse-media-batch.py`.
- Create `tests/unit/test_migration_inventory.py`.
- Create `tests/integration/test_migration_rehearsal.py`.

- [ ] First write focused failing tests for inventory completeness, inode grouping,
  hash coverage, collision detection and forbidden CLI options.
- [ ] Implement the in-memory adapter. Expose separate inspection and planning:
  `inspect_inventory(...) -> InventoryInspection` returns immutable metadata
  groups, a bound digest and blocking reasons; `plan_inventory_batch(...)`
  takes that inspection, generation, selection, bound content declarations and
  optional group dependencies, and returns coverage plus a `BatchPlan`.
  Choose concise typed dataclasses and document exact arguments in docstrings.
  Validate strict field types/relative paths, root mapping and immutable outputs.
  Reject unknown or duplicate declarations and metadata binding mismatches.
  Require hashes for every source group in v1; native files participate in
  collision validation but are never scheduled for copying.
- [ ] Implement `run_rehearsal(scenario) -> RehearsalReport` in an isolated module.
  A named scenario enum includes success, staging corruption, publication
  corruption, destination collision, source mutation, unaccounted hardlink,
  symlink, interruption and containment loss. Add I/O failures if useful.
  Hash actual synthetic files, pass genuine metadata and hashes through the
  adapter, and publish exclusively after staging verification. Keep journal
  facts bound to this one run; do not implement production resume/import.
- [ ] Demonstrate source preservation, copied inode separation, complete alias
  topology, equal-content inode separation and absence of unselected content.
  A partial publication has no completion or scan permission.
- [ ] CLI defaults to success, emits aggregate JSON, returns nonzero for a held
  scenario, rejects path/apply options, and never loads runtime configuration.
- [ ] Run new focused tests in Linux Python 3.12, plus existing planner tests;
  Ruff and self-review. Do not run the whole 4,000+ test suite.
- [ ] Independent review for spec compliance and quality; fix findings.

### Task 2: Document, verify and publish

Files:
- Update `docs/runbooks/incremental-media-planning.md`.
- Update `docs/architecture/homelab-2026/implementation-status.md`.

- [ ] Describe the exact API, CLI commands and partial-coverage limitation.
- [ ] Clearly distinguish real fixture I/O from simulated consumers and future
  production integration. Preserve the prior dated SSH evidence as historical.
- [ ] Run the CLI success and held cases, relevant regressions, lint and diff
  checks. Record only observed results and no new server claims.
- [ ] Whole-change review, commit and push `refactor`; verify remote commit and
  clean working tree. Do not deploy or change the platform catalog.
