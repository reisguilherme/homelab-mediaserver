# Consolidate the collection on the existing large USB disk

The owner requested moving the entire collection onto the already registered
18 TB disk and operating without the smaller media disks until more capacity is
needed. System files, appdata, databases and transcode remain on their existing
system storage. This request authorizes the collection move and the limited
storage configuration/mount changes needed for that disk; it does not authorize
formatting, repartitioning or unrelated data deletion.

## Result

The media registry can contain only `hdd3`. Its physical root remains
`/srv/external3/homeserver`; mergerfs remains `/srv/media-view`, presenting the
same `/data` paths to native applications. The registry does not require the
old SSD media volume or the two smaller USB disks. New downloads, imported
videos and subtitles use the large disk. Retired disks can be disconnected
after verified source cleanup and normal unmount. Their previous configuration
is preserved privately for a later explicit expansion.

The existing multi-pool layouts and their SSD-first behavior remain supported.
For a singleton registry, `primary_pool_id` is `hdd3`; otherwise it remains
`ssd`. This is an implementation choice made to preserve existing identities
and avoid renaming the already populated target pool.

## Migration invariants

- Keep every logical path, native media ID, torrent infohash, sealed descriptor,
  selected-file vector, profile and request state unchanged.
- Preserve source hardlink groups across `media`, `torrents` and private project
  recovery directories. Sparse files, owners, modes, timestamps, ACLs and xattrs
  must survive. Never copy the library and its torrent payload independently.
- Stage copies outside every published branch. Reject distinct files colliding
  at a final relative path; preserve redundant equal copies and sidecar history
  through an explicit receipt rather than overwriting silently.
- Verify copied content and hardlink topology before publication or source
  removal. Detect a changing source and re-copy/re-verify under final quiescence.
- Record a private durable manifest, source/target filesystem identities,
  before-state copies, current progress and the verified destination identities.
  All operational inventories and credentials remain outside Git.
- Reconcile claims, permits and current physical state using the copy proof.
  Do not change permit authority, recreate revoked sources or invent receipts.
  Historical audited manifests stay immutable. The measured installation's
  reservations are all old-primary anchors, rather than per-payload placement.
  Migrate the explicitly inventoried anchor rows to the new primary only after
  validating every current permit/claim and project payload on the target;
  preserve their states, budgets and versions. Assert the exact before rows.
- The measured installation has no derivations, deletion jobs or cleanup
  intents. Require this condition again at cutover; stop if it changes instead
  of improvising translation of captured inode identities. Preserve completed
  cleanup history and audited manifests/jobs; validate planned jobs against
  their current permit lookup. Reconcile the current pause hold by exact permit.
- Pause new acquisitions while transfer capacity is being consumed. Preserve
  operator pauses; final cutover quiesces all writers and download actors.
- On a missing/wrong/RO device, generation change, EIO, insufficient capacity,
  content discrepancy or ledger discrepancy, stop before cutover/cleanup and
  preserve the originals and resumable staging data.
- Source removal is part of the requested move, limited to exact manifested
  paths after destination verification, publication and application readback.
  Re-check source UUID/device/inode/size/mtime before each unlink. Never use
  qBit's logical-path deletion for source cleanup.

## Runtime changes

Storage loading, preparation and startup accept the focused singleton `hdd3`
layout while retaining all prior validation and multi-pool layouts. Metrics,
capacity snapshots, API and worker use the registry's primary pool instead of
assuming `ssd`. Deletion checks follow the primary identity after migration.
An independent Compose storage override includes only the target physical bind
and the existing guarded mergerfs logical bind; appdata binds are unchanged.

The opt-in USB recovery bundle accepts the same singleton layout, identifies
the target by its real registered UUID/serial/size and requires the exact
single branch. It must preserve operator maintenance and stop consumers on a
physical identity change. Existing recovery credentials and the modern Jellyfin
authentication fix remain intact.

Native entrypoint validation must also reject missing, foreign or RO target
storage while the FUSE mount still exists; a mounted mergerfs facade alone is
insufficient evidence that a native consumer can safely write.

Only the existing union's fstab line/dependencies may be edited during cutover;
do not replace fstab or introduce host units. Preserve unrelated services,
router/network settings, GPU settings and physical filesystem formatting.

## Validation and execution

Develop with Python 3.12 / WSL Linux and isolated fixture directories. Run
meaningful regression-first tests for singleton support, old layouts, missing
and foreign mounts, guards, migrations, interrupted copy, conflict and cleanup.
Review operational code independently before production writes. Do not run unit
tests against real media. Physical preparation fixtures use only their own
temporary directories and must clean those directories.

The checkout contains the current uncommitted application, so work in place
with non-overlapping file ownership and private before copies. Do not reset,
stash, commit unrelated work or add new CI, backup infrastructure or host units.
The owner supplied the execution scope directly; technical choices above do
not require an additional generic approval round.
