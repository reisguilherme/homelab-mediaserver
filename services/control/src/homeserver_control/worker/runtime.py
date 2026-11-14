from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from homeserver_control.persistence.db import ReservationResult

from .audited_coverage import AuditedCoverage
from .scheduler import AdmissionCandidate
from .series_identity import SeriesIdentityResolver


class ApprovedRequestSource(Protocol):
    async def list_approved(self, page: int) -> list[dict[str, object]]: ...

    async def get_request_status(self, source_id: str) -> int | None: ...


class ReservationScheduler(Protocol):
    def admit(self, candidate: AdmissionCandidate) -> ReservationResult: ...


class MovieAcquisition(Protocol):
    async def acquire(self, media_key: str, reservation_id: str) -> str: ...


class MovieFinalization(Protocol):
    async def finalize(self, media_key: str, reservation_id: str) -> str: ...


class SeriesFinalization(MovieFinalization, Protocol):
    async def retry_pending_subtitles(self, media_key: str, reservation_id: str) -> str: ...


class Cancellation(Protocol):
    async def reconcile(self, approved_source_ids: set[str]) -> int: ...


class UncertainSourceReconciliation(Protocol):
    async def reconcile(self) -> int: ...


class MoviePriority(Protocol):
    async def prioritize(self) -> str: ...


class RequestProfiles(Protocol):
    async def reconcile(
        self, requests: list[dict], *, preserve_native_series: frozenset[int] = frozenset(),
    ) -> set[str]: ...


class CoordinatedDeletion(Protocol):
    async def run_once(self) -> str: ...


class CollectionArtwork(Protocol):
    async def reconcile(self) -> dict: ...


class JellyfinMetadata(Protocol):
    async def reconcile(self) -> dict: ...


class AuditedImports(Protocol):
    def prepare_manifest(self) -> dict: ...

    async def run_once(self, *, manifest: dict | None = None) -> object: ...


LOGGER = logging.getLogger(__name__)


def _log_failure(context: str, error: Exception, media_key: str | None = None) -> None:
    # Upstream exception strings/tracebacks can contain API keys and response bodies.
    LOGGER.error(
        "%s%s (%s)", context, f" for {media_key}" if media_key else "", type(error).__name__
    )


@dataclass(frozen=True)
class CycleReport:
    processed: int = 0
    accepted: int = 0
    deferred: int = 0
    malformed: int = 0
    grabbed: int = 0
    cancelled: int = 0


class WorkerCycle:
    """Track approved requests without claiming disk before inspecting a torrent."""

    _budgets = {
        "movie": 0,
        "episode": 0,
        "season": 0,
    }

    def __init__(
        self,
        *,
        source: ApprovedRequestSource,
        scheduler: ReservationScheduler,
        acquirer: MovieAcquisition | None = None,
        finalizer: MovieFinalization | None = None,
        series_acquirer: MovieAcquisition | None = None,
        series_finalizer: SeriesFinalization | None = None,
        cancellation: Cancellation | None = None,
        source_reconciler: UncertainSourceReconciliation | None = None,
        discarded_sources: UncertainSourceReconciliation | None = None,
        manual_source_replacements: CoordinatedDeletion | None = None,
        movie_prioritizer: MoviePriority | None = None,
        deletion_coordinator: CoordinatedDeletion | None = None,
        capacity_refresh: Callable[[], Awaitable[object]] | None = None,
        pending_movie_imports: Callable[[], list[dict[str, str]]] | None = None,
        pending_series_imports: Callable[[], list[dict[str, str]]] | None = None,
        series_identity: SeriesIdentityResolver | None = None,
        request_profiles: RequestProfiles | None = None,
        jellyfin_collections: CollectionArtwork | None = None,
        jellyfin_brazilian_metadata: JellyfinMetadata | None = None,
        audited_imports: AuditedImports | None = None,
        audited_allow_seerr: bool = False,
        page_size: int = 20,
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("page size must be between 1 and 100")
        self.source = source
        self.scheduler = scheduler
        self.acquirer = acquirer
        self.finalizer = finalizer
        self.series_acquirer = series_acquirer
        self.series_finalizer = series_finalizer
        self.cancellation = cancellation
        self.source_reconciler = source_reconciler
        self.discarded_sources = discarded_sources
        self.manual_source_replacements = manual_source_replacements
        self.movie_prioritizer = movie_prioritizer
        self.deletion_coordinator = deletion_coordinator
        self.capacity_refresh = capacity_refresh
        self.pending_movie_imports = pending_movie_imports
        self.pending_series_imports = pending_series_imports
        self.series_identity = series_identity
        self.request_profiles = request_profiles
        self.jellyfin_collections = jellyfin_collections
        self.jellyfin_brazilian_metadata = jellyfin_brazilian_metadata
        self.audited_imports = audited_imports
        self.audited_allow_seerr = audited_allow_seerr
        self._request_outcomes: dict[tuple[str, str, str], str] = {}
        self.page_size = page_size

    def _outcome(self, candidate: AdmissionCandidate, phase: str, outcome: str) -> None:
        # Only protocol identities may enter structured logs. Alternative sources and
        # malformed upstream fields can carry URLs or credentials in either string.
        if (
            re.fullmatch(r'0*[1-9][0-9]*(?::[0-9]+)?', candidate.source_id) is None
            or re.fullmatch(
                r'(?:movie:tmdb:0*[1-9][0-9]*|season:tmdb:0*[1-9][0-9]*:[0-9]+)',
                candidate.media_key,
            ) is None
        ):
            return
        # Outcomes are code tokens only; upstream messages and bodies never enter logs.
        if not isinstance(outcome, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,95}', outcome) is None:
            outcome = 'unrecognized_outcome'
        identity = candidate.source_id, candidate.media_key, phase
        if self._request_outcomes.get(identity) == outcome:
            return
        self._request_outcomes[identity] = outcome
        LOGGER.info(json.dumps({'event': 'request_outcome',
                               'request_id': f'seerr:{candidate.source_id}',
                               'media_key': candidate.media_key, 'phase': phase,
                               'outcome': outcome}))

    @classmethod
    def _candidate(cls, raw: Mapping[str, object]) -> AdmissionCandidate:
        source_id = raw.get("source_id")
        media_key = raw.get("media_key")
        kind = raw.get("kind")
        if (
            not isinstance(source_id, str)
            or not source_id
            or not isinstance(media_key, str)
            or not media_key
            or not isinstance(kind, str)
            or kind not in cls._budgets
        ):
            raise ValueError("approved request identity is incomplete")
        return AdmissionCandidate(
            request_id=f"seerr:{source_id}",
            source_id=source_id,
            media_key=media_key,
            budget_bytes=cls._budgets[kind],
        )

    async def run_once(self) -> CycleReport:
        hybrid = self.audited_imports is not None and self.audited_allow_seerr
        coverage = AuditedCoverage()
        manifest = None
        if hybrid:
            try:
                manifest = self.audited_imports.prepare_manifest()
                coverage = AuditedCoverage.from_manifest(manifest)
            except Exception as error:
                _log_failure('audited manifest unavailable; all acquisitions paused', error)
                return CycleReport()
        if self.capacity_refresh is not None:
            try:
                await self.capacity_refresh()
            except Exception as error:
                _log_failure('storage queue snapshot unavailable', error)
        if self.jellyfin_collections is not None:
            try:
                await self.jellyfin_collections.reconcile()
            except Exception as error:
                _log_failure('collection artwork reconciliation deferred', error)
        if self.jellyfin_brazilian_metadata is not None:
            try:
                await self.jellyfin_brazilian_metadata.reconcile()
            except Exception as error:
                _log_failure('Brazilian movie metadata reconciliation deferred', error)
        if self.audited_imports is not None:
            try:
                if hybrid:
                    await self.audited_imports.run_once(manifest=manifest)
                else:
                    await self.audited_imports.run_once()
            except Exception as error:
                _log_failure('audited import deferred', error)
                return CycleReport()
            # Operator reservations are independent of the Seerr approval set.
            # Even a malformed manifest must never fall through to cancellation.
            if not hybrid:
                return CycleReport()
        if self.deletion_coordinator is not None and not hybrid:
            try:
                await self.deletion_coordinator.run_once()
            except Exception as error:
                _log_failure("coordinated deletion failed; acquisitions paused this cycle", error)
                return CycleReport()
        can_prioritize = True
        if self.source_reconciler is not None and not hybrid:
            try:
                await self.source_reconciler.reconcile()
            except Exception as error:
                can_prioritize = False
                _log_failure("uncertain source reconciliation failed", error)
        if self.discarded_sources is not None and not hybrid:
            try:
                await self.discarded_sources.reconcile()
            except Exception as error:
                _log_failure("discarded source cleanup deferred", error)
        processed = accepted = deferred = malformed = grabbed = 0
        approved_source_ids: set[str] = set()
        admitted: list[tuple[AdmissionCandidate, str]] = []
        approved_requests = []
        page = 1
        while True:
            batch = await self.source.list_approved(page)
            if not batch:
                break
            for raw in batch:
                try:
                    candidate = self._candidate(raw)
                except ValueError:
                    malformed += 1
                    can_prioritize = False
                    continue
                processed += 1
                approved_source_ids.add(candidate.source_id)
                if coverage.protects(candidate.media_key):
                    deferred += 1
                    self._outcome(candidate, 'admission', 'audited_target_protected')
                    continue
                approved_requests.append(raw)
                result = self.scheduler.admit(candidate)
                if result.accepted:
                    self._request_outcomes.pop(
                        (candidate.source_id, candidate.media_key, 'admission'), None,
                    )
                    accepted += 1
                    if result.reservation_id is not None:
                        admitted.append((candidate, result.reservation_id))
                else:
                    deferred += 1
                    self._outcome(candidate, 'admission', result.reason or 'reservation_deferred')
                    if candidate.media_key.startswith("season:tmdb:"):
                        can_prioritize = False
            page += 1

        blocked_profiles = set()
        if self.request_profiles is not None:
            try:
                if hybrid:
                    blocked_profiles = await self.request_profiles.reconcile(
                        approved_requests, preserve_native_series=coverage.series,
                    )
                else:
                    blocked_profiles = await self.request_profiles.reconcile(approved_requests)
            except Exception as error:
                blocked_profiles = {raw["media_key"] for raw in approved_requests}
                _log_failure("request profile synchronization failed", error)
        if blocked_profiles:
            can_prioritize = False
        if self.manual_source_replacements is not None and not blocked_profiles and not hybrid:
            try:
                await self.manual_source_replacements.run_once()
            except Exception as error:
                _log_failure("manual source replacement deferred", error)
        pending_movies = self.pending_movie_imports() if self.pending_movie_imports else []
        pending_movies = [pending for pending in pending_movies if not coverage.protects(
            pending['media_key'], pending['reservation_id'],
        )]
        pending_ids = {pending["reservation_id"] for pending in pending_movies}
        finalized_movies: set[str] = set()
        finalized_series: set[str] = set()
        for candidate, reservation_id in admitted:
            profile_ready = candidate.media_key not in blocked_profiles
            if profile_ready:
                self._request_outcomes.pop(
                    (candidate.source_id, candidate.media_key, 'profile'), None,
                )
            if not profile_ready:
                deferred += 1
                reasons = getattr(self.request_profiles, 'blocked_reasons', {})
                self._outcome(candidate, 'profile', reasons.get(
                    candidate.media_key, 'request_profile_unverified',
                ))
                if hybrid:
                    continue
            if candidate.media_key.startswith("movie:tmdb:"):
                # Finish new imports before retrying subtitles for published movies.
                if reservation_id in pending_ids:
                    continue
                if self.acquirer is not None and profile_ready:
                    try:
                        outcome = await self.acquirer.acquire(
                            candidate.media_key, reservation_id
                        )
                        self._outcome(candidate, 'acquisition', outcome)
                        if outcome in {"grabbed", "replaced"}:
                            grabbed += 1
                    except Exception as error:
                        self._outcome(candidate, 'acquisition', 'acquisition_failed')
                        _log_failure("movie acquisition failed", error, candidate.media_key)
                if self.finalizer is not None:
                    finalized_movies.add(reservation_id)
                    try:
                        outcome = await self.finalizer.finalize(
                            candidate.media_key, reservation_id
                        )
                        self._outcome(candidate, 'finalization', outcome)
                        if outcome in {"import_requested", "complete"}:
                            LOGGER.info(
                                "movie finalization %s for %s", outcome,
                                candidate.media_key,
                            )
                    except Exception as error:
                        self._outcome(candidate, 'finalization', 'finalization_failed')
                        _log_failure("movie finalization failed", error, candidate.media_key)
            elif candidate.media_key.startswith("season:tmdb:"):
                if self.series_acquirer is not None and profile_ready:
                    try:
                        outcome = await self.series_acquirer.acquire(
                            candidate.media_key, reservation_id
                        )
                        self._outcome(candidate, 'acquisition', outcome)
                        if outcome in {
                            "reservation_inactive", "unsupported_media",
                            "import_guard", "series_not_monitored",
                        }:
                            can_prioritize = False
                        if outcome in {"grabbed", "replaced"}:
                            grabbed += 1
                    except Exception as error:
                        self._outcome(candidate, 'acquisition', 'acquisition_failed')
                        can_prioritize = False
                        _log_failure("series acquisition failed", error, candidate.media_key)
                else:
                    can_prioritize = False
                if self.series_finalizer is not None:
                    finalized_series.add(reservation_id)
                    try:
                        outcome = await self.series_finalizer.finalize(
                            candidate.media_key, reservation_id
                        )
                        self._outcome(candidate, 'finalization', outcome)
                        if outcome in {"import_requested", "complete"}:
                            LOGGER.info(
                                "series finalization %s for %s", outcome,
                                candidate.media_key,
                            )
                    except Exception as error:
                        self._outcome(candidate, 'finalization', 'finalization_failed')
                        _log_failure("series finalization failed", error, candidate.media_key)
        if self.finalizer is not None and self.pending_movie_imports is not None:
            for pending in pending_movies:
                if pending["reservation_id"] in finalized_movies:
                    continue
                try:
                    status = (2 if pending["source_id"] in approved_source_ids
                              else await self.source.get_request_status(pending["source_id"]))
                    if status not in {2, 5}:
                        continue
                    outcome = await self.finalizer.finalize(
                        pending["media_key"], pending["reservation_id"],
                    )
                    if outcome == "complete":
                        LOGGER.info("movie finalization complete for %s", pending["media_key"])
                except Exception as error:
                    _log_failure("pending movie finalization failed", error, pending["media_key"])
        if self.series_finalizer is not None and self.pending_series_imports is not None:
            pending_series = self.pending_series_imports()
            current = {raw["source_id"]: raw for raw in approved_requests}
            pending_ready, fresh_proofs = [], []
            for pending in pending_series:
                if coverage.protects(pending["media_key"], pending["reservation_id"]):
                    continue
                try:
                    proof = current.get(pending["source_id"])
                    if proof is None:
                        lookup = getattr(self.source, "get_series_request", None)
                        proof = await lookup(pending["source_id"]) if lookup is not None else None
                    if (proof is None or proof.get("source_id") != pending["source_id"]
                            or proof.get("media_key") != pending["media_key"]
                            or proof.get("kind") != "season"):
                        continue
                    if pending["source_id"] not in current:
                        fresh_proofs.append(proof)
                    pending_ready.append(pending)
                except Exception as error:
                    _log_failure("pending series proof unavailable", error, pending["media_key"])
            if self.series_identity is not None:
                self.series_identity.update_requests(approved_requests + fresh_proofs)
            for pending in pending_ready:
                try:
                    if pending["reservation_id"] not in finalized_series:
                        await self.series_finalizer.finalize(
                            pending["media_key"], pending["reservation_id"],
                        )
                    await self.series_finalizer.retry_pending_subtitles(
                        pending["media_key"], pending["reservation_id"],
                    )
                except Exception as error:
                    _log_failure("pending series finalization failed", error, pending["media_key"])
        cancelled = (
            await self.cancellation.reconcile(approved_source_ids)
            if self.cancellation is not None else 0
        )
        if can_prioritize and self.movie_prioritizer is not None and not hybrid:
            try:
                await self.movie_prioritizer.prioritize()
            except Exception as error:
                _log_failure("movie download prioritization failed", error)
        self._request_outcomes = {
            identity: outcome for identity, outcome in self._request_outcomes.items()
            if identity[0] in approved_source_ids
        }
        return CycleReport(processed, accepted, deferred, malformed, grabbed, cancelled)
