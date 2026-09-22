import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from homeserver_telemetry.alerts import NotificationOutbox
from homeserver_telemetry.notifications import NotificationService


@pytest.fixture
def outbox() -> NotificationOutbox:
    root = Path(".runtime") / f"notifications-{uuid4().hex}"
    root.mkdir(parents=True)
    instance = NotificationOutbox(str(root / "outbox.sqlite"))
    try:
        yield instance
    finally:
        instance.connection.close()
        shutil.rmtree(root, ignore_errors=True)


def test_delivery_sanitizes_secrets_and_retries_transport_failures(
    outbox: NotificationOutbox,
) -> None:
    sent: list[dict] = []
    attempts = 0

    def transport(payload: dict) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("offline")
        sent.append(payload)

    service = NotificationService(outbox=outbox, transport=transport)
    service.enqueue(
        event_type="media.available",
        object_id="movie:tmdb:1",
        generation="g1",
        payload={"message": "ready", "token": "secret", "url": "https://x/?token=secret"},
    )
    service.deliver(now=0)
    assert sent == []
    service.deliver(now=60)
    assert sent[0]["token"] == "<redacted>"
    assert "<redacted>" in sent[0]["url"]
