import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import SubtitleArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence
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
    hardlinks_enabled = False
    library = tmp_path / "media"
    copy_bytes = 5 + subtitle.stat().st_size

    async def capacity_provider():
        return CapacityEvidence(
            free_bytes=copy_bytes + 2,
            remaining_by_hash={permit.infohash: 0}, other_pending_bytes=3,
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": hardlinks_enabled})
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
        finalizer.capacity_provider = capacity_provider
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "import_guard"
        hardlinks_enabled = True
        library.mkdir()
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "waiting_space"
        finalizer.import_uid = os.getuid()
        finalizer.import_gid = os.getgid()
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
        ) == "import_uncertain"
        (destination / "Ted.Lasso.S04E01.mkv").unlink()
        os.link(folder / "Ted.Lasso.S04E01.mkv", destination / "Ted.Lasso.S04E01.mkv")
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "complete"
    assert len(posts) == 1
    assert repo.episode_import_state(permit.permit_id) == "complete"
    assert (destination / "Ted.Lasso.S04E01.pt-BR.srt").stat().st_ino == subtitle.stat().st_ino


def test_legacy_episode_permit_cannot_import_another_episodes_subtitle(tmp_path):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:3:1", source_id="3:1",
        media_key="season:tmdb:95480:1", filesystem_id="fixture",
        budget_bytes=100, free_bytes=1000, total_bytes=2000,
    )
    assert reserved.reservation_id
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="sonarr",
        reservation_id=reserved.reservation_id, scope_key="S01E01",
        selected_files=("Slow.Horses.S01E01/Slow.Horses.S01E01.mkv",
                        "Slow.Horses.S01E01/Slow.Horses.S01E02.pt-BR.srt"),
        budget_bytes=100, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    source = tmp_path / "torrents" / "Slow.Horses.S01E01"
    source.mkdir(parents=True)
    (source / "Slow.Horses.S01E02.pt-BR.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nLegenda errada\n", encoding="utf-8"
    )
    video = tmp_path / "media" / "Slow Horses" / "Season 01" / "Slow.Horses.S01E01.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    finalizer = SeriesFinalizer(
        repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
        media_root=tmp_path / "media", gateway_url="http://gateway",
        arr_token="secret", sonarr_url="http://sonarr", sonarr_api_key="secret",
    )
    with pytest.raises(ValidationError, match="episode"):
        finalizer._ensure_subtitle(video, permit)
    assert not list(video.parent.glob("*.srt"))


@pytest.mark.asyncio
@pytest.mark.parametrize("subtitle_case", ["brazilian", "english", "original_ptbr"])
async def test_episode_subtitle_priority_and_original_audio(
    tmp_path, monkeypatch, subtitle_case
):
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
    brazilian_srt = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda brasileira\n"
    english_srt = b"1\n00:00:01,000 --> 00:00:02,000\nEnglish subtitle\n"
    raw = {"streams": [{
        "codec_type": "audio", "tags": {"language": "pt-BR", "title": "Original"},
    }]} if subtitle_case == "original_ptbr" else {}
    monkeypatch.setattr(
        "homeserver_control.worker.series_finalization.validate_media",
        lambda path, **_kwargs: ValidationResult(
            Path(path), 5, MediaProbe(1920, 1080, ("por",) if raw else ("eng",), (), raw),
        ),
    )
    monkeypatch.setattr(
        "homeserver_control.worker.finalization.validate_media",
        lambda path, **_kwargs: ValidationResult(
            Path(path), 5, MediaProbe(1920, 1080, ("por",) if raw else ("eng",), (), raw),
        ),
    )
    imported = False
    posts = []

    class AvailableSubtitles:
        def __init__(self):
            self.calls = []

        async def fetch(
            self, *, tmdb_id, release_title, season, episode, language="BR_PT"
        ):
            assert (tmdb_id, release_title, season, episode) == (
                97546, "Ted.Lasso.S04E01.1080p.WEB-DL", 4, 1
            )
            self.calls.append(language)
            if language == "BR_PT" and subtitle_case == "brazilian":
                return brazilian_srt
            if language == "EN":
                return english_srt
            return None

    source = AvailableSubtitles()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
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
                "name": "Ted.Lasso.S04E01.1080p.WEB-DL",
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
            sonarr_api_key="secret", client=client, subtitle_source=source,
        )
        async def capacity_provider():
            return CapacityEvidence(
                free_bytes=100, remaining_by_hash={permit.infohash: 0}
            )

        finalizer.capacity_provider = capacity_provider
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "import_requested"
        expected_calls = {
            "brazilian": ["BR_PT"],
            "english": ["BR_PT", "EN"],
            "original_ptbr": [],
        }[subtitle_case]
        assert source.calls == expected_calls
        expected_subtitle = {
            "brazilian": brazilian_srt,
            "english": english_srt,
            "original_ptbr": None,
        }[subtitle_case]
        assert SubtitleArtifactStore(database).get(
            reserved.reservation_id, "S04E01", permit.infohash,
            language="EN" if subtitle_case == "english" else "BR_PT",
        ) == expected_subtitle
        destination = library / "Ted Lasso" / "Season 04"
        destination.mkdir(parents=True)
        (destination / "Ted.Lasso.S04E01.mkv").write_bytes(b"video")
        imported = True
        assert await finalizer.finalize(
            "season:tmdb:97546:4", reserved.reservation_id
        ) == "complete"
    assert len(posts) == 1
    if expected_subtitle is None:
        assert not list(destination.glob("*.srt"))
    else:
        suffix = "pt-BR" if subtitle_case == "brazilian" else "en"
        assert (
            destination / f"Ted.Lasso.S04E01.{suffix}.srt"
        ).read_bytes() == expected_subtitle


def _reserved_season(repo, season, *, tmdb_id=99999):
    reserved = repo.reserve(
        request_id=f"seerr:chronology:{season}", source_id=f"chronology:{season}",
        media_key=f"season:tmdb:{tmdb_id}:{season}", filesystem_id="fixture",
        budget_bytes=100, free_bytes=1000, total_bytes=2000,
    )
    assert reserved.reservation_id
    return reserved.reservation_id


def _confirmed_episode(permits, reservation_id, season, number):
    permit = permits.issue(
        infohash=f"{season:02x}{number:02x}" * 10,
        metadata_sha256="b" * 64, destination="/data/torrents",
        category="sonarr", reservation_id=reservation_id,
        scope_key=f"S{season:02d}E{number:02d}",
        selected_files=(f"Show.S{season:02d}E{number:02d}.mkv",),
        budget_bytes=100, expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash,
        destination=permit.destination, metadata_sha256=permit.metadata_sha256,
        effect=lambda _: {"accepted": True},
    )
    return permit


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_state, expected", [
    ("missing", "waiting_previous_season"),
    ("accepted", "waiting_previous_season"),
    ("complete", "downloading"),
    ("preexisting", "downloading"),
    ("catalog_missing", "waiting_previous_season"),
])
async def test_later_season_waits_for_requested_prior_season_import(
    tmp_path, earlier_state, expected,
):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    first_reservation = _reserved_season(repo, 1)
    third_reservation = _reserved_season(repo, 3)
    permits = PermitRegistry(database)
    first_permit = (
        _confirmed_episode(permits, first_reservation, 1, 1)
        if earlier_state not in {"preexisting", "catalog_missing"} else None
    )
    _confirmed_episode(permits, third_reservation, 3, 1)
    if earlier_state in {"accepted", "complete"}:
        assert first_permit is not None
        assert repo.claim_episode_import(first_permit.permit_id)
        repo.record_episode_import(first_permit.permit_id, "sonarr-import-1")
        if earlier_state == "complete":
            repo.complete_episode_import(first_permit.permit_id)
    gateway_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 99999}])
        if request.url.path == "/api/v3/episode":
            episodes = [
                {"id": 31, "seasonNumber": 3, "episodeNumber": 1, "hasFile": False}
            ]
            if earlier_state != "catalog_missing":
                episodes.insert(0, {
                    "id": 11, "seasonNumber": 1, "episodeNumber": 1,
                    "hasFile": earlier_state != "missing",
                })
            return httpx.Response(200, json=episodes)
        if request.url.path == "/api/v2/torrents/info":
            gateway_calls.append(request)
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = SeriesFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            gateway_url="http://download-gateway:8081", arr_token="secret",
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret", client=client,
        )
        assert await finalizer.finalize(
            "season:tmdb:99999:3", third_reservation
        ) == expected
    assert len(gateway_calls) == (
        1 if earlier_state in {"complete", "preexisting"} else 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier_imported, expected", [
    (False, "waiting_previous_episode"),
    (True, "downloading"),
])
async def test_episode_waits_for_missing_earlier_episode_without_permit(
    tmp_path, earlier_imported, expected,
):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reservation_id = _reserved_season(repo, 1)
    permits = PermitRegistry(database)
    _confirmed_episode(permits, reservation_id, 1, 2)
    gateway_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 99999}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": 11, "seasonNumber": 1, "episodeNumber": 1,
                 "hasFile": earlier_imported},
                {"id": 12, "seasonNumber": 1, "episodeNumber": 2, "hasFile": False},
            ])
        if request.url.path == "/api/v2/torrents/info":
            gateway_calls.append(request)
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = SeriesFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            gateway_url="http://download-gateway:8081", arr_token="secret",
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret", client=client,
        )
        assert await finalizer.finalize("season:tmdb:99999:1", reservation_id) == expected
    assert len(gateway_calls) == (1 if earlier_imported else 0)


@pytest.mark.asyncio
async def test_unrequested_prior_seasons_do_not_block_current_season(tmp_path):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reservation_id = _reserved_season(repo, 4)
    permits = PermitRegistry(database)
    _confirmed_episode(permits, reservation_id, 4, 1)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 99999}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[
                {"id": season * 10 + 1, "seasonNumber": season,
                 "episodeNumber": 1, "hasFile": False}
                for season in (1, 2, 3, 4)
            ])
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = SeriesFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            gateway_url="http://download-gateway:8081", arr_token="secret",
            sonarr_url="http://sonarr:8989", sonarr_api_key="secret", client=client,
        )
        assert await finalizer.finalize("season:tmdb:99999:4", reservation_id) == (
            "downloading"
        )
