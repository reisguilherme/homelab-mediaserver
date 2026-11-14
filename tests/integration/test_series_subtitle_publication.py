"""Title-scoped publication preserves verified bytes and durable subtitle recovery."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.deletion_jobs import DeletionJobStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.series_acquisition import SeriesAcquirer
from homeserver_control.worker.series_finalization import SeriesFinalizer
from homeserver_control.worker.subtitle_language import SubtitlePolicy
from homeserver_control.worker.validation import ValidationError, ValidationResult

SRT = b"1\n00:00:01,000 --> 00:00:02,000\nLegenda\n"


class PublicationFixture:
    def __init__(self, tmp_path, monkeypatch, *, pack=True, copy=False):
        self.repo = ReservationRepository(tmp_path / "control.sqlite")
        self.repo.initialize()
        self.jobs = DeletionJobStore(self.repo.path)
        self.permits = PermitRegistry(self.repo.path)
        self.torrents, self.library = tmp_path / "torrents", tmp_path / "library"
        self.library.mkdir()
        self.reservations, self.children, self.parents, self.sources = {}, {}, {}, {}
        self.imported, self.posts, self.fetches, self.notified = set(), [], [], []
        self.available = set()
        self.during_fetch = None
        self.copy = copy
        self.completed = True
        for season, count in ((1, 2), (2, 1)):
            reservation = self.repo.reserve(
                request_id=f"seerr:127:{season}", source_id=f"127:{season}",
                media_key=f"season:tmdb:74606:{season}", filesystem_id="fixture",
                budget_bytes=count * 1000, free_bytes=10000, total_bytes=20000,
            ).reservation_id
            self.reservations[season] = reservation
            files = {}
            for episode in range(1, count + 1):
                scope = f"S{season:02d}E{episode:02d}"
                relative = f"pack{season}/Fixture.Show.{scope}.1080p.WEB-DL.mkv"
                source = self.torrents / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"video")
                self.sources[scope] = source
                files[scope] = (relative,)
            if pack:
                parent = self._permit(season, f"S{season:02d}PACK", tuple(
                    path for names in files.values() for path in names
                ), season)
                self.permits.bind_season_pack(parent.token, episode_files=files)
                self._confirm(parent)
                self.parents[season] = parent
                for scope in files:
                    self.children[scope] = self.permits.get_for_reservation(
                        reservation, scope_key=scope,
                    )
            else:
                for index, (scope, names) in enumerate(files.items(), start=1):
                    child = self._permit(season, scope, names, season * 10 + index)
                    self._confirm(child)
                    self.children[scope] = child
        probe = MediaProbe(1920, 1080, ("por",), (), {
            "streams": [{"codec_type": "audio", "tags": {"language": "por"}}],
        }, duration_seconds=673)
        def validate(path, **_kw):
            return ValidationResult(Path(path), 5, probe)

        monkeypatch.setattr(
            "homeserver_control.worker.series_finalization.validate_media", validate,
        )
        monkeypatch.setattr("homeserver_control.worker.finalization.validate_media", validate)

    def _permit(self, season, scope, files, number):
        parent = self.permits.issue(
            infohash=f"{number:040x}", metadata_sha256=f"{number:064x}",
            destination="/data/torrents", category="sonarr",
            reservation_id=self.reservations[season], scope_key=scope,
            selected_files=files, budget_bytes=1000,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        self.permits.set_quality(parent.token, (1, 1080, 0, 0, 0, 0))
        return parent

    def _confirm(self, permit):
        self.permits.authorize(
            token=permit.token, infohash=permit.infohash, destination=permit.destination,
            metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
        )

    def target(self, scope):
        return self.library / f"Fixture Show/Season {scope[1:3]}" / self.sources[scope].name

    def publish(self, scope, *, mismatch=False):
        target = self.target(scope)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.copy or mismatch:
            target.write_bytes(b"video")
        else:
            os.link(self.sources[scope], target)
        self.imported.add(scope)

    def handler(self, request):
        path = request.url.path
        if path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 124, "tmdbId": 74606, "title": "Fixture Show", "year": 2014,
                "genres": ["Animation"], "path": "/data/media/tv/Fixture Show",
            }])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": index, "seasonNumber": int(scope[1:3]), "episodeNumber": int(scope[4:]),
                "hasFile": scope in self.imported, "episodeFileId": index,
            } for index, scope in enumerate(self.children, start=1)])
        if path.startswith("/api/v3/episodefile/"):
            scope = list(self.children)[int(path.rsplit("/", 1)[-1]) - 1]
            return httpx.Response(200, json={
                "path": "/data/media/tv/" + str(self.target(scope).relative_to(self.library)),
            })
        if path == "/api/v2/torrents/info":
            infohash = request.url.params["hashes"]
            permit = next(v for v in self.children.values() if v.infohash == infohash)
            season = int(permit.scope_key[1:3])
            return httpx.Response(200, json=[{
                "hash": infohash, "progress": 1, "amount_left": 0,
                "content_path": f"/data/torrents/pack{season}",
                "name": f"Fixture.Show.S{season:02d}.1080p.WEB-DL",
            }])
        if path == "/api/v2/torrents/files":
            infohash = request.url.params["hash"]
            return httpx.Response(200, json=[{
                "name": name, "size": 5, "progress": 1 if self.completed else 0,
            } for permit in self.children.values() if permit.infohash == infohash
                for name in permit.selected_files])
        if path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if path == "/api/v3/command":
            self.posts.append(json.loads(request.content))
            return httpx.Response(201, json={"id": len(self.posts)})
        if path == "/Library/Media/Updated":
            self.notified.append(json.loads(request.content))
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected fixture request {request.method} {path}")

    async def fetch(self, **kwargs):
        self.fetches.append(kwargs)
        if self.during_fetch:
            self.during_fetch()
        return SRT if (kwargs["season"], kwargs["episode"]) in self.available else None

    def worker(self, client):
        worker = SeriesFinalizer(
            repository=self.repo, permits=self.permits, torrent_root=self.torrents,
            media_root=self.library, gateway_url="http://gateway", arr_token="fixture",
            sonarr_url="http://sonarr", sonarr_api_key="fixture", client=client,
            subtitle_source=self, subtitle_policy=SubtitlePolicy(providers=("subdl",)),
            import_uid=os.getuid(), import_gid=os.getgid(), is_tombstoned=self.jobs.is_tombstoned,
            jellyfin_url="http://jellyfin", jellyfin_api_key="fixture",
        )
        if self.copy:
            worker._can_hardlink_video = lambda *_a, **_kw: False

            async def capacity():
                return CapacityEvidence(free_bytes=10000, remaining_by_hash={})

            worker.capacity_provider = capacity
        return worker


def test_series_publication_opt_in_is_strict_scoped_and_durable(tmp_path):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    assert not repo.series_subtitle_publication_allowed(74606)
    repo.set_series_subtitle_publication(74606, enabled=True)
    restarted = ReservationRepository(repo.path)
    assert restarted.series_subtitle_publication_allowed(74606)
    assert not restarted.series_subtitle_publication_allowed(999)
    restarted.set_series_subtitle_publication(74606, enabled=False)
    assert not repo.series_subtitle_publication_allowed(74606)
    for invalid in (True, False, 0, -1, "74606", 74606.0):
        with pytest.raises(ValueError):
            repo.set_series_subtitle_publication(invalid, enabled=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("pack,copy", [(True, False), (False, False), (True, True)])
async def test_opted_in_verified_publication_advances_episode_and_season_with_pending_subtitles(
    tmp_path, monkeypatch, pack, copy,
):
    f = PublicationFixture(tmp_path, monkeypatch, pack=pack, copy=copy)
    f.repo.set_series_subtitle_publication(74606, enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        for scope in f.children:
            season = int(scope[1:3])
            key, reservation = f"season:tmdb:74606:{season}", f.reservations[season]
            assert await worker.finalize(key, reservation) == "import_requested"
            assert f.fetches == []
            f.publish(scope)
            assert await worker.finalize(key, reservation) == "subtitles_pending"
            assert f.repo.episode_import_state(f.children[scope].permit_id) == (
                "subtitles_pending_copy" if copy else "subtitles_pending"
            )
            assert worker._episode_imported(
                {"seasonNumber": season, "episodeNumber": int(scope[4:]), "hasFile": True},
                f.reservations, 74606,
            )
            acquirer = SeriesAcquirer(
                repository=f.repo, permits=f.permits, sonarr_url="http://sonarr",
                sonarr_api_key="fixture", prowlarr_url="http://prowlarr", client=client,
            )
            assert acquirer._episode_imported(
                {"seasonNumber": season, "episodeNumber": int(scope[4:]), "hasFile": True},
                f.reservations, 74606,
            )
        assert len(f.posts) == 3 and len(f.notified) == 3
        assert len(f.repo.pending_series_imports()) == 2
        # Verified pending copies release the conservative global import slot too.
        assert f.repo.claim_episode_import("another-import", copy_allowed=True)


@pytest.mark.asyncio
async def test_default_series_still_waits_before_import_and_other_opt_in_does_not_apply(
    tmp_path, monkeypatch,
):
    f = PublicationFixture(tmp_path, monkeypatch)
    f.repo.set_series_subtitle_publication(999, enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        assert await f.worker(client).finalize(
            "season:tmdb:74606:1", f.reservations[1],
        ) == "waiting_subtitles"
    assert f.posts == [] and len(f.fetches) == 4


@pytest.mark.asyncio
async def test_native_inode_mismatch_never_becomes_published_pending(tmp_path, monkeypatch):
    f = PublicationFixture(tmp_path, monkeypatch)
    f.repo.set_series_subtitle_publication(74606, enabled=True)
    child = f.children["S01E01"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        assert await worker.finalize("season:tmdb:74606:1", f.reservations[1]) == "import_requested"
        f.publish("S01E01", mismatch=True)
        assert await worker.finalize("season:tmdb:74606:1", f.reservations[1]) == "import_uncertain"
    assert f.repo.episode_import_state(child.permit_id) == "accepted"
    assert not f.notified


@pytest.mark.asyncio
async def test_default_accepted_import_with_vanished_subtitle_stays_unverified(
    tmp_path, monkeypatch,
):
    f = PublicationFixture(tmp_path, monkeypatch)
    child = f.children["S01E01"]
    assert f.repo.claim_episode_import(child.permit_id)
    f.repo.record_episode_import(child.permit_id, "fixture-command")
    f.publish("S01E01")
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        with pytest.raises(ValidationError, match="subtitle vanished"):
            await worker.finalize("season:tmdb:74606:1", f.reservations[1])
        assert not worker._episode_imported(
            {"seasonNumber": 1, "episodeNumber": 1, "hasFile": True}, f.reservations, 74606,
        )
    assert f.repo.episode_import_state(child.permit_id) == "accepted"
    assert f.notified == []


@pytest.mark.asyncio
async def test_incomplete_current_payload_never_becomes_published_pending(tmp_path, monkeypatch):
    f = PublicationFixture(tmp_path, monkeypatch)
    f.repo.set_series_subtitle_publication(74606, enabled=True)
    child = f.children["S01E01"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        assert await worker.finalize("season:tmdb:74606:1", f.reservations[1]) == "import_requested"
        f.publish("S01E01")
        f.completed = False
        assert await worker.finalize("season:tmdb:74606:1", f.reservations[1]) == "downloading"
    assert f.repo.episode_import_state(child.permit_id) == "accepted"
    assert f.notified == []


@pytest.mark.asyncio
async def test_opted_in_import_with_ready_subtitle_completes_without_pending(tmp_path, monkeypatch):
    f = PublicationFixture(tmp_path, monkeypatch)
    f.repo.set_series_subtitle_publication(74606, enabled=True)
    child = f.children["S01E01"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        worker.subtitle_store.put(child.reservation_id, child.scope_key, child.infohash, SRT)
        assert await worker.finalize("season:tmdb:74606:1", f.reservations[1]) == "import_requested"
        f.publish("S01E01")
        assert await worker.finalize("season:tmdb:74606:1", f.reservations[1]) == "complete"
    assert f.repo.episode_import_state(child.permit_id) == "complete"
    assert f.target("S01E01").with_suffix(".pt-BR.srt").read_bytes() == SRT
    assert f.fetches == []


@pytest.mark.asyncio
async def test_pending_subtitle_retry_rotates_and_recovers_after_restart_and_revocation(
    tmp_path, monkeypatch,
):
    f = PublicationFixture(tmp_path, monkeypatch)
    f.repo.set_series_subtitle_publication(74606, enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        for scope in ("S01E01", "S01E02"):
            assert await worker.finalize(
                "season:tmdb:74606:1", f.reservations[1],
            ) == "import_requested"
            f.publish(scope)
            assert await worker.finalize(
                "season:tmdb:74606:1", f.reservations[1],
            ) == "subtitles_pending"
        f.repo.set_series_subtitle_publication(74606, enabled=False)
        worker = f.worker(client)
        assert await worker.retry_pending_subtitles(
            "season:tmdb:74606:1", f.reservations[1],
        ) == "waiting_subtitles"
        f.available.add((1, 2))
        assert await worker.retry_pending_subtitles(
            "season:tmdb:74606:1", f.reservations[1],
        ) == "complete"
    assert len(f.posts) == 2
    assert f.repo.episode_import_state(f.children["S01E01"].permit_id) == "subtitles_pending"
    assert f.repo.episode_import_state(f.children["S01E02"].permit_id) == "complete"
    assert f.target("S01E02").with_suffix(".pt-BR.srt").read_bytes() == SRT
    assert f.fetches[-1]["episode"] == 2
    assert f.fetches[-1]["episode_duration_seconds"] == 673


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["request", "season", "episode"])
async def test_cancellation_during_subtitle_fetch_prevents_later_artifact_and_sidecar_writes(
    tmp_path, monkeypatch, cancel,
):
    f = PublicationFixture(tmp_path, monkeypatch)
    f.repo.set_series_subtitle_publication(74606, enabled=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(f.handler)) as client:
        worker = f.worker(client)
        await worker.finalize("season:tmdb:74606:1", f.reservations[1])
        f.publish("S01E01")
        await worker.finalize("season:tmdb:74606:1", f.reservations[1])
        f.available.add((1, 1))
        if cancel == "request":
            f.during_fetch = lambda: f.repo.cancel_unstarted(f.reservations[1])
        else:
            key = ("season:tmdb:74606:1" if cancel == "season"
                   else "episode:tmdb:74606:S01E01")
            f.during_fetch = lambda: f.jobs.tombstone(key, "fixture")
        assert await worker.retry_pending_subtitles(
            "season:tmdb:74606:1", f.reservations[1],
        ) == "reservation_inactive"
    child = f.children["S01E01"]
    assert worker.subtitle_store.get(child.reservation_id, child.scope_key, child.infohash) is None
    assert not list(f.library.rglob("*.srt"))
    assert f.repo.episode_import_state(child.permit_id) == "subtitles_pending"
