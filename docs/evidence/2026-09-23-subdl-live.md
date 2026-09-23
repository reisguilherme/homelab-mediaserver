# SubDL preflight and download rollout — 2026-09-23

Production release: `2fded1569b6f56e18f48848552918e8de1ee4ff1` from `codex/download-gateway`.

## Validation

- The guarded `/srv/data` mount passed its UUID check. A production backup completed successfully before each release update.
- The full production Compose configuration, including `/etc/homeserver/compose.override.yaml`, passed `docker compose config --quiet`. The production smoke check passed after restart.
- In WSL2, lint passed; 62 unit, 35 contract, and 65 integration tests passed. `make compose-check` could not run there because Docker Compose is not installed in that WSL distribution; production Compose was validated on the server.
- The control database has schema migrations 1–6. The SubDL key is a root-owned `0600` server file mounted read-only in the worker. No credential is part of the release artifact.

## Live acquisition evidence

- SubDL returned an exact-release `BR_PT` SRT for Ted Lasso S04E01. The source file was Windows-1252; the worker converted it to valid UTF-8 before persisting the artifact and issuing a permit.
- A Dallas Buyers Club Blu-ray torrent included a small `Sample/` video. The corrected manifest selects only the main movie; a second non-sample feature remains ineligible. SubDL returned an exact-release `BR_PT` SRT for the selected SPARKS release.
- The gateway confirmed permits for Ted Lasso S04E01–E05, Backrooms, and Dallas Buyers Club. The worker database held seven corresponding pt-BR subtitle artifacts. qBittorrent reported Ted Lasso S04E01 and Backrooms downloading; the other five new torrents were queued. Sonarr and Radarr showed the same seven items in their queues.
- The formerly imported movie remained available. No new movie or episode had completed download and import at the time of this check, so final subtitle placement and playback sync still need a live check.

## Remaining request limits

- Twelve request rows were `waiting_space`. Active reservations totaled 443 GB against roughly 474.4 GB free on the 502.9 GB media filesystem; the 5% safety margin left too little unreserved capacity for another 81 GB movie or 100 GB season reservation.
- The Invite and Slow Horses had reservations but no confirmed gateway permit in this snapshot. They still require an eligible release with a bundled pt-BR subtitle or an exact-release SubDL SRT; their final source outcome was not proven in this rollout.
- Bazarr's SubDL provider was active and returned pt-BR results. OpenSubtitles.com authentication with the supplied credentials returned HTTP 401; Bazarr also reported HTTP 426 for that provider. It was disabled pending corrected account/API access. Podnapisi remained configured but had a connection error during the manual check.

The Seerr “Requested” label may remain until Arr import completes even while a permitted torrent is downloading. The gateway permit and Arr/qBittorrent queues are the operational evidence of active acquisition.
