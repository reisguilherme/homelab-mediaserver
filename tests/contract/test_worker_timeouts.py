"""Configured timeouts must reach the actual HTTPX request boundary."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_common.env import load_settings
from homeserver_control.api.deletion_capture import DeletionAdmission
from homeserver_control.gateway.app import _configured_upstream
from homeserver_control.gateway.permits import Permit, PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.acquisition import MovieAcquirer
from homeserver_control.worker.movie_priority import MoviePrioritizer
from homeserver_control.worker.series_acquisition import SeriesAcquirer
from homeserver_control.worker.source_reconciliation import SourceReconciler
from homeserver_control.worker.subdl import SubDLSource

SRT = b"1\n00:00:01,000 --> 00:00:02,000\nFixture subtitle\n"


def test_runtime_qbit_login_and_reads_consume_generic_timeout(monkeypatch):
    observed = []

    def handler(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, text="Ok." if request.method == "POST" else "5.1.2")

    original_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setenv("HOMESERVER_QBIT_USERNAME", "fixture")
    monkeypatch.setenv("HOMESERVER_QBIT_PASSWORD", "fixture")
    monkeypatch.setenv("HOMESERVER_HTTP_TIMEOUT_SECONDS", "37")
    adapter = _configured_upstream()
    try:
        assert adapter.read("/api/v2/app/version") == "5.1.2"
    finally:
        adapter.client.close()
    assert len(observed) == 2
    assert all(set(timeout.values()) == {37.0} for timeout in observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("injected", [True, False])
async def test_deletion_preflight_preserves_injected_or_configured_timeout(
    tmp_path, monkeypatch, injected
):
    observed = []

    def handler(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(200, json={"fixture": True})

    original_client = httpx.AsyncClient
    if not injected:
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs),
        )
    async with original_client(timeout=37, transport=httpx.MockTransport(handler)) as client:
        admission = DeletionAdmission(
            jobs=None,
            media_root=tmp_path,
            snapshot_path=tmp_path / "capacity.json",
            filesystem_id="fixture",
            jellyfin_url="http://jellyfin",
            radarr_url="http://radarr",
            radarr_api_key="fixture",
            sonarr_url="http://sonarr",
            sonarr_api_key="fixture",
            client=client if injected else None,
            http_timeout_seconds=9 if injected else 37,
        )
        assert await admission._get_json("http://jellyfin/Users", headers={}) == {"fixture": True}
    assert len(observed) == 1
    assert set(observed[0].values()) == {37.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["movie", "episode"])
async def test_subdl_search_and_stream_preserve_injected_timeout(kind):
    observed = []
    title = "Fixture.2026.1080p.WEB" if kind == "movie" else "Fixture.S01E01.1080p.WEB"

    def handler(request):
        observed.append(request.extensions["timeout"])
        if request.url.host == "dl.subdl.com":
            return httpx.Response(200, content=SRT)
        scope = {} if kind == "movie" else {"season": 1, "episode": 1}
        return httpx.Response(
            200,
            json={
                "status": True,
                "results": [{"tmdb_id": 10, "type": "movie" if kind == "movie" else "tv"}],
                "subtitles": [
                    {
                        **scope,
                        "language": "BR_PT",
                        "unpack_files": [
                            {
                                **scope,
                                "language": "BR_PT",
                                "format": "srt",
                                "size": len(SRT),
                                "release_name": title,
                                "url": "/subtitle/123/fixture",
                            }
                        ],
                    }
                ],
            },
        )

    async with httpx.AsyncClient(timeout=37, transport=httpx.MockTransport(handler)) as client:
        source = SubDLSource(api_key="fixture-key", client=client)
        if kind == "movie":
            result = await source.fetch_movie(tmdb_id=10, release_titles=[title])
        else:
            result = await source.fetch(tmdb_id=10, release_title=title, season=1, episode=1)
    assert result == SRT
    assert len(observed) == 2
    assert all(set(timeout.values()) == {37.0} for timeout in observed)


@pytest.mark.asyncio
async def test_priority_and_reconciliation_preserve_injected_timeout():
    observed = []

    def handler(request):
        observed.append(request.extensions["timeout"])
        return httpx.Response(
            200,
            json={"state": "unchanged", "count": 0}
            if request.url.path.endswith("prioritize-movies")
            else {"state": "confirmed"},
        )

    permit = Permit(
        permit_id="fixture",
        token="fixture-token",
        infohash="a" * 40,
        destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        state="unknown",
    )

    class Uncertain:
        def list_uncertain(self, **_kwargs):
            return [permit]

    async with httpx.AsyncClient(timeout=37, transport=httpx.MockTransport(handler)) as client:
        options = {"gateway_url": "http://gateway", "arr_token": "fixture", "client": client}
        assert await MoviePrioritizer(**options).prioritize() == "unchanged"
        assert await SourceReconciler(permits=Uncertain(), **options).reconcile() == 1
    assert len(observed) == 2
    assert all(set(timeout.values()) == {37.0} for timeout in observed)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["movie", "series"])
async def test_arr_release_search_uses_separate_configured_timeout(tmp_path, kind):
    env = tmp_path / ".env"
    env.write_text("HOMESERVER_HTTP_TIMEOUT_SECONDS=37\nHOMESERVER_SEARCH_TIMEOUT_SECONDS=123\n")
    settings = load_settings(env)
    repository = ReservationRepository(tmp_path / "control.sqlite")
    repository.initialize()
    reservation = repository.reserve(
        request_id="fixture",
        source_id="fixture",
        media_key="movie:tmdb:10",
        filesystem_id="fixture",
        budget_bytes=0,
        free_bytes=100,
        total_bytes=100,
    )
    permits = PermitRegistry(repository.path)
    observed = []

    def handler(request):
        observed.append((request.url.path, request.extensions["timeout"]))
        payload = {
            "/api/v3/config/downloadclient": {"enableCompletedDownloadHandling": False},
            "/api/v3/config/mediamanagement": {"copyUsingHardlinks": True},
            "/api/v3/movie": [{"tmdbId": 10, "id": 1, "hasFile": False}],
            "/api/v3/release": [],
        }[request.url.path]
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(
        timeout=settings.http_timeout_seconds, transport=httpx.MockTransport(handler)
    ) as client:
        options = {
            "repository": repository,
            "permits": permits,
            "prowlarr_url": "http://prowlarr",
            "client": client,
            "search_timeout_seconds": settings.search_timeout_seconds,
        }
        if kind == "movie":
            acquirer = MovieAcquirer(
                radarr_url="http://radarr", radarr_api_key="fixture", **options
            )
            assert (
                await acquirer.acquire("movie:tmdb:10", reservation.reservation_id)
                == "no_eligible_release"
            )
        else:
            acquirer = SeriesAcquirer(
                sonarr_url="http://sonarr", sonarr_api_key="fixture", **options
            )
            assert [
                item
                async for item in acquirer._eligible_releases(
                    series_id=1, episode_id=1, season=1, episode=1, tmdb_id=10
                )
            ] == []
    assert any(path == "/api/v3/release" for path, _ in observed)
    for path, timeout in observed:
        assert set(timeout.values()) == {123.0 if path == "/api/v3/release" else 37.0}
