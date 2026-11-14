import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.validation import ValidationError


def source(tmp_path, *, token="EP01", pack=True):
    repository = ReservationRepository(tmp_path / "control.sqlite")
    repository.initialize()
    reservation = repository.reserve(
        request_id="fixture", source_id="fixture", media_key="season:tmdb:123:2",
        filesystem_id="fixture", budget_bytes=0, free_bytes=50000, total_bytes=100000,
    ).reservation_id
    registry = PermitRegistry(repository.path)
    files = (f"pack/Show {token}.mkv", f"pack/Show {token}.pt-BR.srt")
    permit = registry.issue(
        infohash="a" * 40, metadata_sha256="f" * 64,
        reservation_id=reservation, scope_key="S02PACK" if pack else "S02E01",
        category="sonarr", destination="/data/torrents", selected_files=files,
        budget_bytes=1000, expires_at=datetime.now(UTC) + timedelta(hours=1),
        capacity=CapacityEvidence(50000, {}),
    )
    catalog = [
        {"seasonNumber": 1, "episodeNumber": 1, "absoluteEpisodeNumber": 1},
        {"seasonNumber": 2, "episodeNumber": 1, "absoluteEpisodeNumber": 13},
    ]
    return repository, registry, permit, catalog


@pytest.mark.parametrize("token", ["EP01", "SE02.EP01", "2x01", "- 013"])
@pytest.mark.parametrize("pack", [False, True])
def test_receipt_survives_restart_and_validates_video_and_subtitle(tmp_path, token, pack):
    repository, registry, permit, catalog = source(tmp_path, token=token, pack=pack)
    if pack:
        registry.bind_season_pack(
            permit.token, episode_files={"S02E01": permit.selected_files},
            episode_catalog=catalog, allow_absolute=True,
        )
    else:
        registry.bind_episode_identity(permit.token, episode_catalog=catalog, allow_absolute=True)
    registry = PermitRegistry(repository.path)
    episode = registry.get_for_reservation(permit.reservation_id, scope_key="S02E01")
    assert all(registry.episode_identity_matches(episode, path) for path in episode.selected_files)
    assert not registry.episode_identity_matches(replace(episode, scope_key="S02E02"),
                                                episode.selected_files[0])
    finalizer = SeriesFinalizer(
        repository=repository, permits=registry, torrent_root=tmp_path,
        gateway_url="http://gateway:8081", arr_token="fixture",
        sonarr_url="http://sonarr:8989", sonarr_api_key="fixture",
    )
    finalizer._validate_selected_episode_subtitles(episode, season=2, number=1)
    with pytest.raises(ValidationError):
        finalizer._validate_selected_episode_subtitles(episode, season=2, number=2)


@pytest.mark.parametrize("token", ["EP01", "- 013"])
def test_contextual_names_are_rejected_without_a_receipt(tmp_path, token):
    _, registry, permit, _ = source(tmp_path, token=token)
    with pytest.raises(ValueError, match="another episode"):
        registry.bind_season_pack(permit.token, episode_files={"S02E01": permit.selected_files})


def test_receipt_cannot_be_rebound_and_duplicates_remain_ambiguous(tmp_path):
    _, registry, permit, catalog = source(tmp_path, token="- 013")
    arguments = {"episode_files": {"S02E01": permit.selected_files}, "allow_absolute": True}
    ambiguous = catalog + [{"seasonNumber": 3, "episodeNumber": 1, "absoluteEpisodeNumber": 13}]
    with pytest.raises(ValueError, match="another episode"):
        registry.bind_season_pack(permit.token, episode_catalog=ambiguous, **arguments)
    registry.bind_season_pack(permit.token, episode_catalog=catalog, **arguments)
    with pytest.raises(ValueError, match="cannot change"):
        registry.bind_season_pack(
            permit.token, episode_catalog=catalog + [{"seasonNumber": 3, "episodeNumber": 1}],
            **arguments,
        )


@pytest.mark.parametrize("column, value", [("infohash", "b" * 40), ("metadata_sha256", "e" * 64)])
def test_changed_source_receipt_is_rejected_without_a_legacy_fallback(tmp_path, column, value):
    repository, registry, permit, catalog = source(tmp_path, token="S02E01")
    registry.bind_season_pack(
        permit.token, episode_files={"S02E01": permit.selected_files}, episode_catalog=catalog,
    )
    with sqlite3.connect(repository.path) as connection:
        if column == "infohash":
            connection.execute("UPDATE season_pack_identity_receipts SET infohash=?", (value,))
        else:
            connection.execute(
                "UPDATE season_pack_identity_receipts SET metadata_sha256=?", (value,),
            )
    episode = registry.get_for_reservation(permit.reservation_id, scope_key="S02E01")
    with pytest.raises(ValueError, match="differs"):
        registry.episode_identity_matches(episode, episode.selected_files[0])


def test_zero_absolute_sentinel_does_not_block_explicit_normal_episode(tmp_path):
    _, registry, permit, catalog = source(tmp_path, token="S02E01", pack=False)
    for row in catalog:
        row["absoluteEpisodeNumber"] = 0
    registry.bind_episode_identity(permit.token, episode_catalog=catalog)
    assert registry.episode_identity_matches(permit, permit.selected_files[0])


@pytest.mark.parametrize("mode", ["cancel", "expire"])
def test_unused_receipt_is_removed_atomically_with_its_permit(tmp_path, mode):
    repository, registry, permit, catalog = source(tmp_path, pack=False)
    registry.bind_episode_identity(permit.token, episode_catalog=catalog)
    if mode == "cancel":
        assert registry.cancel_authorized(permit.token)
    else:
        with sqlite3.connect(repository.path) as connection:
            connection.execute(
                "UPDATE gateway_permits SET expires_at=? WHERE token=?",
                ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), permit.token),
            )
        assert registry.retire_expired_authorized(permit.reservation_id, scope_key="S02E01") == 1
    with sqlite3.connect(repository.path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM season_pack_identity_receipts",
        ).fetchone()[0]
        assert count == 0


@pytest.mark.asyncio
async def test_live_replacement_keeps_identity_receipt_through_probe_promotion(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from homeserver_control.worker.series_acquisition import SeriesAcquirer
    from homeserver_control.worker.source_health import SourceHealthStore

    repository, registry, permit, catalog = source(tmp_path, token="S02E01", pack=False)
    registry.authorize(
        token=permit.token, infohash=permit.infohash, destination=permit.destination,
        metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    registry.set_quality(permit.token, (1, 1080))
    old = registry.get(permit.token)

    async def capacity():
        return CapacityEvidence(50000, {old.infohash: 1000})

    def put(candidate, _):
        assert all(
            registry.episode_identity_matches(candidate, p) for p in candidate.selected_files
        )

    acquirer = SeriesAcquirer(
        repository=repository, permits=registry, sonarr_url="http://sonarr:8989",
        sonarr_api_key="fixture", prowlarr_url="http://prowlarr:9696",
        gateway_url="http://gateway:8081", arr_token="fixture",
        torrent_store=SimpleNamespace(put=put), health_store=SourceHealthStore(repository.path),
        live_source_probes=True, capacity_provider=capacity,
    )

    async def add(candidate, _):
        registry.authorize(
            token=candidate.token, infohash=candidate.infohash, destination=candidate.destination,
            metadata_sha256=candidate.metadata_sha256, effect=lambda _: {"accepted": True},
        )

    async def monitor(_):
        return "probing"

    monkeypatch.setattr(acquirer, "_add_verified_torrent", add)
    monkeypatch.setattr(acquirer, "_monitor_probe", monitor)
    assert await acquirer._dispatch_replacement(
        old=old, infohash="b" * 40, metadata_sha256="e" * 64,
        selected_files=("candidate/Show - 013.mkv", "candidate/Show - 013.pt-BR.srt"),
        exact_bytes=2000, torrent=b"fixture", quality_rank=(1, 1080),
        episode_catalog=catalog, allow_absolute=True,
    ) == "probing"
    probe = registry.get_probe(old.permit_id)
    promoted = registry.promote_probe(probe.token)
    reloaded = PermitRegistry(repository.path)
    assert all(reloaded.episode_identity_matches(promoted, p) for p in promoted.selected_files)
