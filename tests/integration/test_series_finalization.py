from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.validation import ValidationError, ValidationResult


@pytest.mark.asyncio
async def test_completed_episode_is_validated_before_one_sonarr_import(tmp_path, monkeypatch):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:3:4", source_id="3:4",
        media_key="season:tmdb:97546:4", filesystem_id="fixture",
        budget_bytes=100_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert reserved.reservation_id
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="sonarr",
        reservation_id=reserved.reservation_id, scope_key="S04E01",
        selected_files=("Ted.Lasso.S04E01/Ted.Lasso.S04E01.mkv",
                        "Ted.Lasso.S04E01/Ted.Lasso.S04E01.pt-BR.srt"),
        budget_bytes=1000, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _permit: {"accepted": True},
    )
    torrents = tmp_path / "torrents"
    folder = torrents / "Ted.Lasso.S04E01"
    folder.mkdir(parents=True)
    (folder / "Ted.Lasso.S04E01.mkv").write_bytes(b"video")
    subtitle = folder / "Ted.Lasso.S04E01.pt-BR.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nLegenda\n", encoding="utf-8")
    monkeypatch.setattr(
        "homeserver_control.worker.series_finalization.validate_media",
        lambda path, **_kwargs: ValidationResult(
            Path(path), 5, MediaProbe(1920, 1080, ("eng",), (), {}),
        ),
    )
    posts = []
    imported = False
    library = tmp_path / "media"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 44, "seasonNumber": 4, "episodeNumber": 1,
                "hasFile": imported, "episodeFileId": 9 if imported else 0,
            }])
        if request.url.path == "/api/v3/episodefile/9":
            return httpx.Response(200, json={
                "path": "/data/media/tv/Ted Lasso/Season 04/Ted.Lasso.S04E01.mkv",
            })
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "progress": 1, "amount_left": 0,
                "content_path": "/data/torrents/Ted.Lasso.S04E01",
            }])
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[
                {"name": permit.selected_files[0], "size": 5},
                {"name": permit.selected_files[1], "size": subtitle.stat().st_size},
            ])
        if request.url.path == "/api/v3/command" and request.method == "POST":
            posts.append(request)
            assert request.read().decode().find("DownloadedEpisodesScan") >= 0
            return httpx.Response(201, json={"id": 7, "status": "queued"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = SeriesFinalizer(
            repository=repo, permits=permits, torrent_root=torrents,
            media_root=library, gateway_url="http://download-gateway:8081",
            arr_token="secret", sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", client=client,
        )
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "import_requested"
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "import_pending"
        destination = library / "Ted Lasso" / "Season 04"
        destination.mkdir(parents=True)
        (destination / "Ted.Lasso.S04E01.mkv").write_bytes(b"video")
        imported = True
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "complete"
    assert len(posts) == 1
    assert repo.episode_import_state(permit.permit_id) == "complete"
    assert (destination / "Ted.Lasso.S04E01.pt-BR.srt").stat().st_ino == subtitle.stat().st_ino


@pytest.mark.asyncio
async def test_external_subdl_episode_subtitle_is_required_and_installed(tmp_path, monkeypatch):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:3:4", source_id="3:4",
        media_key="season:tmdb:97546:4", filesystem_id="fixture",
        budget_bytes=100_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert reserved.reservation_id
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="sonarr",
        reservation_id=reserved.reservation_id, scope_key="S04E01",
        selected_files=("Ted.Lasso.S04E01/Ted.Lasso.S04E01.mkv",),
        budget_bytes=1000, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256,
        effect=lambda _permit: {"accepted": True},
    )
    torrent_folder = tmp_path / "torrents" / "Ted.Lasso.S04E01"
    torrent_folder.mkdir(parents=True)
    (torrent_folder / "Ted.Lasso.S04E01.mkv").write_bytes(b"video")
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda brasileira\n"
    monkeypatch.setattr(
        "homeserver_control.worker.series_finalization.validate_media",
        lambda path, **_kwargs: ValidationResult(
            Path(path), 5, MediaProbe(1920, 1080, ("eng",), (), {}),
        ),
    )
    imported = False
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 44, "seasonNumber": 4, "episodeNumber": 1,
                "hasFile": imported, "episodeFileId": 9 if imported else 0,
            }])
        if request.url.path == "/api/v3/episodefile/9":
            return httpx.Response(200, json={
                "path": "/data/media/tv/Ted Lasso/Season 04/Ted.Lasso.S04E01.mkv",
            })
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "progress": 1, "amount_left": 0,
                "content_path": "/data/torrents/Ted.Lasso.S04E01",
            }])
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[{
                "name": permit.selected_files[0], "size": 5,
            }])
        if request.url.path == "/api/v3/command" and request.method == "POST":
            posts.append(request)
            return httpx.Response(201, json={"id": 18})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    library = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = SeriesFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            media_root=library, gateway_url="http://download-gateway:8081",
            arr_token="secret", sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", client=client,
        )
        with pytest.raises(ValidationError, match="Brazilian"):
            await finalizer.finalize("season:tmdb:97546:4", reserved.reservation_id)
        assert not posts
        SubtitleArtifactStore(database).put(
            reserved.reservation_id, "S04E01", permit.infohash, srt
        )
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "import_requested"
        destination = library / "Ted Lasso" / "Season 04"
        destination.mkdir(parents=True)
        (destination / "Ted.Lasso.S04E01.mkv").write_bytes(b"video")
        imported = True
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "complete"
    assert len(posts) == 1
    assert (destination / "Ted.Lasso.S04E01.pt-BR.srt").read_bytes() == srt
