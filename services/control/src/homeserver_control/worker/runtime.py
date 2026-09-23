from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from homeserver_control.persistence.db import ReservationResult

from .scheduler import AdmissionCandidate


class ApprovedRequestSource(Protocol):
    async def list_approved(self, page: int) -> list[dict[str, object]]: ...


class ReservationScheduler(Protocol):
    def admit(self, candidate: AdmissionCandidate) -> ReservationResult: ...


class MovieAcquisition(Protocol):
    async def acquire(self, media_key: str, reservation_id: str) -> str: ...


class MovieFinalization(Protocol):
    async def finalize(self, media_key: str, reservation_id: str) -> str: ...


class Cancellation(Protocol):
    async def reconcile(self, approved_source_ids: set[str]) -> int: ...


LOGGER = logging.getLogger(__name__)


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
        series_finalizer: MovieFinalization | None = None,
        cancellation: Cancellation | None = None,
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
        self.page_size = page_size

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
        processed = accepted = deferred = malformed = grabbed = 0
        approved_source_ids: set[str] = set()
        admitted: list[tuple[AdmissionCandidate, str]] = []
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
                    continue
                processed += 1
                approved_source_ids.add(candidate.source_id)
                result = self.scheduler.admit(candidate)
                if result.accepted:
                    accepted += 1
                    if result.reservation_id is not None:
                        admitted.append((candidate, result.reservation_id))
                else:
                    deferred += 1
            page += 1

        for candidate, reservation_id in admitted:
            if candidate.media_key.startswith("movie:tmdb:"):
                if self.acquirer is not None:
                    try:
                        outcome = await self.acquirer.acquire(
                            candidate.media_key, reservation_id
                        )
                        if outcome == "grabbed":
                            grabbed += 1
                    except Exception:
                        LOGGER.exception(
                            "movie acquisition failed for %s", candidate.media_key
                        )
                if self.finalizer is not None:
                    try:
                        outcome = await self.finalizer.finalize(
                            candidate.media_key, reservation_id
                        )
                        if outcome in {"import_requested", "complete"}:
                            LOGGER.info(
                                "movie finalization %s for %s", outcome,
                                candidate.media_key,
                            )
                    except Exception:
                        LOGGER.exception(
                            "movie finalization failed for %s", candidate.media_key
                        )
            elif candidate.media_key.startswith("season:tmdb:"):
                if self.series_acquirer is not None:
                    try:
                        outcome = await self.series_acquirer.acquire(
                            candidate.media_key, reservation_id
                        )
                        if outcome == "grabbed":
                            grabbed += 1
                    except Exception:
                        LOGGER.exception(
                            "series acquisition failed for %s", candidate.media_key
                        )
                if self.series_finalizer is not None:
                    try:
                        outcome = await self.series_finalizer.finalize(
                            candidate.media_key, reservation_id
                        )
                        if outcome in {"import_requested", "complete"}:
                            LOGGER.info(
                                "series finalization %s for %s", outcome,
                                candidate.media_key,
                            )
                    except Exception:
                        LOGGER.exception(
                            "series finalization failed for %s", candidate.media_key
                        )
        cancelled = (
            await self.cancellation.reconcile(approved_source_ids)
            if self.cancellation is not None else 0
        )
        return CycleReport(processed, accepted, deferred, malformed, grabbed, cancelled)
