# Single media disk implementation plan

> **For agentic workers:** Use regression-first implementation and independent review. Preserve the dirty checkout and coordinate non-overlapping file ownership.

**Goal:** Move the collection to the existing `hdd3` disk, verify it, and run the
media stack without requiring the retired media pools.

**Architecture:** Keep the current physical and logical paths for `hdd3` and
`/data`. Add a focused singleton registry using the existing storage guards;
stage and prove the actual move privately before a quiesced state/configuration
cutover and exact source cleanup.

**Tech stack:** Python 3.12, SQLite, Docker Compose, ext4, mergerfs, rsync.

**Spec:** `docs/superpowers/specs/2026-11-08-single-media-disk-design.md`

## Global constraints

- No formatting, repartitioning, unrelated data deletion, new host units or
  infrastructure. Preserve private credentials, media and inventories outside Git.
- Preserve native IDs, logical paths, sealed descriptors, hardlinks and authority.
- Legacy layouts retain their current validation and SSD-first behavior.
- Production cutover requires durable copy proof, consistent ledger/configuration
  backups and independent review; source cleanup follows readback, never precedes it.
- Tests use Python 3.12/Linux temporary fixtures; no tests on real media.

## Review focus

- A singleton must never write to an absent/wrong disk or an old mount fallback.
- Library and torrent hardlinks must survive while sparse allocation stays bounded.
- Old physical identities in live permits, claims and reservations must not make
  migrated data inaccessible or authorize a canceled source.
- An interruption must preserve originals and be resumable from durable evidence.
- Storage consumers must use current bindings and the exact new union branches.

### Task 1: Registry, preparation and startup

Files: `services/common/src/homeserver_common/storage.py`,
`services/common/src/homeserver_common/storage_startup.py`,
`scripts/prepare-storage.py`; corresponding storage unit tests.

Interface: add `StorageRegistry.primary_pool_id` and `primary_pool` properties.
Allow exactly singleton `{hdd3}` in addition to the existing supported layouts.
`prepare-storage.py --hdd3-uuid VALUE` alone prepares/verifies the singleton;
legacy flag combinations keep their existing meaning.

- [x] Write failing isolated tests for singleton loading, strict shape/UUID/caps,
  primary identity, startup and preparation; retain missing/foreign/RO safeguards.
- [x] Implement the minimal changes and run focused old/new layout suites.
- [x] Independently review before shipping the common bundle.

### Task 2: Runtime consumers and Compose

Files: worker/API primary-pool uses, deletion coordinator, capacity/host metrics,
Compose renderer and a standalone singleton storage override; related tests.

Consumes: `registry.primary_pool_id` / `registry.primary_pool` from Task 1.
Produces: a standalone `compose.storage.single.yaml` with only `hdd3` physical
storage binds and the existing logical union bind/entrypoint.

- [x] Pin primary-pool behavior and singleton rendered override in fixture tests.
- [x] Replace direct SSD assumptions only where media registry identity is used;
  preserve the basic layout and existing API/display field compatibility.
- [x] Verify generated override shape and that old storage binds are absent.
- [x] Verify the complete production Compose merge before cutover.

### Task 3: USB recovery singleton support

Files: `homeserver_common/usb_recovery.py`, `scripts/install-usb-recovery.py`,
recovery CLI/installer/controller tests.

Consumes: focused singleton preparation and registry support from Task 1.
Produces: singleton configuration/observation/restoration with exact target
identity and branch validation; existing 3/4-pool installations remain valid.

- [x] Write failing singleton config, availability, reboot and installation tests.
- [x] Remove unconditional SSD checks for the focused singleton path, keeping
  generation, authority, maintenance, mount and native readback guards.
- [x] Test modern Jellyfin auth remains unchanged and configured capacity margin
  uses the actual `HOMESERVER_` key when reinstalling.

### Task 4: Private migration operation

Files: ignored private operational scripts and evidence; no media inventory in Git.

- [x] Verify the target UUID, current mounts and unique allocated/logical size.
- [x] Audit physical ledger references and whether a migration command exists.
- [x] Build and independently review a resumable copy/verifier using fixture
  files with hardlinks, sparse extents, conflicts, changes and interrupted writes.
- [x] Pause new acquisitions with an owned maintenance marker; preserve operator
  pauses and only stage unexposed copies on the validated target filesystem.
- [ ] Copy the three source project trees, retaining hardlinks and metadata;
  produce content verification and final relative-path conflict reports.
- [ ] Under final quiescence, re-sync/prove changes and reconcile a copy of the
  database/claims/snapshots first. Explicitly validate live reservation semantics
  and leave historical immutable evidence intact.
- [ ] Require the inventoried zero derivations/deletion jobs/cleanup intents
  again; migrate the exact old-primary reservation anchor rows only after all
  current permit/claim/payload proofs pass. Preserve states/budgets/versions and
  completed cleanup/audited history. Validate planned jobs use current permits.
- [ ] Save private exact before-state, ship only reviewed changed code, publish
  proved data, install singleton registry/recovery, edit only union persistence
  and recreate required consumers with correct binds.
- [ ] Verify native queue, library IDs/files, physical hardlinks, capacity,
  deletion guards, worker/API health and external panels.
- [ ] Remove exact verified old paths and empty directories, normally unmount
  retired USB media, clear only owned maintenance and record final evidence.

## Verification and operational record

- Storage core/preparation/startup: 137 focused tests passed under Linux root,
  including the temporary real-UID-1000 fixture. USB recovery: 181 tests passed.
- Integrated Linux result: 4,245 passed, 155 skipped, seven dependency warnings.
  Skips are pinned proxy runtime/images and root-only fixtures unavailable in
  that unprivileged suite. Global Ruff/compilation/shell checks passed.
- Independent review found a stale logical FUSE-bind gap in the first native
  singleton guard. Device/root/read-only regression tests reproduced it; the
  guard now ties the local view to the current host union and physical target.
- The private initial copier passed nine Linux fixtures and independent review.
  It is running against unpublished staging; originals remain intact.
- Final native guard fixtures: 21 passed; global lint passed again. The actual
  server's base plus singleton Compose merge has no retired physical binds.
- Reviewed server sources and both candidate images are prepared and their
  baked file hashes verified. Running images, union mounts and storage registry
  have not been switched. The SSD copy phase finished; USB source copy continues.
- Private copy/delta/SHA fixtures: 63 passed in the independent Linux run.
- The detached incident pipeline is running: it waits for the original copier,
  performs one fresh delta, then independently hashes the moved inode groups.
  It preserves originals and stops at an unpublished proof or a guarded hold;
  it does not perform the final cutover or source cleanup. Combined helper
  validation: 82 tests passed; root independently ran the scheduler plus ledger
  adapter (50 passed). Current process/phase readback confirmed both jobs alive.
- Read-only Jellyfin health and public info returned 200. A preliminary native
  catalog identity receipt is stored privately; final quiescence must refresh it.
- Configuration plans are rendered privately. The actual verification Compose
  merge keeps all media/physical storage binds read-only. Host configuration is
  unchanged; final cutover remains gated on the physical transfer/proof.
- A private cutover workflow draft is preparatory only: no real Linux backend
  has been implemented or activated. Do not infer an automatic final switch or
  source cleanup from the running copy/SHA pipeline. Revalidate the full current
  proof, quiescence, actual application readback and before-state at that step.
- Content verifier and ledger transition are separate private reviewed helpers;
  their production application and final cutover remain pending copy completion.
- Ruling: operate in the existing dirty checkout because it contains the actual
  uncommitted application. Preserve all unrelated edits and private evidence.
