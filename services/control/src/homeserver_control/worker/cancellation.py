"""Reconcile Seerr approval withdrawals without deleting active downloads."""

from __future__ import annotations

import logging
from typing import Protocol

from homeserver_control.persistence.db import ReservationRepository

LOGGER = logging.getLogger(__name__)


class RequestStatusSource(Protocol):
    async def get_request_status(self, source_id: str) -> int | None: ...


class CancellationReconciler:
    def __init__(
        self, *, source: RequestStatusSource, repository: ReservationRepository
    ) -> None:
        self.source = source
        self.repository = repository

    async def reconcile(self, approved_source_ids: set[str]) -> int:
        cancelled = 0
        for reservation_id, source_id in self.repository.active_seerr_requests():
            if source_id in approved_source_ids:
                continue
            status = await self.source.get_request_status(source_id)
            # Completed and failed requests are not proof of withdrawal.
            if status is not None and status not in {1, 3}:
                continue
            result = self.repository.cancel_unstarted(reservation_id)
            if result == "cancelled":
                cancelled += 1
            elif result == "download_started":
                LOGGER.warning(
                    "Seerr request %s was withdrawn after download started; "
                    "reservation retained for manual reconciliation",
                    source_id,
                )
        return cancelled
