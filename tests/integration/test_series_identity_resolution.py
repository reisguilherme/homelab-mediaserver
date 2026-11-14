"""Current Seerr binding admits only the proven native series in both paths."""

import httpx
import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.series_acquisition import SeriesAcquirer
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.series_identity import SeriesIdentityResolver


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["acquirer", "finalizer"])
@pytest.mark.parametrize("fault", [None, "positive_tmdb", "ambiguous", "native_id", "tvdb"])
async def test_series_paths_resolve_only_the_current_authoritative_binding(
    tmp_path, consumer, fault,
):
    repository = ReservationRepository(tmp_path / "control.sqlite")
    repository.initialize()
    reserved = repository.reserve(
        request_id="seerr:3:1", source_id="3:1", media_key="season:tmdb:97546:1",
        filesystem_id="fixture", budget_bytes=1_000_000,
        free_bytes=500_000_000, total_bytes=600_000_000,
    )
    resolver = SeriesIdentityResolver()
    resolver.update_requests([{
        "source_id": "3:1", "kind": "season", "media_key": "season:tmdb:97546:1",
        "tvdb_id": 383203, "external_service_id": 6,
    }])
    series = {"id": 6, "tmdbId": 0, "tvdbId": 383203, "monitored": True,
              "path": "/data/media/tv/Fixture"}
    if fault == "positive_tmdb":
        series["tmdbId"] = 999
    elif fault == "native_id":
        series["id"] = 7
    elif fault == "tvdb":
        series["tvdbId"] = 999
    catalog = [series]
    if fault == "ambiguous":
        catalog.append(series | {"id": 7})
    episode_queries = []

    def handler(request):
        if request.url.path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=catalog)
        if request.url.path == "/api/v3/episode":
            episode_queries.append(dict(request.url.params))
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected fixture request: {request.method} {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        common = {
            "repository": repository, "permits": PermitRegistry(repository.path),
            "sonarr_url": "http://sonarr:8989", "sonarr_api_key": "fixture",
            "client": client, "series_identity": resolver,
        }
        if consumer == "acquirer":
            acquirer = SeriesAcquirer(prowlarr_url="http://prowlarr:9696", **common)
            result = await acquirer.acquire("season:tmdb:97546:1", reserved.reservation_id)
            assert result == ("season_not_monitored" if fault is None else "series_not_monitored")
        else:
            finalizer = SeriesFinalizer(
                torrent_root=tmp_path, gateway_url="http://gateway:8081", arr_token="fixture",
                **common,
            )
            context = {}
            assert await finalizer._episodes(97546, media_context=context) == []
            assert context == (series | {"tmdbId": 97546} if fault is None else {})
    assert episode_queries == ([{"seriesId": "6"}] if fault is None else [])
    assert series["tmdbId"] == (999 if fault == "positive_tmdb" else 0)
