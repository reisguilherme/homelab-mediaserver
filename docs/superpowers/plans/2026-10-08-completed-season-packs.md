# Completed Season Packs Implementation Plan

Goal: a completed season requires one exact complete season pack; only ongoing
seasons allow new individual episode acquisitions. Unknown metadata waits.

Scope: isolated USB workspace only; no .env, server, media, commits or deployment.
The movie acquisition and discovery modules belong to another agent.

- [ ] RED: pure season completion and canonical filename identity cases; integration
  cases completed/ongoing/unknown with real fixture manifests.
- [ ] GREEN: pure domain helpers; require full canonical episode catalog and UTC
  completion evidence; preserve existing sources and admission/tombstone guards.
- [ ] GREEN: completed-season acquisition never falls through to individual
  dispatch or loses to a better individual release; ongoing uses aired episodes.
- [ ] GREEN: season pack file identity accepts EP/SE/absolute numbering only with
  an unambiguous canonical binding, retaining per-file quality and total budget.
- [ ] Verify focused Linux/WSL tests and lint; update configuration documentation;
  hand the pure helper interface to the independent audit implementer.
