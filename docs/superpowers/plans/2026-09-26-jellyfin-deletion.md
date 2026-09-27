# Jellyfin Coordinated Deletion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** A manual Delete of one movie or episode in Jellyfin removes the selected media from the managed stack, including download data where safe, and prevents automatic reacquisition. Bulk season/series Delete fails closed.

**Architecture:** Nginx keeps port 8096 and forwards only Jellyfin item DELETE requests to the control API. The API authenticates against Jellyfin, captures item identity before it vanishes, and persists an idempotent job. The worker processes guarded stages and finally calls the native Jellyfin DELETE after Arr removes the library file. Jellyfin's media bind stays read-only.

**Tech Stack:** Python 3.12, FastAPI, httpx, SQLite, Docker Compose, Nginx, Jellyfin 10.10.7, Radarr/Sonarr v3 APIs, qBittorrent Web API v2.

**Spec:** [2026-09-26-jellyfin-deletion-design.md](../specs/2026-09-26-jellyfin-deletion-design.md)

## Global Constraints

- Only an authenticated administrator's explicit Jellyfin Delete starts a job; watched state and library scans never do.
- Check `/srv/data` identity and file/Arr/torrent identities before side effects. Reject symlinks, ambiguous IDs, mixed torrent packages, and changed files.
- The worker may remove only the selected media; a shared episode file or torrent stays intact until its entire content is selected.
- Persist the job and tombstone before remote mutation. Repeated calls and restarts must not repeat completed effects or target changed media.
- Keep raw credentials, media files, and production databases out of Git. The Arr-facing qBittorrent gateway continues to deny deletion.

## Review Focus

- DELETE requests for non-item Jellyfin endpoints must still reach Jellyfin; item bulk DELETE must fail closed.
- Missing/changed mount or file, including a library rescan, must not remove Arr or torrent state.
- Existing completed reservations may have multiple superseded permits; cleanup must consider all without touching a permit owned by another media key.
- An episode file or torrent containing more than the selected episode must remain and produce a visible blocked job.
- A crash after Radarr/Sonarr cleanup must resume from durable stage without seeking a now-deleted Jellyfin item.

---

### Task 1: Jellyfin ingress

**Files:** `deploy/compose.yaml`, `deploy/compose.dev.yaml`, `deploy/compose.prod.yaml`, `deploy/jellyfin-proxy/nginx.conf`, `tests/system/test_jellyfin_proxy.py`.

**Interfaces:** `DELETE /Items/{id}` goes to `control-api:8080`; all other requests retain Jellyfin behavior.

- [ ] Write a system test asserting port 8096 belongs to the proxy, Jellyfin has no host port and retains a read-only media bind, item DELETE routes to control-api, and WebSocket/Range requests route to Jellyfin.
- [ ] Run the test and observe it fail for the missing proxy.
- [ ] Add the Nginx service/config and run the system test plus `make compose-check`.

### Task 2: Durable deletion job and tombstones

**Files:** `services/control/src/homeserver_control/persistence/migrations/007_deletion_jobs.sql`, `services/control/src/homeserver_control/persistence/deletion_jobs.py`, `tests/integration/test_deletion_jobs.py`.

**Interfaces:** `DeletionJobStore.enqueue(item_id, item_type, payload)`, `get`, `next_queued`, `set_stage`, `tombstone`, `is_tombstoned`.

- [ ] Test duplicate enqueue returns the same job and leaves its original payload unchanged; restart must preserve stage.
- [ ] Observe expected test failure, then create schema and store methods using `BEGIN IMMEDIATE` for writes.
- [ ] Test tombstones across restart and `make test-integration`.

### Task 3: Authenticated capture and worker orchestration

**Files:** `api/app.py`, `worker/deletion_coordinator.py`, `worker/__main__.py`, `worker/runtime.py`, `adapters/{jellyfin,arr,seerr,qbittorrent}.py`, `gateway/app.py`, `gateway/allowlist.py`; focused contract/integration tests.

**Interfaces:** API returns HTTP 204 only after durable acceptance; worker processes one queued job per cycle and moves stages monotonically.

- [ ] Test 401/403 for missing, non-admin or non-delete-authorized Jellyfin tokens, and 422 for unsupported IDs/types.
- [ ] Test movie capture using a Jellyfin item with path/TMDb matching exactly one Radarr record and reservation, and reject path or file identity mismatch.
- [ ] Test movie cleanup order: tombstone, internal qBittorrent delete scoped to permits, exact Radarr `moviefile/{id}` delete, Radarr record delete with `deleteFiles=false` and exclusion, Seerr request removal, Jellyfin internal DELETE; repeat after each failure stage.
- [ ] Test episode cleanup: unmonitor only selected episode; block shared `episodeFileId`/torrent; write episode tombstone and keep subsequent episode ordering.
- [ ] Test that bulk series and season Delete is rejected before mutation.
- [ ] Test mount failure, unexpected path, symlink, ambiguous IDs, duplicate API calls, and Arr-facing qBittorrent delete denial.
- [ ] Implement each behavior after its red test and run unit, contract and integration suites.

### Task 4: Production setup and verification

**Files:** `docs/runbooks/operations.md`, `docs/runbooks/service-setup.md`, deployment files as necessary.

- [ ] Run lint, unit, contract, integration, compose and smoke checks on Linux/WSL2.
- [ ] Back up control DB and deployed Compose, then deploy via `scripts/deploy.sh` after mount UUID guard.
- [ ] Verify Jellyfin login/catalog, WebSocket and Range playback through port 8096.
- [ ] Add a tiny temporary movie fixture, click/call Jellyfin Delete through the proxy, verify job stages, Jellyfin disappearance and that no real media or torrent changed; remove fixture remnants.
- [ ] Verify published branch/release and document the operator's Delete path and recovery steps.
