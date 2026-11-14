import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.finalization import MovieFinalizer
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.validation import ValidationResult


@pytest.mark.asyncio
@pytest.mark.parametrize("series", [False, True], ids=["movie", "series"])
@pytest.mark.parametrize(("profile_case", "animation", "needs_subtitles"), [
    ("Dublado", False, True),
    ("Dublado", True, True),
    ("Standard", False, False),
    ("Max Quality", False, False),
    ("Standard", True, True),
    ("legacy", False, False),
    ("deleted", False, True),
    ("unavailable", False, True),
    ("malformed", False, True),
    ("ambiguous", False, True),
    ("invalid_id", False, True),
])
async def test_selected_profile_controls_original_ptbr_subtitle_waiver(
    tmp_path, monkeypatch, series, profile_case, animation, needs_subtitles,
):
    """Dublado must still search after import/validation even with original pt-BR."""
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    media_key = "season:tmdb:10:1" if series else "movie:tmdb:10"
    reservation = repo.reserve(
        request_id="seerr:10", source_id="10", media_key=media_key,
        filesystem_id="fixture", budget_bytes=100, free_bytes=1000, total_bytes=2000,
    )
    permits = PermitRegistry(repo.path)
    permit = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64, destination="/data/torrents",
        category="sonarr" if series else "radarr",
        reservation_id=reservation.reservation_id,
        scope_key="S01E01" if series else None,
        selected_files=("Film/Film.S01E01.mkv",), budget_bytes=100,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    permits.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    source = tmp_path / "torrents/Film/Film.S01E01.mkv"
    target = tmp_path / "media/Film/Film.S01E01.mkv"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    if not series:
        os.link(source, target)
        assert repo.claim_movie_import(reservation.reservation_id)
        repo.record_movie_import(reservation.reservation_id, "42")

    original_ptbr = MediaProbe(1920, 1080, ("pt-BR",), (), {"streams": [{
        "codec_type": "audio", "disposition": {"original": 1},
        "tags": {"language": "pt-BR", "title": "Original"},
    }]})
    for module in ("finalization", "series_finalization"):
        monkeypatch.setattr(
            f"homeserver_control.worker.{module}.validate_media",
            lambda path, **_: ValidationResult(Path(path), 5, original_ptbr),
        )

    attempts = []

    class MissingSubtitles:
        async def fetch(self, *, language, match_mode, **_):
            attempts.append((language, match_mode))
            return None

        fetch_movie = fetch

    profiles = [
        {"id": 89, "name": "Standard"},
        {"id": 31, "name": "Dublado"},
        {"id": 7, "name": "Max Quality"},
    ]
    profile_id = {"Standard": 89, "Dublado": 31, "Max Quality": 7}.get(profile_case, 31)
    media = {"id": 2, "tmdbId": 10, "genres": ["Animation" if animation else "Drama"]}
    if profile_case != "legacy":
        media["qualityProfileId"] = (
            404 if profile_case == "deleted" else
            True if profile_case == "invalid_id" else profile_id
        )
        # An embedded display name must not override native profile identity.
        media["qualityProfile"] = {"name": "Standard"}
    profile_reads = []

    def handler(request):
        assert request.method == "GET"
        path = request.url.path
        if path == "/api/v3/qualityprofile":
            profile_reads.append(request.url.host)
            if profile_case == "unavailable":
                return httpx.Response(503)
            if profile_case == "malformed":
                return httpx.Response(200, json={"profiles": profiles})
            if profile_case == "ambiguous":
                return httpx.Response(200, json=profiles + [{"id": 31, "name": "Standard"}])
            return httpx.Response(200, json=profiles)
        if path == "/api/v3/movie":
            return httpx.Response(200, json=[media | {
                "hasFile": True, "path": "/data/media/movies/Film",
                "movieFile": {"relativePath": "Film.S01E01.mkv"},
            }])
        if path == "/api/v3/series":
            return httpx.Response(200, json=[media])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": 11, "seasonNumber": 1, "episodeNumber": 1, "hasFile": False,
            }])
        if path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": permit.infohash, "progress": 1, "amount_left": 0,
                "content_path": "/data/torrents/Film", "name": "Film.S01E01.1080p.BluRay",
            }])
        if path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[{"name": permit.selected_files[0], "size": 5}])
        if path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": False})
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = (SeriesFinalizer if series else MovieFinalizer)(
            repository=repo, permits=permits, torrent_root=tmp_path / "torrents",
            media_root=tmp_path / "media", gateway_url="http://gateway", arr_token="fixture",
            client=client, subtitle_source=MissingSubtitles(),
            **({"sonarr_url": "http://sonarr", "sonarr_api_key": "fixture"} if series else
               {"radarr_url": "http://radarr", "radarr_api_key": "fixture"}),
        )
        outcome = await finalizer.finalize(media_key, reservation.reservation_id)

    assert outcome == (
        "waiting_subtitles" if needs_subtitles else "import_guard" if series else "complete"
    )
    assert attempts == ([
        ("BR_PT", "exact"), ("BR_PT", "same_duration"),
        ("EN", "exact"), ("EN", "same_duration"),
    ] if needs_subtitles else [])
    if profile_case == "legacy" or animation or profile_case == "invalid_id":
        assert profile_reads == []
    else:
        assert profile_reads == ["sonarr" if series else "radarr"]
    assert finalizer.subtitle_policy.skip_original_audio_languages == ("pt-BR",)
