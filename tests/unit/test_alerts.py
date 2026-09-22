from homeserver_telemetry.alerts import NotificationOutbox


def test_outbox_deduplicates_and_does_not_store_provider_tokens() -> None:
    outbox = NotificationOutbox(":memory:")
    outbox.enqueue(
        event_type="media.available",
        object_id="movie:tmdb:1",
        generation="1",
        payload={"message": "ready", "url": "https://example.test/notify?token=do-not-store"},
    )
    outbox.enqueue(
        event_type="media.available",
        object_id="movie:tmdb:1",
        generation="1",
        payload={"message": "duplicate"},
    )
    pending = outbox.pending()
    assert len(pending) == 1
    assert "do-not-store" not in pending[0]["payload_json"]
    assert "token=" not in pending[0]["payload_json"]


def test_outbox_can_mark_sent_and_retry_failed_items() -> None:
    outbox = NotificationOutbox(":memory:")
    item_id = outbox.enqueue(
        event_type="service.error", object_id="api", generation="3", payload={"message": "down"}
    )
    outbox.mark_failed(item_id, retry_at=123.0)
    assert outbox.pending(now=123.0)[0]["attempts"] == 1
    outbox.mark_sent(item_id)
    assert outbox.pending(now=123.0) == []
