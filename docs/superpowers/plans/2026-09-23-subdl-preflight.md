# SubDL Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admit inspectable torrents without bundled subtitles when an exact-release pt-BR SRT is secured before download.

**Architecture:** A small SubDL adapter returns verified SRT bytes; a SQLite artifact store preserves them across restarts. Movie and series acquisition use this fallback only after inspecting a video-only torrent. Finalizers require the stored artifact before import and write it to the library after the Arr imports the video.

**Tech Stack:** Python 3.12, httpx, SQLite, pytest, Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-23-subdl-preflight-design.md`.

## Global Constraints

- Preserve reservation and download permit checks before every grab.
- Keep all credentials out of Git and logs.
- Do not widen the source floor beyond Blu-ray/remux and WEB-DL or the existing size ceilings.
- Keep fail-closed behavior when SubDL is unavailable or returns ambiguous metadata.

## Review Focus

- A subtitle for the wrong season or episode must never authorize a torrent.
- A `pt`/`por` or Portugal-only entry must never count as `BR_PT`.
- Arbitrary download URLs, oversized payloads and malformed SRT must be rejected.
- An orphaned or corrupt stored subtitle must block import after a restart.
- Existing torrents with bundled pt-BR subtitles must keep their current behavior.

---

### Task 1: Exact-release SubDL adapter and persisted artifact

**Files:** Create `services/control/src/homeserver_control/worker/subdl.py`, `services/control/src/homeserver_control/persistence/subtitle_artifacts.py`, `services/control/src/homeserver_control/persistence/migrations/006_subtitle_artifacts.sql`; test in `tests/unit/test_subdl.py` and `tests/integration/test_subtitle_artifacts.py`.

**Interfaces:** `SubDLSource.fetch(tmdb_id: int, release_title: str, season: int | None = None, episode: int | None = None) -> bytes | None`; `SubtitleArtifactStore.put/get(reservation_id, scope_key, infohash, data)`.

- [ ] Add tests for exact match, wrong language/episode, malformed response, unsafe URL, download size/format, persistence and corrupt digest; run them and observe the expected failures.
- [ ] Implement bounded HTTP search/download and SQLite migration/store; run the focused tests to green.

### Task 2: Acquisition guarded by persisted subtitle

**Files:** Modify `worker/acquisition.py`, `worker/series_acquisition.py`, `worker/__main__.py`, `deploy/compose.prod.yaml`; test in `tests/integration/test_movie_acquisition.py`, `tests/integration/test_series_acquisition.py`, `tests/unit/test_release.py`, `tests/unit/test_worker_runtime.py`.

**Interfaces:** Acquirers receive optional `SubDLSource` and `SubtitleArtifactStore`. Existing manifests with pt-BR sidecars need neither.

- [ ] Add tests for video-only exact-match grab, absent subtitle no grab, subtitle persisted before POST, and Sonarr `WEB`/WEBDL classification; watch them fail.
- [ ] Implement the smallest acquirer/runtime changes; run the focused tests to green.

### Task 3: Finalization using the same verified SRT

**Files:** Modify `worker/finalization.py`, `worker/series_finalization.py`; test in `tests/integration/test_movie_finalization.py`, `tests/integration/test_series_finalization.py`.

**Interfaces:** Finalizers look up the artifact by reservation, scope and infohash, verify its digest and SRT structure, then create the library `.pt-BR.srt` atomically.

- [ ] Add tests for import with external SRT, missing/corrupt artifact rejection and restart idempotence; watch them fail.
- [ ] Implement validation and atomic sidecar creation; run focused tests to green.

### Task 4: Deploy and evidence

**Files:** Update `docs/runbooks/service-setup.md` and add deployment evidence under `docs/evidence/`.

- [ ] Run `make lint test-unit test-contract test-integration compose-check smoke` in WSL2.
- [ ] Put the SubDL key in a root-only server file, build the reviewed release, run the existing guarded deployment and production smoke.
- [ ] Verify provider status, Seerr requests, reservations, permits, Arr queue and gateway state without printing credentials; record which requests remain limited by source or capacity.
