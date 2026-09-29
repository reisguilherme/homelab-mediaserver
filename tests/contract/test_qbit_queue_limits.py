import json
from urllib.parse import parse_qs

import httpx
import pytest

from homeserver_common.env import load_settings, serialize_env
from homeserver_control.configuration.qbittorrent import (
    adopt_qbit_preferences,
    apply_qbit_settings,
    effective_qbit_preferences,
    qbit_preferences,
)


def test_default_queue_allows_ten_downloads_without_queuing_finished_seeds():
    desired = qbit_preferences({})
    assert desired["queueing_enabled"] is True
    assert desired["max_active_downloads"] == 10
    assert desired["max_active_uploads"] == -1
    assert desired["max_active_torrents"] == -1
    assert desired["dont_count_slow_torrents"] is False
    assert desired["max_ratio"] == -1
    assert desired["max_ratio_enabled"] is False
    assert desired["max_seeding_time"] == -1
    assert desired["max_seeding_time_enabled"] is False
    assert desired["max_inactive_seeding_time"] == -1
    assert desired["max_inactive_seeding_time_enabled"] is False


def test_unlimited_seeding_removes_a_legacy_total_cap_without_removing_download_cap():
    desired = qbit_preferences({
        "HOMESERVER_DOWNLOAD_MAX_ACTIVE": "10",
        "HOMESERVER_SEED_MAX_ACTIVE": "-1",
        "HOMESERVER_TORRENT_MAX_ACTIVE": "12",
        "HOMESERVER_UPLOAD_LIMIT_BYTES": "2499584",
    })
    assert desired["max_active_downloads"] == 10
    assert desired["max_active_uploads"] == desired["max_active_torrents"] == -1
    assert desired["up_limit"] == 2_499_584


@pytest.mark.asyncio
async def test_native_queue_update_keeps_upload_bandwidth_and_network_preferences():
    state = {
        "queueing_enabled": True,
        "max_active_downloads": 4,
        "max_active_uploads": 8,
        "max_active_torrents": 12,
        "dont_count_slow_torrents": True,
        "up_limit": 2_499_584,
        "dht": True,
        "pex": True,
        "encryption": 1,
        "proxy_type": 0,
        "bittorrent_protocol": 0,
    }
    writes = []

    def handler(request):
        if request.url.path == "/api/v2/auth/login":
            return httpx.Response(200, text="Ok.")
        if request.url.path == "/api/v2/torrents/categories":
            return httpx.Response(200, json={"radarr": {}, "sonarr": {}})
        if request.url.path == "/api/v2/app/setPreferences":
            payload = json.loads(parse_qs(request.content.decode())["json"][0])
            writes.append(payload)
            state.update(payload)
            return httpx.Response(200)
        assert request.url.path == "/api/v2/app/preferences" and request.method == "GET"
        return httpx.Response(200, json=state)

    settings = {
        "HOMESERVER_DOWNLOAD_MAX_ACTIVE": "10",
        "HOMESERVER_SEED_MAX_ACTIVE": "-1",
        "HOMESERVER_TORRENT_MAX_ACTIVE": "12",
        "HOMESERVER_UPLOAD_LIMIT_BYTES": "2499584",
    }
    async with httpx.AsyncClient(
        base_url="http://qbittorrent", transport=httpx.MockTransport(handler)
    ) as client:
        assert await apply_qbit_settings(settings, client)
        assert not await apply_qbit_settings(settings, client)
    assert len(writes) == 1
    assert state["max_active_downloads"] == 10
    assert state["max_active_uploads"] == state["max_active_torrents"] == -1
    assert state["dont_count_slow_torrents"] is False
    assert state["up_limit"] == 2_499_584
    assert state["dht"] is True and state["pex"] is True
    assert state["encryption"] == 1
    assert state["proxy_type"] == state["bittorrent_protocol"] == 0
    assert not {"up_limit", "dht", "pex", "encryption", "proxy_type", "bittorrent_protocol"} & (
        writes[0].keys()
    )


def test_native_unlimited_values_round_trip_through_canonical_env(tmp_path):
    native = effective_qbit_preferences({}) | {
        "max_active_downloads": 10,
        "max_active_uploads": -1,
        "max_active_torrents": -1,
        "up_limit": 2_499_584,
        "dont_count_slow_torrents": False,
    }
    path = tmp_path / ".env"
    path.write_text(serialize_env(adopt_qbit_preferences(native)), encoding="utf-8")
    settings = load_settings(path, mode="dev")
    assert settings.download_max_active == 10
    assert settings.seed_max_active == settings.torrent_max_active == -1
    assert effective_qbit_preferences(settings) == native


def test_explicit_positive_seed_and_total_limits_still_work():
    desired = qbit_preferences({
        "HOMESERVER_DOWNLOAD_MAX_ACTIVE": "3",
        "HOMESERVER_SEED_MAX_ACTIVE": "9",
        "HOMESERVER_TORRENT_MAX_ACTIVE": "12",
    })
    assert desired["max_active_downloads"] == 3
    assert desired["max_active_uploads"] == 9
    assert desired["max_active_torrents"] == 12
