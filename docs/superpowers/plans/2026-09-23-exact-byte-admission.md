# Exact Byte Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admit media only when the verified torrent's actual bytes fit alongside outstanding bytes of admitted torrents on the media filesystem.

**Architecture:** Keep zero-byte request records before release selection. The worker inspects torrent metadata and obtains a fresh qBittorrent queue snapshot. A SQLite write transaction issues the gateway permit and claims exactly the inspected torrent size after comparing free filesystem bytes with all outstanding permitted downloads. Completed bytes are already reflected in filesystem free space.

**Tech Stack:** Python 3.12, SQLite WAL, httpx, FastAPI gateway, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-23-exact-byte-admission-design.md`

## Global Constraints

- Quality priority and pt-BR subtitle eligibility stay intact.
- All torrent files count because the current qBittorrent add downloads the full torrent.
- New admission fails closed if the mount or gateway queue snapshot is unavailable.
- No automatic media deletion, native Arr auto-grab, or qBittorrent LAN API exposure.

## Review Focus

- `metaDL` with `total_size=0` must retain its full verified claim.
- A second worker with a stale queue snapshot must see a newly inserted permit in SQLite and count its full claim.
- A completed torrent must have zero outstanding bytes because its file is already reflected in filesystem free space.
- A request with no eligible source must consume zero bytes.
- A metadata mismatch at the gateway must remain rejected even when enough disk exists.

---

### Task 1: Remove fixed media size ceilings

**Files:** `services/control/src/homeserver_control/domain/{policy,capacity,selection,torrent_bytes}.py`, `services/control/src/homeserver_control/worker/{acquisition,series_acquisition,finalization,series_finalization,validation}.py`, corresponding unit and integration tests.

**Interfaces:** `available_bytes(free, total, commitments)` returns `max(0, free - sum(positive commitments))`; `validate_media(..., maximum_bytes=verified_torrent_bytes)` still verifies import against the permit.

- [ ] Add tests: 120 GB v1 torrent metadata is structurally valid; a 6 GB episode and 90 GB movie are eligible if their metadata is valid; free space has no fixed safety subtraction.
- [ ] Run those tests and confirm failures due to old 100/80/5 GB limits and fixed safety subtraction.
- [ ] Remove the product ceilings from selection, parser, acquisition and import validation. Preserve structural metadata limits and quality/subtitle guards.
- [ ] Run focused tests, then unit tests; commit the isolated change.

### Task 2: Persist requests without speculative claims

**Files:** `services/control/src/homeserver_control/worker/runtime.py`, `services/control/src/homeserver_control/persistence/db.py`, `tests/unit/test_worker_runtime.py`, `tests/integration/test_reservations.py`.

**Interfaces:** `WorkerCycle._budgets` becomes zero for movie, episode and season; `ReservationRepository.reserve(..., budget_bytes=0)` remains idempotent and consumes no capacity.

- [ ] Add a test where several approved requests without releases all persist with zero commitment and a later one can be evaluated.
- [ ] Run and confirm the test fails against fixed `81_000_000_000` and `100_000_000_000` budgets.
- [ ] Change request admission to zero-byte records. Keep cancellation and identity behavior.
- [ ] Run focused integration tests and commit.

### Task 3: Atomic exact-byte permit issuance

**Files:** `services/control/src/homeserver_control/gateway/permits.py`, `services/control/src/homeserver_control/worker/capacity_evidence.py` (new), `tests/integration/test_gateway_reservation.py`, `tests/unit/test_capacity.py`.

**Interfaces:** `CapacityEvidence(free_bytes: int, remaining_by_hash: dict[str, int], other_pending_bytes: int)`; `PermitRegistry.issue(..., capacity: CapacityEvidence | None = None)` uses `BEGIN IMMEDIATE` when capacity is supplied, computes each active permit's remaining bytes (full permit size when its hash is missing), adds pending unmanaged torrents, verifies new bytes fit, normalizes the reservation's verified budget, and inserts the permit in one transaction. The existing no-capacity path remains for isolated legacy fixtures, never for production acquirers.

- [ ] Add tests for a 2 GB permit on a zero-byte reservation, a 3 GB permit rejected with only 2 GB uncommitted, two concurrent issuers, a completed torrent reporting zero remaining, an unknown `metaDL` torrent consuming its full size, and a stale snapshot missing a newly inserted permit.
- [ ] Run and confirm the new tests fail because capacity-aware issuance is missing.
- [ ] Implement transaction-scoped claim, input validation and no-fixed-episode-limit. Roll back both claim and permit on error.
- [ ] Run focused and full gateway tests; commit.

### Task 4: Wire real queue and filesystem evidence into acquisition

**Files:** `services/control/src/homeserver_control/worker/{__main__,acquisition,series_acquisition}.py`, `tests/integration/{test_movie_acquisition,test_series_acquisition}.py`, `tests/contract/test_gateway_arr.py`.

**Interfaces:** The acquirers receive an async `capacity_provider`. Production provider checks the recent mounted-filesystem snapshot, reads current `/data` free bytes, and queries all torrents through the gateway's authenticated internal capacity endpoint. Only admitted rows with positive `total_size` and valid `amount_left` enter `remaining_by_hash`; admitted unknown rows consume their full permit size in Task 3. Unmanaged known rows contribute to `other_pending_bytes`; an unmanaged unknown row fails closed.

- [ ] Add tests where a 2 GB movie and a 6 GB episode each issue a permit for exact `inspected.total_bytes`; unavailable evidence issues no permit; reported release size cannot substitute for parsed torrent metadata.
- [ ] Run and confirm failures against fixed budgets and episode ceiling.
- [ ] Pass evidence into atomic issuance immediately before posting to Arr, then preserve existing gateway hash/metadata validation.
- [ ] Run contract, integration and unit suites; commit.

### Task 5: Existing state, deploy and live verification

**Files:** `docs/superpowers/specs/2026-09-21-homeserver-design.md`, `docs/evidence/` note without private server inventory, deployment scripts only if migration is required.

- [ ] Add a migration test proving old reservations without permits contribute zero pending bytes and old permits with unknown qBittorrent metadata remain conservative.
- [ ] Update the system design to replace fixed 80/5/100 GB admission budgets with inspected bytes and queue accounting.
- [ ] Run `make lint`, `make test-unit`, `make test-contract`, `make test-integration`, `make compose-check` and `make smoke` in Linux/WSL.
- [ ] Back up control state, deploy through `scripts/deploy.sh`, verify the mount guard and Compose, then observe a real request entering with an exact-size permit. Confirm old no-source requests no longer block later ones; never remove existing torrents or media as part of migration.
