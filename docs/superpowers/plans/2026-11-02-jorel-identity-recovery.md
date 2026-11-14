# Series identity and localized ASC recovery

> **For agentic workers:** Use regression-first implementation and independent review. Preserve the current dirty checkout; do not stash, reset or commit unrelated work.

**Goal:** Let approved Jorel S01–S05 requests pass profile verification, discover their existing ASC packs, and use normal capacity-checked acquisition and verified import.

**Design:** Preserve the authenticated Seerr TMDB/TVDB/native-service binding. Share an identity resolver between profile synchronization, series acquisition and finalization. A missing/zero Sonarr TMDB may be normalized only through an unambiguous verified TVDB/native binding; positive conflicts and duplicate identities remain rejected. Resolve localized release aliases using authoritative metadata and native Sonarr decisions; never admit a fuzzy title or bypass the gateway.

**Constraints:** Python 3.12/Linux fixtures, no production data in tests, no secrets or raw inventory in Git, no media deletion, no mount/fstab changes, no added infrastructure. Existing profiles, byte limits, subtitle policy, descriptor and episode checks continue to apply.

## Tasks

- [x] Reproduce zero-TMDB profile/acquisition/finalization failures with isolated fixtures; preserve Seerr `tvdb_id` and `external_service_id` and implement a shared `SeriesIdentityResolver.update_requests(requests)` / `.resolve(catalog, tmdb_id)` interface. Return canonical copies without changing native PUT payload identities. Test positive conflicts, ambiguous matches, malformed IDs and stale bindings.
- [x] Reproduce localized ASC packs failing native identity parsing. Implement narrowly proven alias resolution while retaining the original release, native rejection decisions and sealed metadata checks. Test the five-season case and rejection of other series/seasons and ambiguous aliases.
- [x] Exercise the actual Standard-profile indexer iterator, including its configured ASC primary tier and cached season results. Require full native season vectors for alias packs. Recognize ASC's explicit Nacional release claim as generic Portuguese only when supported by release context and a Portuguese original; retain all later audio validation.
- [x] Reproduce the actual nonempty Sonarr search response: preserve every native rejected row, but allow a separately proven ASC copy of an unresolved `Unknown Series`-only result to request a fresh native identifier-aware decision. Mixed vetoes and known conflicting identities remain rejected.
- [x] Wire one resolver through worker construction, run focused tests and required Linux checks, and review the combined change. Keep failed or unrelated baseline checks explicit.
- [x] Prepare a private rollback copy and deploy only reviewed changed files. Restart only the affected worker through the existing runtime. Verify profile outcomes, real ASC candidate evaluation and normal gateway admissions; never manually inject torrents or fabricate permits. Check finalization after completed downloads, verify published paths if publication occurs, and report subtitle holds without bypassing policy.

## Review focus

Authenticated identity binding must survive an already-applied profile choice and restart; TVDB/native ID conflicts must fail closed; native profile updates must not overwrite zero TMDB with an invented native value; parser aliases must preserve release season/quality/audio facts; restart must not accidentally launch stale actors or alter mounts.

## Operational rulings

- Work in place with non-overlapping file ownership: the checkout contains the live uncommitted project, including required untracked modules. Creating a clean HEAD worktree would omit that state. Save narrow diffs and preserve all pre-existing edits.
- User explicitly requested the fix after reviewing the diagnosis. Proceed with implementation and validation without another approval checkpoint for routine reversible work.

## Verification record

- Python 3.12.3 / WSL Ubuntu, `uv sync --frozen`.
- Initial final-batch suite: 3,946 passed, 154 skipped (proxy binaries/images or root UID fixtures unavailable); `make lint` passed. Explicit PYTHONPATH is required for subprocess import checks.
- Read-only live preflight found and ranked complete ASC candidates for all five canonical season vectors (26/26/26/26/23).
- First worker-only deployment confirmed profile application and healthy services, then runtime verification exposed native Unknown Series deduplication. This requires the final native-context regression before claiming downloads resumed.
- All five native seasons have independently confirmed closure; S05 has an aired season-finale marker. Descriptor S01 passes file identity, size, hash and all 26 episode bindings.
- Final native-context regression batch: 3,964 passed, 154 skipped, lint/compilation/shell checks passed. Independent focused review found no remaining actionable issue. A single excess blank line in an existing test import block was normalized for global lint.
- Read-only live native-context replay preserved all nine original Unknown Series vetoes and produced fully proven retry candidates for S01/S05. All five descriptors independently pass their 26/26/26/26/23 episode vectors.
- Final image deployed by recreating only the existing control-worker service; unrelated containers unchanged. Project source ownership remains UID/GID 1000. Live admission verification is still pending below.
- Final native-context batch: 3,964 passed, 154 skipped; global `make lint` passed; focused independent review found no actionable issue. Only the worker was recreated with the final image.
- Live result: all five pack parents confirmed by the gateway on hdd3, all five sources received payload bytes. Last verification: S01/S04 100%, S02 34.2%, S03 29.9%, S05 89.0%. Standard remains selected. S02/S03 recovered transient snapshot refusals through normal automatic retry; the filesystem guard was unchanged.
- Publication is still pending. S01 finalization now reaches `waiting_subtitles`, rather than identity/profile failure. The existing animation subtitle policy remains enforced; no import completion is claimed.
- Panel, Jellyfin health and control readiness returned HTTP 200; storage pools/union were ready, USB recovery enabled, and no new kernel I/O errors appeared since deployment. Private code rollback and detailed operational evidence remain outside Git under the existing incident directory.

## Completed-download import follow-up

All five packs subsequently completed, but native publication remained 0/127.
The subtitle stage rejects the localized source filename because finalization
does not receive the authenticated localized title used by discovery. A separate
read-only provider check also returned HTTP 429 with `daily_limit`; this is not
evidence that subtitles do not exist. Existing TV policy requires subtitles
before publication, so a publication exception requires the owner's preference.

- [x] Propagate an authenticated, exact-ID localized title to finalization without leaking internal metadata into native update payloads. Preserve episode, release-family and edition checks; cover conflicts and withdrawn bindings with isolated regressions.
- [x] Respect SubDL rate limiting across attempts on the same source instance, including both TV and movie lookups. Use Retry-After when valid, otherwise the existing 15-minute cache horizon, with safe diagnostics and fixture-only tests.
- [x] Correct the audited Hill House importer's missing-native-TMDB check using the exact native series ID and positive TVDB already sealed in its validated manifest. Keep positive conflicts and malformed identities rejected; do not fabricate import receipts or alter the manifest.
- [ ] Review incremental changes, run focused and Linux checks, save private rollback artifacts and recreate only the worker. Verify real subtitle-stage behavior and report remaining provider/policy holds accurately.
- [x] Audit new Seerr requests: #128 and #129 were approved, profile-verified and downloading through confirmed gateway permits; no manual intervention was needed.

The independent incremental review of all six production modules found no
actionable issue. Focused regression-first suites passed for aliases (233),
provider quota handling (85) and audited series identity (56). Global lint and
compilation passed; final combined Linux suite and deployment verification are
tracked above.

- Combined Linux result: 4,033 passed, 154 skipped, 7 dependency deprecation
  warnings in 208 seconds. Global `make lint` passed. Skips concern unavailable
  proxy fixtures/binaries and real-root UID fixtures; these checks do not prove
  physical playback or external-provider availability.
- Built and checked all six production module hashes in a network-disabled,
  read-only ephemeral container. Private source/image rollback is under the
  existing USB-recovery incident root; deployment touches only control-worker.
- Deployed at 20:01 UTC, worker-only. Alias lookup now reaches the real SubDL
  provider, which logs HTTP 429/daily_limit with a 900-second cooldown. Pure live
  checks identify the correct subtitle release for all five first episodes.
  Hill House began importing, with physical same-device/inode/size hardlinks
  confirmed for its first three native files. Worker/API/gateway/Jellyfin health
  passed and no new kernel I/O errors appeared.

## Owner-authorized Jorel publication exception

The owner selected: "Importar agora e continuar buscando legendas" for Jorel.
Implement this as a durable per-series opt-in, default empty, rather than a
global change to the existing TV subtitle requirement. Verified native video
publication must be distinguishable from completed subtitle recovery.

- [x] Add a scoped repository opt-in and verified subtitle-pending episode states. Preserve all source/native/path/space checks and cancellation guards.
- [x] Permit chronological publication through verified pending episodes. Retry subtitles durably without repeating native imports or starving new episodes; retain the default subtitle gate for every title without the opt-in.
- [x] Review and validate isolated regressions and Linux checks, deploy worker-only, record only TMDB 74606 as allowed, and verify actual native imports and pending subtitle tracking.

Native import preflight also returned `Unknown Series` for the exact localized
video, with no tracked download context. A narrowly proven pack alias now uses
the existing native manual-import protocol: one exact candidate, only that
unresolved-series veto, preserved quality/languages, and positive series/episode
IDs from the current authenticated catalog and permit binding. Mixed vetoes,
conflicts and invalid descriptors remain blocked before import intent. Canonical
pack scans keep their existing behavior. The earlier combined run was interrupted
before this additional fix and is not counted as a passing result.

The reviewed private operational publisher pauses the daemon through its existing
maintenance flag, waits for acknowledgement, and invokes the same finalizer for
the five confirmed sources. It must stop on uncertain imports or changed
authorization/recovery state, preserve all intents, verify all native/source
links, and remove only its own coordination flag before returning work to the
daemon. It introduces no host service or separate persistent worker.

Final review strengthened native quality matching so WEBRip cannot be imported
as WEB-DL and remux requires explicit native evidence. The storage lifecycle
fixture was updated to implement the new active-reservation/source contracts;
production guards were retained. Its 22 tests and the 37 localized-import
regressions passed. A combined run before those two corrections had one fixture
failure and 4,130 passes; it is not the final passing suite.

Final combined Linux verification passed: 4,141 tests, 154 skips, 7 dependency
deprecation warnings in 225 seconds. Global lint/compilation passed. All seven
production file hashes, including migration 008, were confirmed in the built
read-only/network-disabled image before worker activation.

The no-dispatch live preflight exposed Sonarr omitting all three optional
unresolved identity fields (`series`, `seasonNumber`, `episodes`). The final
guard accepts omitted/null unresolved fields or an empty episode list, while
present contradictions, invalid types and nonempty vectors remain rejected.
The exact native `Unknown Series` veto, source/candidate checks and authenticated
catalog identity proof remain mandatory. This protocol adjustment passed 84
critical tests and independent review. Single-module updates used exact hashes;
the failed preflights wrote no import intent or owner opt-in.

The final live preflight passed for all five first episodes, then the owner's
choice was persisted for TMDB 74606 only. The coordinated publisher acknowledged
an idle daemon and began ordered native imports; the first 18 files reached
verified `subtitles_pending` receipts.

Final verification at 21:07 UTC confirmed all 127 native imports (26/26/26/26/23),
all 127 physical source/native links, and all 127 episodes indexed in Jellyfin.
The 127 subtitle-pending receipts remain durable, with only TMDB 74606 opted in.
The private publisher exited, its maintenance flag was removed, and the normal
daemon resumed without an error. Worker/API/gateway/Jellyfin were healthy and
the kernel showed no new I/O errors during the final publication interval.

The two new requests, Rogue One and Pinocchio, both have native files and Seerr
available status. Hill House has all ten episodes imported. Its subtitles and
Saiki's existing subtitle requirement remain in recovery; AHS S13 still lacks
its thirteenth episode. No media was deleted, no disks/mounts were changed, and
Standard remains selected for Jorel. Detailed evidence and reversible code
snapshots remain private under the existing incident root, outside Git.
