from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .models import TelemetrySnapshot


class SnapshotPublisher:
    def __init__(self, publish: Callable[[str, bytes], None]) -> None:
        self._publish = publish

    def publish_snapshot(self, topic: str, payload: dict[str, Any]) -> bytes:
        if topic != "homeserver/v1/server/snapshot":
            raise ValueError("telemetry publisher may write only the server snapshot topic")
        snapshot = TelemetrySnapshot.model_validate(payload)
        encoded = snapshot.model_dump_json().encode("utf-8")
        if len(encoded) > 8 * 1024:
            raise ValueError("telemetry snapshot exceeds CYD message limit")
        self._publish(topic, encoded)
        return encoded
