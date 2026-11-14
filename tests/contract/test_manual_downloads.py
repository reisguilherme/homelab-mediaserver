from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from homeserver_control.api.app import ControlState, create_app
from homeserver_control.api.downloads import DownloadControl
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.source_requests import SourceReplacementRequests
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.manual_sources import ManualSourceReplacements
from homeserver_telemetry.app import create_app as telemetry_app
from homeserver_telemetry.downloads import DownloadsProxy


@pytest.fixture
def controlled_download(tmp_path):
    path = tmp_path / "control.sqlite"
    repository = ReservationRepository(path)
    repository.initialize()
    reservation = repository.reserve(
        request_id="seerr:1",
        source_id="1",
        media_key="movie:tmdb:1",
        filesystem_id="fixture",
        budget_bytes=0,
        free_bytes=10_000,
        total_bytes=20_000,
    )
    permits = PermitRegistry(path)
    permit = permits.issue(
        infohash="a" * 40,
        destination="/data/torrents",
        category="radarr",
        reservation_id=reservation.reservation_id,
        metadata_sha256="b" * 64,
        selected_files=("movie.mkv",),
        budget_bytes=1000,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        capacity=CapacityEvidence(free_bytes=10_000, remaining_by_hash={}),
    )
    permits.authorize(
        token=permit.token,
        infohash=permit.infohash,
        destination=permit.destination,
        metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    entry = {
        "hash": permit.infohash,
        "name": "Film 1080p BluRay",
        "amount_left": 800,
        "total_size": 1000,
        "downloaded": 200,
        "state": "downloading",
        "progress": 0.2,
        "num_seeds": 12,
        "dlspeed": 500,
        "save_path": "/data/torrents",
        "private_token": "never-return-this",
    }
    calls = []

    def gateway(request):
        calls.append(request.method)
        assert request.headers["X-Arr-Token"] == "arr-secret"
        return httpx.Response(200, json=[entry])

    requests = SourceReplacementRequests(path)
    downloads = DownloadControl(
        db_path=path,
        requests=requests,
        gateway_url="http://gateway:8081",
        arr_token="arr-secret",
        transport=httpx.MockTransport(gateway),
    )
    state = ControlState(
        db_path=path,
        admin_token="admin-secret",
        csrf_token="csrf-secret",
        downloads=downloads,
    )
    return TestClient(create_app(state=state)), requests, permit, entry, calls, repository


def test_manual_download_list_is_authenticated_and_contains_no_internal_secrets(
    controlled_download,
):
    client, _, permit, _, calls, _ = controlled_download
    assert client.get("/api/v1/downloads").status_code == 401
    response = client.get("/api/v1/downloads", headers={"X-Admin-Token": "admin-secret"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["infohash"] == permit.infohash
    assert item["replace_allowed"] is True
    assert item["seeds"] == 12
    assert item["remaining_bytes"] == 800
    assert "arr-secret" not in response.text
    assert permit.token not in response.text
    assert "never-return-this" not in response.text
    assert calls == ["GET"]


def test_manual_replacement_is_csrf_guarded_specific_and_idempotent(controlled_download):
    client, requests, permit, _, calls, _ = controlled_download
    path = f"/api/v1/downloads/{permit.infohash}/replace"
    payload = {"permit_id": permit.permit_id}
    assert (
        client.post(path, headers={"X-Admin-Token": "admin-secret"}, json=payload).status_code
        == 403
    )
    headers = {"X-Admin-Token": "admin-secret", "X-CSRF-Token": "csrf-secret"}
    first = client.post(path, headers=headers, json=payload)
    repeated = client.post(path, headers=headers, json=payload)
    assert first.status_code == repeated.status_code == 202
    assert first.json()["request_id"] == repeated.json()["request_id"]
    assert first.json()["state"] == "queued"
    assert requests.claim() is not None
    assert requests.claim() is None
    assert set(calls) == {"GET"}, "HTTP action must not stop or replace the source"
    assert client.post(path, headers=headers, json={"permit_id": "outdated"}).status_code == 409


def test_completed_or_cancelled_sources_cannot_be_manually_replaced(controlled_download):
    client, _, permit, entry, _, repository = controlled_download
    headers = {"X-Admin-Token": "admin-secret", "X-CSRF-Token": "csrf-secret"}
    path = f"/api/v1/downloads/{permit.infohash}/replace"
    entry["amount_left"] = 0
    entry["progress"] = 1
    assert (
        client.post(path, headers=headers, json={"permit_id": permit.permit_id}).status_code == 409
    )
    entry["amount_left"] = 800
    entry["progress"] = 0.2
    with repository._connect() as connection:
        connection.execute("UPDATE requests SET state='cancel_requested'")
    assert (
        client.post(path, headers=headers, json={"permit_id": permit.permit_id}).status_code == 409
    )


def _panel_proxy():
    calls = []

    def api(request):
        assert request.headers["X-Admin-Token"] == "internal-admin-secret"
        calls.append(request)
        if request.method == "POST":
            assert request.headers["X-CSRF-Token"] == "internal-csrf-secret"
            return httpx.Response(202, json={"request_id": "job1", "state": "queued"})
        return httpx.Response(200, json={"items": []})

    proxy = DownloadsProxy(
        username="root",
        password="fixture-password",
        admin_token="internal-admin-secret",
        csrf_token="internal-csrf-secret",
        transport=httpx.MockTransport(api),
    )
    return TestClient(telemetry_app(status_provider=lambda: {}, downloads_proxy=proxy)), calls


def test_panel_downloads_require_basic_and_reject_cross_origin_and_form_posts():
    client, calls = _panel_proxy()
    assert client.get("/api/v1/downloads").status_code == 401
    assert client.get("/api/v1/downloads", auth=("root", "wrong")).status_code == 401
    assert client.get("/api/v1/downloads", auth=("root", "fixture-password")).status_code == 200
    path = f"/api/v1/downloads/{'a' * 40}/replace"
    payload = {"permit_id": "permit-id"}
    auth = ("root", "fixture-password")
    assert client.post(path, auth=auth, json=payload).status_code == 403
    headers = {"X-HomeServer-Action": "replace", "Origin": "http://evil.example"}
    assert client.post(path, auth=auth, headers=headers, json=payload).status_code == 403
    headers["Origin"] = "http://testserver"
    assert client.post(path, auth=auth, headers=headers, data=payload).status_code == 403
    response = client.post(path, auth=auth, headers=headers, json=payload)
    assert response.status_code == 202
    assert len(calls) == 2
    page = client.get("/ui/downloads", auth=auth)
    assert "internal-admin-secret" not in page.text
    assert "internal-csrf-secret" not in page.text
    assert "fixture-password" not in page.text
    assert 'id="downloads-list"' in page.text
    assert "Trocar fonte" in page.text
    assert "frame-ancestors 'none'" in page.headers['content-security-policy']


def test_panel_missing_credentials_fails_closed():
    proxy = DownloadsProxy(username="root", password="", admin_token="", csrf_token="")
    client = TestClient(telemetry_app(status_provider=lambda: {}, downloads_proxy=proxy))
    assert client.get("/api/v1/downloads", auth=("root", "anything")).status_code == 503


def test_download_controls_work_behind_status_prefix_without_relaxing_origin():
    client, calls = _panel_proxy()
    client.base_url = "http://homelab.example.ts.net"
    auth = ("root", "fixture-password")
    headers = {"X-HomeServer-Path": "/status"}
    page = client.get("/ui/downloads", auth=auth, headers=headers)
    assert page.status_code == 200
    assert 'href="/status/"' in page.text
    assert "fetch('/status/api/v1/downloads'" in page.text
    assert "fetch(`/status/api/v1/downloads/" in page.text
    path = f"/api/v1/downloads/{'a' * 40}/replace"
    headers |= {"X-HomeServer-Action": "replace", "Origin": "http://homelab.example.ts.net"}
    response = client.post(path, auth=auth, headers=headers, json={"permit_id": "fixture"})
    assert response.status_code == 202
    headers["Origin"] = "http://evil.example"
    assert client.post(path, auth=auth, headers=headers,
                       json={"permit_id": "fixture"}).status_code == 403
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_worker_manual_command_is_durable_and_runs_once(controlled_download):
    client, requests, permit, _, _, _ = controlled_download
    response = client.post(
        f"/api/v1/downloads/{permit.infohash}/replace",
        headers={"X-Admin-Token": "admin-secret", "X-CSRF-Token": "csrf-secret"},
        json={"permit_id": permit.permit_id},
    )
    assert response.status_code == 202
    restarted_requests = SourceReplacementRequests(requests.db_path)
    calls = []

    class Acquirer:
        async def manual_replace(self, media_key, reservation_id, permit_id, *, scope_key):
            calls.append((media_key, reservation_id, permit_id, scope_key))
            return "no_eligible_release"

    worker = ManualSourceReplacements(
        requests=restarted_requests,
        permits=None,
        movie_acquirer=Acquirer(),
        series_acquirer=None,
    )
    assert await worker.run_once() == "no_eligible_release"
    assert await worker.run_once() == "idle"
    assert len(calls) == 1
    assert calls[0][2] == permit.permit_id
    assert requests.latest(permit.permit_id)["state"] == "no_candidate"


@pytest.mark.asyncio
async def test_worker_refuses_source_cancelled_after_dashboard_click(controlled_download):
    client, requests, permit, _, _, repository = controlled_download
    client.post(
        f"/api/v1/downloads/{permit.infohash}/replace",
        headers={"X-Admin-Token": "admin-secret", "X-CSRF-Token": "csrf-secret"},
        json={"permit_id": permit.permit_id},
    )
    with repository._connect() as connection:
        connection.execute("UPDATE requests SET state='cancel_requested'")
    worker = ManualSourceReplacements(
        requests=requests,
        permits=None,
        movie_acquirer=None,
        series_acquirer=None,
    )
    assert await worker.run_once() == "source_changed"
    assert requests.latest(permit.permit_id)["state"] == "stale"
