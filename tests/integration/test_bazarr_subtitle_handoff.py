import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.subtitle_artifacts import (
    MOVIE_FINALIZER_SOURCE,
    SubtitleArtifactStore,
)
from homeserver_control.worker.finalization import MovieFinalizer
from homeserver_control.worker.subtitle_language import SubtitlePolicy
from homeserver_control.worker.validation import ValidationError, ValidationResult

SRT = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda instalada pelo Bazarr\n"


@pytest.fixture
def imported_movie(tmp_path, monkeypatch):
    repository = ReservationRepository(tmp_path / "control.sqlite")
    repository.initialize()
    reservation = repository.reserve(
        request_id="seerr:1", source_id="1", media_key="movie:tmdb:10",
        filesystem_id="fixture", budget_bytes=1000, free_bytes=10000, total_bytes=20000,
    )
    permits = PermitRegistry(repository.path)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
        category="radarr", reservation_id=reservation.reservation_id,
        selected_files=("Film/movie.mkv",), budget_bytes=1000,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    source = tmp_path / "torrents/Film/movie.mkv"
    video = tmp_path / "media/Film/Imported Film (2024).mkv"
    source.parent.mkdir(parents=True)
    video.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    os.link(source, video)
    assert repository.claim_movie_import(reservation.reservation_id)
    repository.record_movie_import(reservation.reservation_id, "42")
    probe = MediaProbe(1920, 1080, ("eng",), (), {}, duration_seconds=7200)
    monkeypatch.setattr(
        "homeserver_control.worker.finalization.validate_media",
        lambda path, **_: ValidationResult(Path(path), 5, probe),
    )

    def handler(request):
        if request.url.path == "/api/v3/movie":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 10, "hasFile": True,
                "path": "/data/media/movies/Film",
                "movieFile": {"relativePath": video.name}, "genres": ["Drama"],
            }])
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "content_path": "/data/torrents/Film",
                "progress": 1, "amount_left": 0,
            }])
        if request.url.path == "/Library/Media/Updated":
            assert repository.import_state(reservation.reservation_id) == "subtitles_pending"
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return repository, permits, permit, video, probe, handler


def finalizer_for(imported_movie, client, *, policy=None, subtitle_source=None):
    repository, permits, _, video, _, _ = imported_movie
    return MovieFinalizer(
        repository=repository, permits=permits,
        torrent_root=video.parents[2] / "torrents", media_root=video.parents[1],
        gateway_url="http://gateway", arr_token="fixture", radarr_url="http://radarr",
        radarr_api_key="fixture", client=client, subtitle_policy=policy,
        subtitle_source=subtitle_source,
        jellyfin_url="http://jellyfin", jellyfin_api_key="fixture",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["pt-BR", "ptBR", "pt-BR.hi", "ptBR.hi"])
async def test_late_bazarr_subtitle_completes_without_subdl_and_keeps_file(imported_movie, label):
    repository, _, permit, video, _, handler = imported_movie
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = finalizer_for(imported_movie, client)
        assert await finalizer.finalize(
            "movie:tmdb:10", permit.reservation_id,
        ) == "waiting_subtitles"
        assert repository.import_state(permit.reservation_id) == "subtitles_pending"
        sidecar = video.with_name(f"{video.stem}.{label}.srt")
        sidecar.write_bytes(SRT)
        before = sidecar.stat()
        digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
        assert await finalizer.finalize("movie:tmdb:10", permit.reservation_id) == "complete"
        assert repository.import_state(permit.reservation_id) == "complete"
    assert list(video.parent.glob("*.srt")) == [sidecar]
    assert sidecar.stat().st_ino == before.st_ino
    assert sidecar.stat().st_mtime_ns == before.st_mtime_ns
    assert hashlib.sha256(sidecar.read_bytes()).hexdigest() == digest
    assert SubtitleArtifactStore(repository.path).get(
        permit.reservation_id, None, permit.infohash, source=MOVIE_FINALIZER_SOURCE,
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sidecar_case", [
    "empty", "invalid", "nul", "unrelated", "other_directory", "forced", "partial",
    "portugal", "generic_portuguese", "symlink_inside", "symlink_outside",
])
async def test_invalid_or_unrelated_bazarr_subtitle_cannot_complete(imported_movie, sidecar_case):
    repository, _, permit, video, _, handler = imported_movie
    sidecar = video.with_name(f"{video.stem}.pt-BR.srt")
    if sidecar_case == "unrelated":
        sidecar = video.with_name(f"{video.stem}.extended.pt-BR.srt")
    elif sidecar_case == "other_directory":
        sidecar = video.parents[1] / "Another" / sidecar.name
        sidecar.parent.mkdir()
    elif sidecar_case in {"forced", "partial"}:
        sidecar = video.with_name(f"{video.stem}.pt-BR.{sidecar_case}.srt")
    elif sidecar_case == "portugal":
        sidecar = video.with_name(f"{video.stem}.pt-PT.srt")
    elif sidecar_case == "generic_portuguese":
        sidecar = video.with_name(f"{video.stem}.pt.srt")
    if sidecar_case.startswith("symlink"):
        linked = (video.parent / "other.srt" if sidecar_case == "symlink_inside"
                  else video.parents[2] / "outside.srt")
        linked.write_bytes(SRT)
        sidecar.symlink_to(linked)
    else:
        sidecar.write_bytes({"empty": b"", "invalid": b"invalid", "nul": SRT + b"\x00"}.get(
            sidecar_case, SRT,
        ))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = finalizer_for(imported_movie, client)
        assert await finalizer.finalize(
            "movie:tmdb:10", permit.reservation_id,
        ) == "waiting_subtitles"
    assert repository.import_state(permit.reservation_id) == "subtitles_pending"
    assert sidecar.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [
    SubtitlePolicy(providers=("subdl",)),
    SubtitlePolicy(languages=("en-US",)),
])
async def test_bazarr_handoff_obeys_enabled_provider_and_language(imported_movie, policy):
    repository, _, permit, video, _, handler = imported_movie
    sidecar = video.with_name(f"{video.stem}.pt-BR.srt")
    sidecar.write_bytes(SRT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = finalizer_for(imported_movie, client, policy=policy)
        assert await finalizer.finalize(
            "movie:tmdb:10", permit.reservation_id,
        ) == "waiting_subtitles"
    assert repository.import_state(permit.reservation_id) != "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("preferred_available", [False, True])
async def test_bazarr_english_fallback_retains_brazilian_preference(
    imported_movie, preferred_available,
):
    _, _, permit, video, _, handler = imported_movie
    english = video.with_name(f"{video.stem}.en.hi.srt")
    english.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nEnglish subtitle\n")

    class Subtitles:
        async def fetch_movie(self, *, language, **kwargs):
            return SRT if preferred_available and language == "BR_PT" else None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = finalizer_for(imported_movie, client, subtitle_source=Subtitles())
        assert await finalizer.finalize("movie:tmdb:10", permit.reservation_id) == "complete"
    assert english.read_bytes() == b"1\n00:00:01,000 --> 00:00:02,000\nEnglish subtitle\n"
    brazilian = video.with_name(f"{video.stem}.pt-BR.srt")
    assert brazilian.exists() == preferred_available
    if preferred_available:
        assert brazilian.read_bytes() == SRT


@pytest.mark.asyncio
async def test_embedded_english_fallback_still_completes(imported_movie):
    _, _, permit, video, probe, handler = imported_movie
    probe.raw["streams"] = [{
        "codec_type": "subtitle", "codec_name": "subrip",
        "tags": {"language": "eng", "title": "English"}, "disposition": {"forced": 0},
    }]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = finalizer_for(imported_movie, client)
        assert await finalizer.finalize("movie:tmdb:10", permit.reservation_id) == "complete"
    assert not list(video.parent.glob("*.srt"))


@pytest.mark.asyncio
async def test_bazarr_handoff_rejects_video_outside_library(imported_movie):
    _, _, permit, video, _, handler = imported_movie
    outside = video.parents[2] / "outside.mkv"
    outside.write_bytes(b"video")
    outside.with_suffix(".pt-BR.srt").write_bytes(SRT)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = finalizer_for(imported_movie, client)
        with pytest.raises(ValidationError, match="escapes library"):
            finalizer._ensure_subtitle_for_video(outside, permit, None)


@pytest.mark.asyncio
async def test_bazarr_handoff_requires_admitted_physical_pool(imported_movie, monkeypatch):
    _, _, _, video, _, handler = imported_movie
    sidecar = video.with_name(f"{video.stem}.pt-BR.srt")
    sidecar.write_bytes(SRT)

    class Registry:
        pools = {name: SimpleNamespace(host_root=Path("/srv") / name)
                 for name in ("ssd", "hdd")}

        def resolve(self, name, path, *, writable=False):
            return video if str(path).endswith(".mkv") else sidecar

    # Replace only mergerfs' OS evidence; use the real physical_path checks.
    monkeypatch.setattr(os, "getxattr", lambda path, key: os.fsencode(
        Path("/srv") / ("hdd" if Path(path).suffix == ".mkv" else "ssd")
        / "media/movies" / Path(path).relative_to(video.parents[1]),
    ))
    physical_permit = SimpleNamespace(pool_id="hdd", selected_files=())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = finalizer_for(imported_movie, client)
        finalizer.storage_registry = Registry()
        monkeypatch.setattr(finalizer.permits, "placement_valid", lambda *args, **kwargs: True)
        with pytest.raises(ValidationError, match="admitted physical pool"):
            finalizer._ensure_subtitle_for_video(video, physical_permit, None)


@pytest.mark.asyncio
async def test_bazarr_sidecar_is_preserved_when_subdl_artifact_already_exists(imported_movie):
    repository, _, permit, video, _, handler = imported_movie
    sidecar = video.with_name(f"{video.stem}.pt-BR.hi.srt")
    sidecar.write_bytes(SRT)
    stored = b"1\n00:00:01,000 --> 00:00:02,000\nOutra legenda do SubDL\n"
    SubtitleArtifactStore(repository.path).put(
        permit.reservation_id, None, permit.infohash, stored, source=MOVIE_FINALIZER_SOURCE,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = finalizer_for(imported_movie, client)
        assert await finalizer.finalize("movie:tmdb:10", permit.reservation_id) == "complete"
    assert sidecar.read_bytes() == SRT
    assert list(video.parent.glob("*.srt")) == [sidecar]
