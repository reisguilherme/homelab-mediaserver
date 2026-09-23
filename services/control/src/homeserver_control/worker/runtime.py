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


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleReport:
    processed: int = 0
    accepted: int = 0
    deferred: int = 0
    malformed: int = 0
    grabbed: int = 0


class WorkerCycle:
    """Reserve approved requests before any optional movie acquisition."""

    _budgets = {
        "movie": 50_000_000_000,
        "episode": 5_000_000_000,
        "season": 100_000_000_000,
    }

    def __init__(
        self,
        *,
        source: ApprovedRequestSource,
        scheduler: ReservationScheduler,
        acquirer: MovieAcquisition | None = None,
        page_size: int = 20,
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("page size must be between 1 and 100")
        self.source = source
        self.scheduler = scheduler
        self.acquirer = acquirer
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
                result = self.scheduler.admit(candidate)
                if result.accepted:
                    accepted += 1
                    if (
                        self.acquirer is not None
                        and candidate.media_key.startswith("movie:tmdb:")
                        and result.reservation_id is not None
                    ):
                        try:
                            outcome = await self.acquirer.acquire(
                                candidate.media_key, result.reservation_id
                            )
                            if outcome == "grabbed":
                                grabbed += 1
                        except Exception:
                            LOGGER.exception("movie acquisition failed for %s", candidate.media_key)
                else:
                    deferred += 1
            if len(batch) < self.page_size:
                break
            page += 1
        return CycleReport(processed, accepted, deferred, malformed, grabbed)
