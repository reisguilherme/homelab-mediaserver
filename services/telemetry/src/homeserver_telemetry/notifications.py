from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .alerts import NotificationOutbox


class NotificationService:
    """Persist first, deliver later, and retry without exposing credentials."""

    def __init__(
        self, *, outbox: NotificationOutbox, transport: Callable[[dict[str, Any]], None]
    ) -> None:
        self.outbox = outbox
        self.transport = transport

    def enqueue(
        self,
        *,
        event_type: str,
        object_id: str,
        generation: str,
        payload: dict[str, Any],
    ) -> int:
        return self.outbox.enqueue(
            event_type=event_type,
            object_id=object_id,
            generation=generation,
            payload=payload,
        )

    def deliver(self, *, now: float) -> int:
        delivered = 0
        for item in self.outbox.pending(now=now):
            try:
                payload = json.loads(item["payload_json"])
                if not isinstance(payload, dict):
                    raise ValueError("notification payload is not an object")
                self.transport(payload)
            except Exception:
                attempts = int(item.get("attempts", 0)) + 1
                retry_at = now + min(3600, 60 * (2 ** max(0, attempts - 1)))
                self.outbox.mark_failed(int(item["id"]), retry_at=retry_at)
            else:
                self.outbox.mark_sent(int(item["id"]))
                delivered += 1
        return delivered
