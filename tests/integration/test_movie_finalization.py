from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.worker.finalization import MovieFinalizer
from homeserver_control.worker.validation import ValidationError, ValidationResult


@pytest.mark.asyncio
async def test_completed_movie_is_validated_before_one_radarr_import(tmp_path, monkeypatch):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:1101383",
        filesystem_id="fixture", budget_bytes=81_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert reserved.reservation_id
    permits = PermitRegistry(tmp_path / "control.sqlite")
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
        category="radarr", reservation_id=reserved.reservation_id,
        selected_files=("Film/movie.mkv", "Film/movie.pt-BR.srt"),
        budget_bytes=81_000_000_000, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _permit: {"accepted": True},
    )
    torrents = tmp_path / "torrents"
    film = torrents / "Film"
    film.mkdir(parents=True)
    (film / "movie.mkv").write_bytes(b"video")
    (film / "movie.pt-BR.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nUma legenda em portugues\n", encoding="utf-8"
    )
    probe = MediaProbe(1920, 1080, ("eng",), (), {})
    monkeypatch.setattr(
        "homeserver_control.worker.finalization.validate_media",
        lambda path, **_kwargs: ValidationResult(Path(path), 5, probe),
    )
    posts = []
    imported = False
    hardlinks_enabled = False
    library = tmp_path / "media"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": hardlinks_enabled})
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "progress": 1, "amount_left": 0,
                "save_path": "/data/torrents", "content_path": "/data/torrents/Film",
            }])
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[
                {"name": "Film/movie.mkv", "size": 5},
                {"name": "Film/movie.pt-BR.srt", "size": (film / "movie.pt-BR.srt").stat().st_size},
            ])
        if request.url.path == "/api/v3/command" and request.method == "POST":
            posts.append(request)
            return httpx.Response(201, json={"id": 4, "status": "queued"})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{
                "id": 2, "tmdbId": 1101383, "hasFile": imported,
                "path": "/data/media/movies/Film",
                "movieFile": {"relativePath": "movie.mkv"} if imported else None,
            }])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = MovieFinalizer(
            repository=repo, permits=permits, torrent_root=torrents,
            media_root=library,
            gateway_url="http://download-gateway:8081", arr_token="secret",
            radarr_url="http://radarr:7878", radarr_api_key="secret", client=client,
        )
        assert (
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
            == "import_guard"
        )
        hardlinks_enabled = True
        assert (
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
            == "import_requested"
        )
        assert (
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
            == "import_pending"
        )
        (library / "Film").mkdir(parents=True)
        (library / "Film/movie.mkv").write_bytes(b"video")
        imported = True
        assert await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id) == "complete"
    assert len(posts) == 1
    assert repo.import_state(reserved.reservation_id) == "complete"
    assert (
        (library / "Film/movie.pt-BR.srt").stat().st_ino
        == (film / "movie.pt-BR.srt").stat().st_ino
    )


@pytest.mark.asyncio
async def test_finalizer_rejects_legacy_portugal_subtitle_permit(tmp_path, monkeypatch):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:3", source_id="3", media_key="movie:tmdb:1101383",
        filesystem_id="fixture", budget_bytes=81_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert reserved.reservation_id
    permits = PermitRegistry(tmp_path / "control.sqlite")
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reserved.reservation_id,
        selected_files=("Film/movie.mkv", "Film/movie.pt-PT.srt"),
        budget_bytes=81_000_000_000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _permit: {"accepted": True},
    )
    folder = tmp_path / "torrents" / "Film"
    folder.mkdir(parents=True)
    (folder / "movie.mkv").write_bytes(b"video")
    subtitle = folder / "movie.pt-PT.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nOlá\n", encoding="utf-8")
    monkeypatch.setattr(
        "homeserver_control.worker.finalization.validate_media",
        lambda path, **_kwargs: ValidationResult(
            Path(path), 5, MediaProbe(1920, 1080, ("eng",), (), {}),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "progress": 1, "amount_left": 0,
                "content_path": "/data/torrents/Film",
            }])
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[
                {"name": "Film/movie.mkv", "size": 5},
                {"name": "Film/movie.pt-PT.srt", "size": subtitle.stat().st_size},
            ])
        raise AssertionError("Radarr import must not occur")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = MovieFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            gateway_url="http://download-gateway:8081", arr_token="secret",
            radarr_url="http://radarr:7878", radarr_api_key="secret", client=client,
        )
        with pytest.raises(ValidationError, match="Brazilian"):
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
    assert repo.import_state(reserved.reservation_id) is None


@pytest.mark.asyncio
async def test_incomplete_torrent_cannot_be_imported(tmp_path):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:1101383",
        filesystem_id="fixture", budget_bytes=81_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert reserved.reservation_id
    permits = PermitRegistry(tmp_path / "control.sqlite")
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
        category="radarr", reservation_id=reserved.reservation_id,
        budget_bytes=81_000_000_000, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _permit: {"accepted": True},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/torrents/info"
        return httpx.Response(200, json=[{"hash": permit.infohash, "progress": 0.5}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = MovieFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            gateway_url="http://download-gateway:8081", arr_token="secret",
            radarr_url="http://radarr:7878", radarr_api_key="secret", client=client,
        )
        assert (
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
            == "downloading"
        )
    assert repo.import_state(reserved.reservation_id) is None


@pytest.mark.asyncio
async def test_external_subdl_movie_subtitle_is_required_and_installed(tmp_path, monkeypatch):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:7", source_id="7", media_key="movie:tmdb:152532",
        filesystem_id="fixture", budget_bytes=81_000_000_000,
        free_bytes=500_000_000_000, total_bytes=600_000_000_000,
    )
    assert reserved.reservation_id
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reserved.reservation_id,
        selected_files=("Film/movie.mkv",), budget_bytes=81_000_000_000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256,
        effect=lambda _permit: {"accepted": True},
    )
    torrent_folder = tmp_path / "torrents" / "Film"
    torrent_folder.mkdir(parents=True)
    (torrent_folder / "movie.mkv").write_bytes(b"video")
    srt = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda brasileira\n"
    monkeypatch.setattr(
        "homeserver_control.worker.finalization.validate_media",
        lambda path, **_kwargs: ValidationResult(
            Path(path), 5, MediaProbe(1920, 1080, ("eng",), (), {}),
        ),
    )
    imported = False
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "progress": 1, "amount_left": 0,
                "content_path": "/data/torrents/Film",
            }])
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[{"name": "Film/movie.mkv", "size": 5}])
        if request.url.path == "/api/v3/command" and request.method == "POST":
            posts.append(request)
            return httpx.Response(201, json={"id": 12})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{
                "id": 7, "tmdbId": 152532, "hasFile": imported,
                "path": "/data/media/movies/Film",
                "movieFile": {"relativePath": "movie.mkv"} if imported else None,
            }])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    library = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = MovieFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            media_root=library, gateway_url="http://download-gateway:8081",
            arr_token="secret", radarr_url="http://radarr:7878",
            radarr_api_key="secret", client=client,
        )
        with pytest.raises(ValidationError, match="Brazilian"):
            await finalizer.finalize("movie:tmdb:152532", reserved.reservation_id)
        assert not posts
        SubtitleArtifactStore(database).put(reserved.reservation_id, None, permit.infohash, srt)
        assert await finalizer.finalize(
            "movie:tmdb:152532", reserved.reservation_id
        ) == "import_requested"
        destination = library / "Film"
        destination.mkdir(parents=True)
        (destination / "movie.mkv").write_bytes(b"video")
        imported = True
        assert await finalizer.finalize(
            "movie:tmdb:152532", reserved.reservation_id
        ) == "complete"
    assert len(posts) == 1
    assert (destination / "movie.pt-BR.srt").read_bytes() == srt
