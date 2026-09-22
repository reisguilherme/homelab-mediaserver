from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from homeserver_control.persistence.db import ReservationResult

from .scheduler import AdmissionCandidate


class ApprovedRequestSource(Protocol):
    async def list_approved(self, page: int) -> list[dict[str, object]]: ...


class ReservationScheduler(Protocol):
    def admit(self, candidate: AdmissionCandidate) -> ReservationResult: ...


@dataclass(frozen=True)
class CycleReport:
    processed: int = 0
    accepted: int = 0
    deferred: int = 0
    malformed: int = 0


class WorkerCycle:
    """Reconcile approved requests into persistent reservations.

    This boundary deliberately stops before any Arr/qBittorrent mutation. A
    request must pass the capacity transaction first; later worker stages can
    consume the persisted reservation without allowing a source to bypass it.
    """

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
        page_size: int = 20,
    ) -> None:
        if not 1 <= page_size <= 100:
            raise ValueError("page size must be between 1 and 100")
        self.source = source
        self.scheduler = scheduler
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
        processed = accepted = deferred = malformed = 0
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
                else:
                    deferred += 1
            if len(batch) < self.page_size:
                break
            page += 1
        return CycleReport(processed, accepted, deferred, malformed)
