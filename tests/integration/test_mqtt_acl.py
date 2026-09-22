from pathlib import Path


def test_cyd_acl_is_read_only_for_server_snapshots() -> None:
    acl = Path("deploy/mosquitto/acl").read_text(encoding="utf-8")
    assert "user telemetry-publisher" in acl
    assert "topic write homeserver/v1/server/#" in acl
    assert "user cyd-01" in acl
    assert "topic read homeserver/v1/server/#" in acl
    assert "topic write homeserver/v1/cyd-01/availability" in acl
    assert "user cyd-01\ntopic write homeserver/v1/server/#" not in acl


def test_mqtt_broker_disables_anonymous_and_caps_cyd_payload() -> None:
    config = Path("deploy/mosquitto/mosquitto.conf").read_text(encoding="utf-8")
    assert "allow_anonymous false" in config
    assert "message_size_limit 8192" in config
