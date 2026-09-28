from homeserver_control.configuration.native_config import plan_settings
from homeserver_control.configuration.qbittorrent import (
    adopt_qbit_preferences,
    effective_qbit_preferences,
    qbit_preferences,
)


def test_native_diff_restricts_ownership_and_redacts():
    changes = plan_settings("example", {"password": "old", "manual": 1}, {"password": "new"})
    assert len(changes) == 1
    assert changes[0].before == changes[0].after == "[redacted]"
    assert not plan_settings("example", {"managed": 2, "manual": 1}, {"managed": 2})
    for key in ("startup.account", "account"):
        assert "private-user" not in repr(plan_settings("native", {}, {key: "private-user"}))
    provider = {"fields": [{"name": "baseUrl", "value": "https://fixture/?key=private-key"}]}
    assert "private-key" not in repr(plan_settings("prowlarr", {}, provider))


def test_upload_conversion_and_import_exact_bytes():
    assert qbit_preferences({})["upnp"] is False
    assert qbit_preferences({})["web_ui_upnp"] is False
    assert qbit_preferences({})["up_limit"] == 2_500_000
    assert qbit_preferences({"HOMESERVER_UPLOAD_LIMIT_BYTES": "2499584"})["up_limit"] == 2499584
    assert effective_qbit_preferences({})["up_limit"] == 2499584


def test_adopt_qbit_keeps_exact_effective_queue_and_bandwidth():
    actual = qbit_preferences({}) | {
        "up_limit": 2499584,
        "max_active_downloads": 3,
        "max_active_uploads": 9,
        "max_active_torrents": 12,
        "max_connec": 731,
        "max_connec_per_torrent": 82,
    }
    adopted = adopt_qbit_preferences(actual)
    assert adopted["HOMESERVER_UPLOAD_LIMIT_BYTES"] == "2499584"
    assert qbit_preferences(adopted) == actual


def test_qbit_connection_limits_follow_canonical_fields():
    desired = qbit_preferences(
        {
            "HOMESERVER_TORRENT_MAX_CONNECTIONS": "720",
            "HOMESERVER_TORRENT_MAX_CONNECTIONS_PER_TORRENT": "80",
        }
    )
    assert desired["max_connec"] == 720
    assert desired["max_connec_per_torrent"] == 80
    assert desired["queueing_enabled"] is True
