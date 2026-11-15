# Native Containment and Checkpoint Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Execute tasks continuously within the already authorized isolated preparation scope.

**Goal:** Demonstrate real native-service containment and verified Linux checkpoint restoration using synthetic data only.

**Architecture:** A manual checkpoint library inventories a quiescent tree, creates a GNU tar/PAX archive and restores to a new private tree. A separate harness owns disposable Jellyfin/Arr containers and supplies identity-bound containment checks. No production entrypoint or runtime hook.

**Tech Stack:** Python 3.12 standard library, GNU tar, pytest/Ruff, WSL Linux, local Docker.

**Spec:** [design](../specs/2026-11-15-native-checkpoint-design.md).

## Global Constraints

- Python 3.12; no new Python dependency, runtime hook, scheduler, backup manager, CI/CD or host unit.
- No SSH, production data/configuration, real service stop/start, mount changes or maintenance removal.
- Fixtures under owned private Linux `/tmp` roots; no arbitrary production roots/container IDs accepted by a CLI.
- Keep sources and partial artifacts; fail closed on uncertainty; no forced kill or forced container deletion.
- Public output is aggregate, without fixture secrets, native logs or detailed inventory.
- `partial_restart_safe=false` and `production_ready=false` in native reports; simulated tests cannot establish native execution.

## Review Focus

1. Container restarted then stopped again: changed start/finish identity must invalidate containment (Task 2).
2. WAL committed state missing from base DB: preserve/recover it in a disposable restored copy (Task 1).
3. Archive traversal, symlink ancestry and external hardlink: reject before extraction; outside sentinel unchanged (Task 1).
4. Cleanup loses Docker visibility: retain fixture and possible consumers rather than deleting mounted data (Task 2).
5. Native HTTP healthy but data not restored: require independent API object/catalog preservation, and distinguish Arr scan coverage (Task 2).

### Task 1: Quiescent checkpoint and isolated restore library

**Files:** Create `services/control/src/homeserver_control/checkpoint.py`, optional focused `checkpoint_manifest.py` for inventory/archive validation if needed, and `tests/integration/test_checkpoint.py`.

**Interfaces:**
- `capture_checkpoint(source: Path, bundle: Path, *, guard: Callable[[], None], expected_databases: tuple[str, ...] = ()) -> CheckpointReceipt`.
- `restore_checkpoint(bundle: Path, destination: Path, *, receipt: CheckpointReceipt, guard: Callable[[], None]) -> RestoreReport`.
- `CheckpointReceipt`: immutable archive SHA-256, manifest SHA-256 and declared scope/evidence fields; no operational permission.
- `RestoreReport`: `bytes_verified`, `metadata_verified`, `sqlite_verified`, `database_count`; private details remain in bundle/destination. Keep names stable for Task 2.
- Restore destination must not exist. Bundle paths fixed within newly created bundle; no user-specified archive member mapping.

- [ ] Write failing Linux tests for round-trip including directories, binary xattr, hardlinks, internal symlink, nanosecond mtime, UID/GID/modes/ACL where supported. Root fixture tests must exercise distinct UID/GID and ACL, not silently pass as unsupported.
- [ ] Add SQLite WAL committed-row fixture using a child process that exits without checkpoint; expected database corruption must fail even with damaged header.
- [ ] Add source mutation/guard failure, existing destination, archive corruption/truncation, duplicate/extra/missing member, traversal, symlink pivot/external hardlink, unsupported special, unaccounted link and secrecy tests. Malicious archive tests may construct trusted-test receipts to reach structural validation.
- [ ] Run failing focused tests, then implement capture/restore with GNU tar preservation, canonical private manifest, safe preflight, bounded size, immutable expected digests and before/after checks. Detect files by header, not extension. SQLite checks use a disposable copy of the restored tree and leave reference bytes unchanged.
- [ ] Run focused tests in WSL Python 3.12 both ordinary user and root for metadata cases; Ruff and diff checks. Report exact capabilities/skips. Commit only Task 1 files.

### Task 2: Native containment harness and CLI

**Files:** Create `services/control/src/homeserver_control/native_containment.py`, `native_rehearsal.py`, optional `native_rehearsal_apps.py` for API setup/readback; `scripts/rehearse-native-checkpoint.py`; `tests/unit/test_native_containment.py`; `tests/integration/test_native_checkpoint_rehearsal.py`; `docs/runbooks/native-checkpoint-rehearsal.md`. Update current preparation/runbook links after evidence.

**Interfaces:** Consume Task 1 signatures exactly. `run_native_rehearsal(scenario='success')` returns a dataclass report with `scope`, `containment_kind`, `completed`, `native_versions`, `native_scan_baseline_verified`, `checkpoint_restore_verified`, `restored_application_state_verified`, `partial_restart_safe=False`, `production_ready=False`, `blocked_reasons`, `resources_retained`.

- [ ] Write unit tests proving full-set validation before mutation; exact IDs/image/binds/nonce; rejected replacement, restart-and-stop, extra bind, Docker errors; no force kill and retention on uncertain cleanup; CLI rejects external roots/IDs/options.
- [ ] Implement Docker transport using argument arrays, local Unix socket only, explicit no remote endpoint. Harness creates its own fixture and containers, resolves all images first, records actual versions, validates all identities and start/finish timestamps. No automatic pulls or external network.
- [ ] Build real Jellyfin baseline with tiny valid local media and local metadata, disable external metadata fetching in this isolated fixture, create library and verify returned item identity. For Arr create/read a local API configuration or root-folder object and record clearly whether a catalogue scan was exercised. API via container exec, no exposed host ports.
- [ ] Implement success flow: baseline, stop/readback, checkpoint callback checks, opaque archive transfer + hash readback, restore to exclusive new root, clone restored tree for native startup, API state readback, safe stop/remove/cleanup of owned clones. Preserve reference restore tree until verification is complete.
- [ ] Implement `unexpected_restart` fixture: restart one owned clone after containment, then stop it; guard must reject changed timestamps before next capture/publication effect. Expected negative scenario exits 1 with blocked report. No restart of actual services.
- [ ] Run unit/integration tests, then real success and unexpected-restart scenarios with local images. Record versions, app-state evidence and limitations. Docker integration tests are explicit opt-in; no skipped test is claimed as native evidence.
- [ ] Run focused checkpoint/native and previous migration regression tests, Ruff, CLI/help checks; update documentation/status from measured results and commit. Independent review before integration/push to `refactor`.
