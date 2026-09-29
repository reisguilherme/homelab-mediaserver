import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.validation import ValidationResult


def _bencode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    return b"d" + b"".join(
        _bencode(key) + _bencode(value[key]) for key in sorted(value)
    ) + b"e"


def _torrent(entries):
    total = sum(size for _, size in entries)
    piece_length = 16 * 1024 * 1024
    return _bencode({b"info": {
        b"name": b"Series.S02.1080p.WEB-GROUP",
        b"files": [{b"length": size, b"path": [name.encode()]} for name, size in entries],
        b"piece length": piece_length,
        b"pieces": b"a" * (((total + piece_length - 1) // piece_length) * 20),
    }})


def _manifest(entries, **overrides):
    from homeserver_control.worker.season_packs import inspect_season_pack

    kwargs = {
        "season": 2,
        "episode_runtimes": {1: 40, 2: 40},
        "release": {
            "title": "Series S02 1080p WEB-GROUP", "size": 2_000_000_000,
            "quality": {"quality": {"source": "web", "resolution": 1080}},
        },
        "release_policy": ReleasePolicy(resolutions=(1080,)),
        "allow_external_subtitle": True,
    }
    return inspect_season_pack(_torrent(entries), **(kwargs | overrides))


def test_complete_pack_maps_each_episode_but_claims_every_torrent_byte():
    entries = [
        ("Series.S02E01.1080p.WEB-GROUP.mkv", 900_000_000),
        ("Series.S02E01.pt-BR.srt", 100),
        ("Series.S02E02.1080p.WEB-GROUP.mkv", 950_000_000),
        ("Series.S02E02.pt-PT.srt", 110),
        ("sample.mkv", 20),
        ("readme.txt", 30),
    ]
    manifest = _manifest(entries)
    assert manifest is not None
    prefix = "Series.S02.1080p.WEB-GROUP/"
    assert manifest.episode_files == {
        "S02E01": (prefix + entries[0][0], prefix + entries[1][0]),
        "S02E02": (prefix + entries[2][0],),
    }
    assert manifest.budget_bytes == 1_850_000_260
    assert manifest.selected_files == tuple(prefix + name for name, _ in entries)
    assert manifest.infohash == inspect_torrent(_torrent(entries)).infohash


@pytest.mark.parametrize("entries", [
    [("Series.S02E01.1080p.mkv", 900_000_000)],
    [("Series.S02E01.1080p.mkv", 900_000_000), ("Series.S02E03.1080p.mkv", 900_000_000)],
    [("Series.S02E01E02.1080p.mkv", 1_900_000_000)],
    [("Series.S02E01.1080p.mkv", 900_000_000), ("Series.S02E02.1080p.mkv", 900_000_000),
     ("Series.S02E02.Directors.Cut.1080p.mkv", 1_000_000_000)],
    [("Series.S02E01.1080p.mkv", 400_000_000), ("Series.S02E02.1080p.mkv", 1_800_000_000)],
    [("Series.S02E01.1080p.mkv", 900_000_000), ("Series.S02E02.720p.mkv", 900_000_000)],
])
def test_pack_rejects_missing_extra_combined_duplicate_small_or_wrong_resolution_video(entries):
    assert _manifest(entries) is None


def test_pack_rejects_4k_release_even_with_series_policy_4k_enabled():
    assert _manifest(
        [("Series.S02E01.mkv", 2_000_000_000), ("Series.S02E02.mkv", 2_000_000_000)],
        release={"quality": {"quality": {"source": "web", "resolution": 2160}}},
        release_policy=ReleasePolicy(),
    ) is None


def test_pack_checks_each_episode_runtime_and_subtitle_availability():
    entries = [("Series.S02E01.mkv", 900_000_000), ("Series.S02E02.mkv", 900_000_000)]
    assert _manifest(entries, episode_runtimes={1: 40, 2: 55}) is None
    assert _manifest(entries, allow_external_subtitle=False) is None


def _registry(tmp_path, *, selected_files=None):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    result = repo.reserve(
        request_id="seerr:1:2", source_id="1:2", media_key="season:tmdb:97546:2",
        filesystem_id="fixture", budget_bytes=0, free_bytes=20_000, total_bytes=30_000,
    )
    permits = PermitRegistry(repo.path)
    parent = permits.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        reservation_id=result.reservation_id, scope_key="S02PACK",
        category="sonarr", destination="/data/torrents",
        selected_files=selected_files or ("pack/Series.S02E01.mkv", "pack/Series.S02E02.mkv"),
        budget_bytes=10_000, capacity=CapacityEvidence(free_bytes=20_000, remaining_by_hash={}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return repo, permits, parent


def test_pack_bindings_keep_one_physical_admission_and_claim(tmp_path):
    repo, permits, parent = _registry(tmp_path)
    bindings = {
        "S02E01": ("pack/Series.S02E01.mkv",),
        "S02E02": ("pack/Series.S02E02.mkv",),
    }
    permits.bind_season_pack(parent.token, episode_files=bindings)
    permits.bind_season_pack(parent.token, episode_files=bindings)
    permits = PermitRegistry(repo.path)
    first = permits.get_for_reservation(parent.reservation_id, scope_key="S02E01")
    second = permits.get_for_reservation(parent.reservation_id, scope_key="S02E02")
    assert first is not None and second is not None
    assert first.permit_id != second.permit_id != parent.permit_id
    assert first.season_pack_parent_id == second.season_pack_parent_id == parent.permit_id
    assert first.token == second.token == parent.token
    assert first.selected_files == bindings["S02E01"]
    assert second.selected_files == bindings["S02E02"]
    assert permits.pending_bytes(CapacityEvidence(
        free_bytes=20_000, remaining_by_hash={},
    )) == 10_000
    assert permits.find_for_metadata(
        infohash=parent.infohash, metadata_sha256=parent.metadata_sha256,
        destination=parent.destination, category=parent.category,
    ).permit_id == parent.permit_id
    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gateway_permits").fetchone()[0] == 1
    permits.authorize(
        token=parent.token, infohash=parent.infohash, destination=parent.destination,
        metadata_sha256=parent.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    first = permits.get_for_reservation(parent.reservation_id, scope_key="S02E01")
    second = permits.get_for_reservation(parent.reservation_id, scope_key="S02E02")
    assert first.state == second.state == "confirmed"
    assert repo.claim_episode_import(first.permit_id)
    repo.record_episode_import(first.permit_id, "1")
    repo.complete_episode_import(first.permit_id)
    assert repo.episode_import_state(second.permit_id) is None
    assert permits.get_for_reservation(parent.reservation_id, scope_key="S02E01").permit_id == (
        first.permit_id
    )
    with pytest.raises(PermissionError):
        permits.deletion_source(
            token=first.token, media_key="season:tmdb:97546:2", scope_key="S02E01",
        )


@pytest.mark.parametrize("bindings", [
    {"S03E01": ("pack/Series.S02E01.mkv",)},
    {"S02E01": ("other/Series.S02E01.mkv",)},
    {"S02E01": ("pack/Series.S02E02.mkv",)},
    {"S02E01": ("pack/Series.S02E01.mkv", "pack/Series.S02E02.mkv")},
])
def test_pack_rejects_binding_to_other_season_file_or_episode(tmp_path, bindings):
    _, permits, parent = _registry(tmp_path)
    with pytest.raises(ValueError):
        permits.bind_season_pack(parent.token, episode_files=bindings)


def test_pack_does_not_capture_an_episode_already_admitted(tmp_path):
    _, permits, parent = _registry(tmp_path)
    permits.issue(
        infohash="c" * 40, metadata_sha256="d" * 64,
        reservation_id=parent.reservation_id, scope_key="S02E01",
        category="sonarr", destination="/data/torrents",
        selected_files=("pack/Series.S02E01.mkv",), budget_bytes=100,
        capacity=CapacityEvidence(free_bytes=20_000, remaining_by_hash={}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(PermissionError, match="episode_already_permitted"):
        permits.bind_season_pack(parent.token, episode_files={
            "S02E01": ("pack/Series.S02E01.mkv",),
        })


def test_individual_episode_cannot_race_an_existing_pack_binding(tmp_path):
    _, permits, parent = _registry(tmp_path)
    permits.bind_season_pack(parent.token, episode_files={
        "S02E01": ("pack/Series.S02E01.mkv",),
    })
    with pytest.raises(PermissionError, match="episode_already_permitted"):
        permits.issue(
            infohash="c" * 40, metadata_sha256="d" * 64,
            reservation_id=parent.reservation_id, scope_key="S02E01",
            category="sonarr", destination="/data/torrents",
            selected_files=("episode/Series.S02E01.mkv",), budget_bytes=100,
            capacity=CapacityEvidence(free_bytes=20_000, remaining_by_hash={}),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )


def test_pack_deletion_association_resolves_only_a_real_child_without_deleting_parent(tmp_path):
    _, permits, parent = _registry(tmp_path)
    permits.bind_season_pack(parent.token, episode_files={
        "S02E01": ("pack/Series.S02E01.mkv",),
    })
    with pytest.raises(PermissionError):
        permits.season_pack_deletion_source(
            token=parent.token, media_key="season:tmdb:97546:2", scope_key="S02E01",
        )
    permits.authorize(
        token=parent.token, infohash=parent.infohash, destination=parent.destination,
        metadata_sha256=parent.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    view = permits.season_pack_deletion_source(
        token=parent.token, media_key="season:tmdb:97546:2", scope_key="S02E01",
    )
    assert view.season_pack_parent_id == parent.permit_id
    assert view.selected_files == ("pack/Series.S02E01.mkv",)
    assert permits.get(parent.token).state == "confirmed"
    for key, scope in [("season:tmdb:97546:3", "S02E01"), ("season:tmdb:97546:2", "S02E02")]:
        with pytest.raises(PermissionError):
            permits.season_pack_deletion_source(token=parent.token, media_key=key, scope_key=scope)
    permits.issue(
        infohash=parent.infohash, metadata_sha256=parent.metadata_sha256,
        destination=parent.destination, category="radarr", selected_files=parent.selected_files,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with pytest.raises(ValueError, match="shared"):
        permits.season_pack_deletion_source(
            token=parent.token, media_key="season:tmdb:97546:2", scope_key="S02E01",
        )


@pytest.mark.parametrize("confirmed", [False, True])
def test_cancel_unused_pack_releases_budget_but_never_cancels_dispatched_source(
    tmp_path, confirmed,
):
    repo, permits, parent = _registry(tmp_path)
    permits.bind_season_pack(parent.token, episode_files={
        "S02E01": ("pack/Series.S02E01.mkv",),
    })
    if confirmed:
        permits.authorize(
            token=parent.token, infohash=parent.infohash, destination=parent.destination,
            metadata_sha256=parent.metadata_sha256, effect=lambda _: {"accepted": True},
        )
    assert permits.cancel_authorized(parent.token) is (not confirmed)
    capacity = CapacityEvidence(free_bytes=20_000, remaining_by_hash={})
    assert permits.pending_bytes(capacity) == (10_000 if confirmed else 0)
    assert repo.active_reservation(parent.reservation_id)["budget_bytes"] == (
        10_000 if confirmed else 0
    )
    assert (permits.get_for_reservation(parent.reservation_id, scope_key="S02E01") is not None) == (
        confirmed
    )


def test_pack_is_releasable_only_after_all_owned_episodes_are_explicitly_deleted(tmp_path):
    repo, permits, parent = _registry(tmp_path)
    permits.bind_season_pack(parent.token, episode_files={
        "S02E01": ("pack/Series.S02E01.mkv",), "S02E02": ("pack/Series.S02E02.mkv",),
    })
    permits.authorize(
        token=parent.token, infohash=parent.infohash, destination=parent.destination,
        metadata_sha256=parent.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    assert not permits.season_pack_fully_deleted(
        token=parent.token, media_key="season:tmdb:97546:2",
    )
    jobs = DeletionJobStore(repo.path)
    jobs.tombstone("episode:tmdb:97546:S02E01", "jellyfin-first")
    assert not permits.season_pack_fully_deleted(
        token=parent.token, media_key="season:tmdb:97546:2",
    )
    jobs.tombstone("episode:tmdb:97546:S02E02", "jellyfin-second")
    assert permits.season_pack_fully_deleted(token=parent.token, media_key="season:tmdb:97546:2")
    assert not permits.season_pack_fully_deleted(
        token=parent.token, media_key="season:tmdb:97546:3",
    )


def test_confirmed_pack_lists_only_episode_views_not_completed_children(tmp_path):
    repo, permits, parent = _registry(tmp_path)
    permits.bind_season_pack(parent.token, episode_files={
        "S02E01": ("pack/Series.S02E01.mkv",), "S02E02": ("pack/Series.S02E02.mkv",),
    })
    permits.authorize(
        token=parent.token, infohash=parent.infohash, destination=parent.destination,
        metadata_sha256=parent.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    assert [
        (view.scope_key, view.infohash) for _, view in permits.list_active_confirmed_episodes()
    ] == [
        ("S02E01", parent.infohash), ("S02E02", parent.infohash),
    ]
    first = permits.get_for_reservation(parent.reservation_id, scope_key="S02E01")
    assert repo.claim_episode_import(first.permit_id)
    repo.record_episode_import(first.permit_id, "1")
    repo.complete_episode_import(first.permit_id)
    assert [view.scope_key for _, view in permits.list_active_confirmed_episodes()] == ["S02E02"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pack_progress", [1, 0.5])
@pytest.mark.parametrize("subtitle_first", [False, True])
async def test_pack_imports_exact_episode_file_then_waits_for_native_confirmation(
    tmp_path, monkeypatch, pack_progress, subtitle_first,
):
    paths = tuple(
        f"pack/Series.S02E{number:02d}{suffix}"
        for number in (1, 2) for suffix in (".mkv", ".pt-BR.srt")
    )
    repo, permits, parent = _registry(tmp_path, selected_files=paths)
    permits.bind_season_pack(parent.token, episode_files={
        "S02E01": paths[:2][::-1] if subtitle_first else paths[:2],
        "S02E02": paths[2:][::-1] if subtitle_first else paths[2:],
    })
    permits.authorize(
        token=parent.token, infohash=parent.infohash, destination=parent.destination,
        metadata_sha256=parent.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    root = tmp_path / "torrents"
    (root / "pack").mkdir(parents=True)
    for path in paths:
        (root / path).write_bytes(
            b"video" if path.endswith(".mkv")
            else b"1\n00:00:01,000 --> 00:00:02,000\nLegenda brasileira\n"
        )
    library = tmp_path / "media"
    library.mkdir()
    imported = set()
    commands = []
    monkeypatch.setattr(
        "homeserver_control.worker.series_finalization.validate_media",
        lambda path, **kwargs: ValidationResult(
            Path(path), 5, MediaProbe(1920, 1080, ("eng",), (), {}),
        ),
    )

    def handler(request):
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": number, "seasonNumber": 2, "episodeNumber": number,
                "hasFile": number in imported, "episodeFileId": number if number in imported else 0,
            } for number in (1, 2)])
        if request.url.path.startswith("/api/v3/episodefile/"):
            number = int(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json={
                "path": f"/data/media/tv/Series.S02E{number:02d}.mkv",
            })
        if request.url.path == "/api/v2/torrents/info":
            return httpx.Response(200, json=[{
                "hash": parent.infohash, "name": "Series.S02.1080p.WEB-GROUP",
                "progress": pack_progress, "amount_left": 0 if pack_progress == 1 else 100,
                "content_path": "/data/torrents/pack",
            }])
        if request.url.path == "/api/v2/torrents/files":
            return httpx.Response(200, json=[{
                "name": path, "size": (root / path).stat().st_size, "progress": 1,
            } for path in paths])
        if request.url.path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if request.url.path == "/api/v3/command" and request.method == "POST":
            commands.append(json.loads(request.content))
            return httpx.Response(201, json={"id": len(commands), "status": "queued"})
        raise AssertionError(f"Unexpected {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = SeriesFinalizer(
            repository=repo, permits=permits, torrent_root=root, media_root=library,
            gateway_url="http://gateway:8081", arr_token="secret", sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", client=client, import_uid=os.getuid(), import_gid=os.getgid(),
        )
        assert await finalizer.finalize("season:tmdb:97546:2", parent.reservation_id) == (
            "import_requested"
        )
        assert commands == [{
            "name": "DownloadedEpisodesScan", "path": "/data/torrents/pack/Series.S02E01.mkv",
            "downloadClientId": parent.infohash.upper(), "importMode": "Copy",
        }]
        assert await finalizer.finalize("season:tmdb:97546:2", parent.reservation_id) == (
            "import_pending"
        )
        os.link(root / paths[0], library / "Series.S02E01.mkv")
        imported.add(1)
        assert await finalizer.finalize("season:tmdb:97546:2", parent.reservation_id) == "complete"
        assert await finalizer.finalize("season:tmdb:97546:2", parent.reservation_id) == (
            "import_requested"
        )
        assert commands[1]["path"] == "/data/torrents/pack/Series.S02E02.mkv"
    assert (root / paths[0]).stat().st_ino == (library / "Series.S02E01.mkv").stat().st_ino
    assert (root / paths[2]).is_file()


@pytest.mark.asyncio
async def test_pack_episode_seven_waits_for_missing_five_and_six(tmp_path):
    repo, permits, parent = _registry(tmp_path, selected_files=("pack/Series.S02E07.mkv",))
    permits.bind_season_pack(parent.token, episode_files={
        "S02E07": ("pack/Series.S02E07.mkv",),
    })
    permits.authorize(
        token=parent.token, infohash=parent.infohash, destination=parent.destination,
        metadata_sha256=parent.metadata_sha256, effect=lambda _: {"accepted": True},
    )

    def handler(request):
        if request.url.path == "/api/v3/series":
            return httpx.Response(200, json=[{"id": 1, "tmdbId": 97546}])
        if request.url.path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": number, "seasonNumber": 2, "episodeNumber": number, "hasFile": number < 5,
            } for number in range(1, 8)])
        raise AssertionError(f"Order guard must precede {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = SeriesFinalizer(
            repository=repo, permits=permits, torrent_root=tmp_path, gateway_url="http://gateway:8081",
            arr_token="secret", sonarr_url="http://sonarr:8989",
            sonarr_api_key="secret", client=client,
        )
        assert await finalizer.finalize("season:tmdb:97546:2", parent.reservation_id) == (
            "waiting_previous_episode"
        )
