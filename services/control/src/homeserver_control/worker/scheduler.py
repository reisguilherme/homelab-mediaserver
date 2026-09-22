from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeserver_control.persistence.db import ReservationRepository, ReservationResult


@dataclass(frozen=True)
class AdmissionCandidate:
    request_id: str
    source_id: str
    media_key: str
    budget_bytes: int


@dataclass(frozen=True)
class FilesystemSnapshot:
    filesystem_id: str | None
    free_bytes: int
    total_bytes: int
    measured_at: float
    stale_after_seconds: float = 30.0

    def usable(self, *, now: float) -> bool:
        return (
            bool(self.filesystem_id)
            and self.free_bytes >= 0
            and self.total_bytes > 0
            and now - self.measured_at <= self.stale_after_seconds
        )


class AdmissionScheduler:
    """Reserve first, then let a worker create the external effect."""

    def __init__(
        self,
        *,
        repository: ReservationRepository,
        snapshot_provider: Callable[[], FilesystemSnapshot],
        clock: Callable[[], float],
    ) -> None:
        self.repository = repository
        self.snapshot_provider = snapshot_provider
        self.clock = clock

    def admit(self, candidate: AdmissionCandidate) -> ReservationResult:
        snapshot = self.snapshot_provider()
        if not snapshot.usable(now=self.clock()):
            return ReservationResult(False, reason="filesystem_snapshot_unavailable")
        return self.repository.reserve(
            request_id=candidate.request_id,
            source_id=candidate.source_id,
            media_key=candidate.media_key,
            filesystem_id=snapshot.filesystem_id or "",
            budget_bytes=candidate.budget_bytes,
            free_bytes=snapshot.free_bytes,
            total_bytes=snapshot.total_bytes,
        )
