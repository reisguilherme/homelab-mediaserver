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
    copy_bytes = 5 + (film / "movie.pt-BR.srt").stat().st_size
    free_bytes = copy_bytes + 2
    library = tmp_path / "media"

    async def capacity_provider():
        return CapacityEvidence(
            free_bytes=free_bytes,
            remaining_by_hash={permit.infohash: 0},
            other_pending_bytes=3,
        )

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
        finalizer.capacity_provider = capacity_provider
        assert (
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
            == "import_guard"
        )
        hardlinks_enabled = True
        library.mkdir()
        assert await finalizer.finalize(
            "movie:tmdb:1101383", reserved.reservation_id
        ) == "waiting_space"
        finalizer.import_uid = os.getuid()
        finalizer.import_gid = os.getgid()
        assert await finalizer.finalize(
            "movie:tmdb:1101383", reserved.reservation_id
        ) == "import_requested"
        assert len(posts) == 1
        assert (
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
            == "import_pending"
        )
        (library / "Film").mkdir(parents=True)
        (library / "Film/movie.mkv").write_bytes(b"video")
        imported = True
        assert (
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
            == "import_uncertain"
        )
        (library / "Film/movie.mkv").unlink()
        os.link(film / "movie.mkv", library / "Film/movie.mkv")
        (library / "Film/movie.pt-BR.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nLegenda errada\n", encoding="utf-8"
        )
        with pytest.raises(ValidationError, match="differs"):
            await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id)
        (library / "Film/movie.pt-BR.srt").unlink()
        assert await finalizer.finalize("movie:tmdb:1101383", reserved.reservation_id) == "complete"
    assert len(posts) == 1
    assert repo.import_state(reserved.reservation_id) == "complete"
    assert (
        (library / "Film/movie.pt-BR.srt").stat().st_ino
        == (film / "movie.pt-BR.srt").stat().st_ino
    )


def test_copy_import_must_match_source_bytes(tmp_path):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:1101383",
        filesystem_id="fixture", budget_bytes=100,
        free_bytes=1000, total_bytes=2000,
    )
    assert reserved.reservation_id
    permits = PermitRegistry(tmp_path / "control.sqlite")
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id=reserved.reservation_id,
        selected_files=("Film/movie.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    source = tmp_path / "torrents" / "Film" / "movie.mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    imported = tmp_path / "media" / "Film" / "movie.mkv"
    imported.parent.mkdir(parents=True)
    imported.write_bytes(b"other")
    finalizer = MovieFinalizer(
        repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
        media_root=tmp_path / "media", gateway_url="http://gateway",
        arr_token="secret", radarr_url="http://radarr", radarr_api_key="secret",
    )
    assert not finalizer._import_matches_source(imported, permit, copy_allowed=True)
    imported.write_bytes(b"video")
    assert finalizer._import_matches_source(imported, permit, copy_allowed=True)


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
        async def capacity_provider():
            return CapacityEvidence(free_bytes=100, remaining_by_hash={})

        finalizer.capacity_provider = capacity_provider
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
@pytest.mark.parametrize("subtitle_case", [
    "brazilian", "english", "embedded_english", "original_ptbr",
])
@pytest.mark.parametrize("generic_video", [False, True])
async def test_movie_subtitle_priority_and_original_audio(
    tmp_path, monkeypatch, subtitle_case, generic_video
):
    release_title = "Film.1080p.BluRay"
    video_stem = "movie" if generic_video else release_title
    folder_name = release_title if generic_video else "Film"
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
        selected_files=(f"{folder_name}/{video_stem}.mkv",),
        budget_bytes=81_000_000_000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256,
        effect=lambda _permit: {"accepted": True},
    )
    torrent_folder = tmp_path / "torrents" / folder_name
    torrent_folder.mkdir(parents=True)
    (torrent_folder / f"{video_stem}.mkv").write_bytes(b"video")
    brazilian_srt = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda brasileira\n"
    english_srt = b"1\n00:00:01,000 --> 00:00:02,000\nEnglish subtitle\n"
    raw = {"streams": [{
        "codec_type": "audio", "tags": {"language": "pt-BR", "title": "Original"},
    }]} if subtitle_case == "original_ptbr" else {}
    if subtitle_case == "embedded_english":
        raw = {"streams": [{
            "codec_type": "subtitle", "codec_name": "subrip",
            "tags": {"language": "eng", "title": "English"},
            "disposition": {"forced": 0},
        }]}
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

        async def fetch(self, *, tmdb_id, release_title, language="BR_PT"):
            assert tmdb_id == 152532
            self.calls.append((language, release_title))
            if release_title != "Film.1080p.BluRay":
                return None
            if language == "BR_PT" and subtitle_case == "brazilian":
                return brazilian_srt
            if language == "EN":
                return english_srt
            return None

    source = AvailableSubtitles()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "progress": 1, "amount_left": 0,
                "content_path": f"/data/torrents/{folder_name}",
                "name": "www.UIndex.org    -    Film.1080p.BluRay",
            }])
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[{
                "name": f"{folder_name}/{video_stem}.mkv", "size": 5,
            }])
        if request.url.path == "/api/v3/command" and request.method == "POST":
            posts.append(request)
            return httpx.Response(201, json={"id": 12})
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{
                "id": 7, "tmdbId": 152532, "hasFile": imported,
                "path": "/data/media/movies/Film",
                "movieFile": {"relativePath": f"{video_stem}.mkv"} if imported else None,
            }])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    library = tmp_path / "media"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = MovieFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            media_root=library, gateway_url="http://download-gateway:8081",
            arr_token="secret", radarr_url="http://radarr:7878",
            radarr_api_key="secret", client=client, subtitle_source=source,
        )
        async def capacity_provider():
            return CapacityEvidence(
                free_bytes=100, remaining_by_hash={permit.infohash: 0}
            )

        finalizer.capacity_provider = capacity_provider
        assert await finalizer.finalize(
            "movie:tmdb:152532", reserved.reservation_id
        ) == "import_requested"
        titles = list(dict.fromkeys([
            video_stem, folder_name, "www.UIndex.org    -    Film.1080p.BluRay",
        ]))
        before_match = titles[:titles.index(release_title) + 1]
        expected_calls = []
        if subtitle_case != "original_ptbr":
            expected_calls.extend(
                ("BR_PT", title) for title in (
                    before_match if subtitle_case == "brazilian" else titles
                )
            )
        if subtitle_case == "english":
            expected_calls.extend(("EN", title) for title in before_match)
        assert source.calls == expected_calls
        expected_subtitle = {
            "brazilian": brazilian_srt,
            "english": english_srt,
            "embedded_english": None,
            "original_ptbr": None,
        }[subtitle_case]
        assert SubtitleArtifactStore(database).get(
            reserved.reservation_id, None, permit.infohash,
            language="EN" if subtitle_case == "english" else "BR_PT",
        ) == expected_subtitle
        destination = library / "Film"
        destination.mkdir(parents=True)
        (destination / f"{video_stem}.mkv").write_bytes(b"video")
        imported = True
        assert await finalizer.finalize(
            "movie:tmdb:152532", reserved.reservation_id
        ) == "complete"
    assert len(posts) == 1
    if expected_subtitle is None:
        assert not list(destination.glob("*.srt"))
    else:
        suffix = "pt-BR" if subtitle_case == "brazilian" else "en"
        assert (destination / f"{video_stem}.{suffix}.srt").read_bytes() == expected_subtitle


def test_hardlink_import_claims_wait_only_for_dispatching_imports(tmp_path):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    first = repo.reserve(
        request_id="seerr:1", source_id="1", media_key="movie:tmdb:1",
        filesystem_id="fixture", budget_bytes=0,
        free_bytes=100, total_bytes=200,
    )
    second = repo.reserve(
        request_id="seerr:2", source_id="2", media_key="movie:tmdb:2",
        filesystem_id="fixture", budget_bytes=0,
        free_bytes=100, total_bytes=200,
    )
    assert first.reservation_id and second.reservation_id
    assert repo.claim_movie_import(first.reservation_id)
    assert not repo.claim_episode_import("another-episode-permit")
    assert not repo.claim_movie_import(second.reservation_id)
    repo.record_movie_import(first.reservation_id, "command-1")
    assert repo.claim_episode_import("another-episode-permit")
    assert not repo.claim_movie_import(second.reservation_id)
    repo.record_episode_import("another-episode-permit", "command-2")
    assert repo.claim_movie_import(second.reservation_id)


def test_copy_import_claims_remain_exclusive(tmp_path):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    first = repo.reserve(
        request_id="seerr:copy-1", source_id="copy-1", media_key="movie:tmdb:1",
        filesystem_id="fixture", budget_bytes=0,
        free_bytes=100, total_bytes=200,
    )
    second = repo.reserve(
        request_id="seerr:copy-2", source_id="copy-2", media_key="movie:tmdb:2",
        filesystem_id="fixture", budget_bytes=0,
        free_bytes=100, total_bytes=200,
    )
    assert first.reservation_id and second.reservation_id
    assert repo.claim_movie_import(first.reservation_id, copy_allowed=True)
    repo.record_movie_import(first.reservation_id, "command-copy")
    assert not repo.claim_episode_import("other-hardlink")
    assert not repo.claim_movie_import(second.reservation_id, copy_allowed=True)
    repo.complete_movie_import(first.reservation_id)
    assert repo.claim_episode_import("other-hardlink")
    repo.record_episode_import("other-hardlink", "command-hardlink")
    assert not repo.claim_movie_import(second.reservation_id, copy_allowed=True)


def test_copy_waits_for_every_accepted_hardlink(tmp_path):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    first = repo.reserve(
        request_id="seerr:hardlink", source_id="hardlink", media_key="movie:tmdb:1",
        filesystem_id="fixture", budget_bytes=0,
        free_bytes=100, total_bytes=200,
    )
    copy = repo.reserve(
        request_id="seerr:copy", source_id="copy", media_key="movie:tmdb:2",
        filesystem_id="fixture", budget_bytes=0,
        free_bytes=100, total_bytes=200,
    )
    assert first.reservation_id and copy.reservation_id
    assert repo.claim_movie_import(first.reservation_id)
    repo.record_movie_import(first.reservation_id, "command-hardlink")
    assert repo.claim_episode_import("episode-hardlink")
    repo.record_episode_import("episode-hardlink", "command-episode")
    assert not repo.claim_movie_import(copy.reservation_id, copy_allowed=True)
    repo.complete_movie_import(first.reservation_id)
    assert not repo.claim_movie_import(copy.reservation_id, copy_allowed=True)
    repo.complete_episode_import("episode-hardlink")
    assert repo.claim_movie_import(copy.reservation_id, copy_allowed=True)
