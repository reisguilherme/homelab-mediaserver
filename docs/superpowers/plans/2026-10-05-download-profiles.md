# Three download profiles implementation plan

> Execution: subagent-driven-development with focused tests and independent review.

Goal: replace generic profiles with Max Quality, Standard (default), Dublado in both Radarr and Sonarr and honor the selected profile in all source searches.

Approved specification: conversation 2026-10-05. Max Quality ranks UHD Remux > UHD Blu-ray > UHD WEB-DL > FHD Remux > FHD Blu-ray > FHD WEB-DL. Standard and Dublado accept only FHD Blu-ray > FHD WEB-DL. Dublado ranks dual PT-BR plus original > PT-BR > original before visual category. All use the existing subtitle policy. No new automatic replacements or upgrades.

## Constraints and rulings
- Preserve the 77 existing worktree changes and all downloaded media/torrents.
- Use shared named-profile definitions; three native profiles per Arr, same names.
- Default to Standard. Legacy Animation assignments migrate to Dublado; other obsolete profile assignments migrate to the configured default (initially Standard). Reassign catalog items before removing obsolete profile definitions; verify IDs and payloads.
- Seed viability minimum defaults to 5, editable in HOMESERVER_RELEASE_MIN_SEEDERS. Rank quality before seeds; dubbed audio before quality. Indexer and capacity protection remain.
- Explicit profile selection replaces automatic animation quality-profile reassignment. Animation detection remains relevant to existing subtitle behavior.
- No CI/CD, backup manager, additional host units, disk operations, or media removal.

## Tasks
- [x] Policy: create common/download_profiles.py definitions; extend release policy with for_profile(name), profile_name, dubbed flag and viability threshold; test full quality ordering, 0/unknown/few seed rejection, dubbed precedence and strict FHD Standard for movies and series. Update settings and .env.example, keeping legacy config compatibility private.
- [x] Native configuration: construct exact ordered profiles from native schemas including nested groups; Radarr original language for normal profiles and Any for Dublado; migrate assignments idempotently, point Seerr to configured default, remove obsolete definitions only after references are migrated; fixture tests for plan read-only, retries, malformed schemas, and safe migration.
- [x] Acquisition: resolve selected native profile IDs for each movie/series search, pass effective per-title policy to initial/manual/pack/episode paths, remove animation override; tests that series Max admits UHD, Standard excludes Remux/UHD, Dublado applies beyond animation, explicit selection survives and no profile leaks between titles.
- [x] Docs and review: document three profiles, mutable defaults, seed threshold, subtitles and choosing profiles in Seerr. Run focused tests then full Linux suite and lint. Review cross-component behavior.
- [x] Activate: sync verified changes, build custom services, reconcile native configuration and confirm Standard defaults plus exact profile names/order in both services. Verify health and existing torrent/library inventory without initiating unrelated downloads.

Review focus: unknown or deleted profile IDs; mixed profiles in a single worker cycle; higher-audio episodes versus lower-audio season packs; native rejection/custom-format constraints; restart during profile migration.

- [x] Seerr existing-title integration: apply explicit choices once before native searches, persist latest request per title, preserve later manual Arr edits and allow edited choices on the same request. Unknown instance/profile defers acquisition while finalization proceeds.
- Ruling: Jellyseerr 2.7.3 request PUT triggers application hooks. Migrate only stored profileId offline in a verified SQLite transaction, and baseline historical approved choices in the worker ledger before activation.
- Ruling: named quality/audio tiers precede season-pack preference; packs win within the same profile tier, while better episode sources must pass metadata, quality and capacity gates.
- Live compatibility: ignore only Radarr's recalculated boolean movieFile.qualityCutoffNotMet during profile read-back; retain all file identity/path/size checks. Migrate collection profile references with individual GET/PUT/GET before pruning, preserving monitoring/search flags. Unsupported import-list migrations fail closed.
- Completed 2026-10-05: 1,879 Linux tests passed, 109 environment-dependent tests skipped; lint passed. Both Arrs and Seerr expose exactly three profiles with Standard default and zero configuration drift. Existing torrent/library inventory preserved. See docs/evidence/2026-10-05-download-profiles.md for live validation and the separate pre-existing subtitle observation.
