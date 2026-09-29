import json
import sqlite3
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence
from homeserver_control.worker.release_quality import ReleasePolicy
from homeserver_control.worker.series_acquisition import SeriesAcquirer


def _encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    return b"d" + b"".join(
        _encode(key) + _encode(item) for key, item in sorted(value.items())
    ) + b"e"


class SeriesFixture:
    def __init__(self, tmp_path, *, episode_count=20, free_bytes=100_000_000_000):
        self.repo = ReservationRepository(tmp_path / "control.sqlite")
        self.repo.initialize()
        self.reservation = self.repo.reserve(
            request_id="fixture", source_id="fixture", media_key="season:tmdb:101:1",
            filesystem_id="fixture", budget_bytes=0, free_bytes=free_bytes,
            total_bytes=200_000_000_000,
        ).reservation_id
        self.permits = PermitRegistry(self.repo.path)
        self.torrents = {}
        self.releases = {}
        self.pack_offers = []
        self.episode_offers = {}
        self.remaining = {}
        self.posts, self.controls, self.searched, self.metadata = [], [], [], []
        self.episode_count, self.free_bytes = episode_count, free_bytes

    def release(
        self, name, episodes, *, seeds=10, group="GROUP", video_bytes=1_500_000_000,
        root_name=None, extra_files=(),
    ):
        files = [
            {b"path": [f"Fixture.S01E{number:02d}.1080p.WEB-{group}.{extension}".encode()],
             b"length": video_bytes if extension == "mkv" else 1000}
            for number in episodes for extension in ("mkv", "pt-BR.srt")
        ]
        files.extend({b"path": [part.encode() for part in path.split("/")], b"length": 1000}
                     for path in extra_files)
        total = sum(item[b"length"] for item in files)
        torrent = _encode({b"info": {
            b"name": (root_name or name).encode(), b"files": files, b"piece length": 16_777_216,
            b"pieces": b"a" * (20 * ((total + 16_777_215) // 16_777_216)),
        }})
        inspected = inspect_torrent(torrent)
        self.torrents[name] = torrent
        release = {
            "guid": name, "indexer": "UIndex (Prowlarr)", "indexerId": 1,
            "title": f"Fixture.S01{'E' + str(episodes[0]).zfill(2) if len(episodes) == 1 else ''}"
                     f".1080p.WEB-DL.AMZN-{group}",
            "releaseGroup": group, "seeders": seeds, "size": total, "rejected": False,
            "infoHash": inspected.infohash, "episodeIds": list(episodes),
            "downloadUrl": f"http://prowlarr:9696/1/download?id={name}",
            "quality": {"quality": {
                "source": "web", "resolution": 1080, "name": "WEBDL-1080p",
            }},
        }
        self.releases[name] = release
        return release

    def existing(
        self, episode, *, root_name=None, extra_files=(), selected_video_only=False,
        state="confirmed", group="GROUP",
    ):
        release = self.release(
            f"existing-{episode}", [episode], root_name=root_name,
            extra_files=extra_files, group=group,
        )
        metadata = inspect_torrent(self.torrents[release["guid"]])
        permit = self.permits.issue(
            infohash=metadata.infohash, metadata_sha256=metadata.metadata_sha256,
            selected_files=tuple(
                item.path for item in metadata.files
                if not selected_video_only or item.path.endswith(".mkv")
            ),
            budget_bytes=metadata.total_bytes, destination="/data/torrents", category="sonarr",
            reservation_id=self.reservation, scope_key=f"S01E{episode:02d}",
            capacity=CapacityEvidence(free_bytes=self.free_bytes, remaining_by_hash=self.remaining),
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        self.confirm(permit)
        self.remaining[permit.infohash] = 0
        TorrentArtifactStore(self.repo.path).put(permit, self.torrents[release["guid"]])
        if state != "confirmed":
            with sqlite3.connect(self.repo.path) as connection:
                connection.execute(
                    "UPDATE gateway_permits SET state=? WHERE token=?", (state, permit.token),
                )
        return permit

    def confirm(self, permit):
        self.permits.authorize(
            token=permit.token, infohash=permit.infohash, destination=permit.destination,
            metadata_sha256=permit.metadata_sha256, effect=lambda _: {"accepted": True},
        )
        self.remaining[permit.infohash] = permit.budget_bytes

    def handler(self, request):
        path = request.url.path
        if path == "/api/v3/config/downloadclient":
            return httpx.Response(200, json={"enableCompletedDownloadHandling": False})
        if path == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True})
        if path == "/api/v3/series":
            return httpx.Response(200, json=[{
                "id": 1, "tmdbId": 101, "monitored": True, "runtime": 45,
            }])
        if path == "/api/v3/episode":
            return httpx.Response(200, json=[{
                "id": number, "seasonNumber": 1, "episodeNumber": number,
                "monitored": True, "hasFile": False,
                "airDateUtc": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            } for number in range(1, self.episode_count + 1)])
        if path == "/api/v3/release" and request.method == "GET":
            if "seasonNumber" in request.url.params:
                self.searched.append("pack")
                return httpx.Response(200, json=self.pack_offers)
            episode = int(request.url.params["episodeId"])
            self.searched.append(episode)
            return httpx.Response(200, json=self.episode_offers.get(episode, []))
        if path == "/1/download":
            name = request.url.params["id"]
            self.metadata.append(name)
            return httpx.Response(200, content=self.torrents[name])
        if path == "/api/v3/release" and request.method == "POST":
            body = json.loads(request.content)
            scope = "S01PACK" if len(body["episodeIds"]) > 1 else f"S01E{body['episodeIds'][0]:02d}"
            permit = self.permits.get_for_reservation(self.reservation, scope_key=scope)
            assert permit is not None
            self.confirm(permit)
            self.posts.append(body["guid"])
            return httpx.Response(200, json={"accepted": True})
        if path in ("/internal/source-state", "/internal/series-queue-state"):
            self.controls.append((path, json.loads(request.content)))
            return httpx.Response(200, json={"state": "started"})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def capacity(self):
        return CapacityEvidence(free_bytes=self.free_bytes, remaining_by_hash=self.remaining)

    def acquirer(self, client, **preferences):
        return SeriesAcquirer(
            repository=self.repo, permits=self.permits, client=client,
            sonarr_url="http://sonarr:8989", sonarr_api_key="fixture",
            prowlarr_url="http://prowlarr:9696", gateway_url="http://gateway:8081",
            arr_token="fixture", torrent_store=TorrentArtifactStore(self.repo.path),
            capacity_provider=self.capacity,
            release_policy=ReleasePolicy.from_environment({}, media_kind="series"),
            **preferences,
        )


@pytest.mark.asyncio
async def test_twenty_episode_pack_is_one_transfer_and_keeps_completed_individual_sources(tmp_path):
    fixture = SeriesFixture(tmp_path)
    preserved = {episode: fixture.existing(episode) for episode in (6, 7)}
    fixture.pack_offers = [fixture.release("pack", list(range(1, 21)))]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        acquirer = fixture.acquirer(client, prefer_season_pack=True)
        assert await acquirer.acquire("season:tmdb:101:1", fixture.reservation) == "grabbed"
        parent = fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK")
        assert parent is not None
        assert parent.budget_bytes == inspect_torrent(fixture.torrents["pack"]).total_bytes
        assert len(parent.selected_files) == 40
        for episode in range(1, 21):
            permit = fixture.permits.get_for_reservation(
                fixture.reservation, scope_key=f"S01E{episode:02d}",
            )
            assert permit is not None
            if episode in preserved:
                assert permit.permit_id == preserved[episode].permit_id
            else:
                assert permit.season_pack_parent_id == parent.permit_id
        fixture.controls.clear()
        restarted = fixture.acquirer(client, prefer_season_pack=True, download_window=2)
        assert await restarted.acquire("season:tmdb:101:1", fixture.reservation) == (
            "waiting_episodes"
        )
        assert fixture.controls == [("/internal/source-state", {
            "permit_token": parent.token, "action": "start",
        })]
        fixture.remaining[parent.infohash] = 0
        fixture.controls.clear()
        await restarted.acquire("season:tmdb:101:1", fixture.reservation)
        assert not any(body["permit_token"] == parent.token for _, body in fixture.controls)
    assert fixture.posts == ["pack"]
    assert fixture.searched == ["pack"]
    with sqlite3.connect(fixture.repo.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM gateway_permits").fetchone()[0] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["incomplete", "weak", "capacity"])
async def test_bad_or_weak_or_nonfitting_pack_falls_back_to_good_episode(tmp_path, failure):
    fixture = SeriesFixture(tmp_path, free_bytes=3_000_000_000 if failure == "capacity" else
                            100_000_000_000)
    fixture.pack_offers = [fixture.release(
        "pack", list(range(1, 20 if failure == "incomplete" else 21)),
        seeds=0 if failure == "weak" else 10,
    )]
    fixture.episode_offers[1] = [fixture.release("episode-one", [1], seeds=20)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == ["episode-one"]
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_seeds,expected", [(6, "same-family"), (4, "other-family")])
async def test_series_affinity_survives_restart_but_does_not_prefer_weak_family(
    tmp_path, matching_seeds, expected,
):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.episode_offers[1] = [fixture.release("first", [1], group="PLAY", seeds=20)]
    fixture.episode_offers[2] = [
        fixture.release("other-family", [2], group="OTHER", seeds=100),
        fixture.release("same-family", [2], group="PLAY", seeds=matching_seeds),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, release_affinity=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
        assert await fixture.acquirer(client, release_affinity=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == ["first", expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("reasons,expected", [
    (["Existing file meets cutoff: WEB-DL-1080p"], "pack"),
    (["Release in queue already meets cutoff: WEB-DL-1080p"], "pack"),
    (["Existing file meets cutoff: WEB-DL-1080p", "Unknown Series"], "single"),
    (["Existing file meets cutoff: WEB-DL-1080p", "Quality is below minimum size"], "single"),
])
async def test_pack_partial_season_cutoff_does_not_bypass_other_native_vetoes(
    tmp_path, reasons, expected,
):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.existing(2)
    fixture.pack_offers = [{
        **fixture.release("pack", [1, 2]), "rejected": True, "rejections": reasons,
    }]
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == [expected]


@pytest.mark.asyncio
async def test_healthy_uindex_takes_priority_over_previous_1337x_family(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.episode_offers[1] = [{
        **fixture.release("first", [1], group="PLAY", seeds=20),
        "indexer": "1337x (Prowlarr)",
    }]
    fixture.episode_offers[2] = [
        fixture.release("uindex", [2], group="OTHER", seeds=10),
        {**fixture.release("old-family", [2], group="PLAY", seeds=100),
         "indexer": "1337x (Prowlarr)"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        for _ in range(2):
            assert await fixture.acquirer(client, release_affinity=True).acquire(
                "season:tmdb:101:1", fixture.reservation,
            ) == "grabbed"
    assert fixture.posts == ["first", "uindex"]


@pytest.mark.asyncio
async def test_concurrent_episode_admission_releases_unused_pack_budget(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.pack_offers = [fixture.release("pack", [1, 2])]
    capacity = fixture.capacity

    async def competing_admission():
        fixture.capacity = capacity
        fixture.existing(1)
        return await capacity()

    fixture.capacity = competing_admission
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "pack_binding_conflict"
    assert fixture.posts == []
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01E01") is not None
    assert fixture.repo.active_reservation(fixture.reservation)["budget_bytes"] == 1_500_001_000


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    "confirmed", "authorized", "unknown", "superseded", "probe_rejected",
])
async def test_pack_cannot_overwrite_any_retained_source_at_the_same_path(tmp_path, state):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    retained = fixture.existing(2, root_name="shared", state=state)
    fixture.pack_offers = [fixture.release("pack", [1, 2], root_name="shared")]
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == ["single"]
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None
    assert fixture.permits.get(retained.token).state == state
    assert not any(body["permit_token"] == retained.token for _, body in fixture.controls)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_root,new_root,old_extra,new_extra,state,expected", [
    ("shared", "shared", "payload", "payload/notes.txt", "confirmed", "single"),
    ("shared", "shared", "payload/notes.txt", "payload", "confirmed", "single"),
    ("shared", "shared", "payload", "payload-other/notes.txt", "confirmed", "pack"),
    ("Shared", "shared", "payload", "payload", "confirmed", "pack"),
    ("shared", "shared", "payload", "payload", "retired", "pack"),
    ("shared", "shared", "payload", "payload", "revoked", "pack"),
])
async def test_pack_path_guard_uses_full_artifact_and_linux_path_boundaries(
    tmp_path, old_root, new_root, old_extra, new_extra, state, expected,
):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    fixture.existing(
        2, root_name=old_root, extra_files=(old_extra,), selected_video_only=True,
        state=state, group="OLD",
    )
    fixture.pack_offers = [fixture.release(
        "pack", [1, 2], root_name=new_root, extra_files=(new_extra,),
    )]
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == [expected]


@pytest.mark.asyncio
async def test_pack_cannot_overwrite_source_retained_from_another_request(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    requested_reservation = fixture.reservation
    fixture.reservation = fixture.repo.reserve(
        request_id="other", source_id="other", media_key="season:tmdb:202:1",
        filesystem_id="fixture", budget_bytes=0, free_bytes=fixture.free_bytes,
        total_bytes=200_000_000_000,
    ).reservation_id
    retained = fixture.existing(2, root_name="shared")
    fixture.reservation = requested_reservation
    fixture.pack_offers = [fixture.release("pack", [1, 2], root_name="shared")]
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == ["single"]
    assert fixture.permits.get(retained.token).state == "confirmed"
    assert fixture.permits.get_for_reservation(fixture.reservation, scope_key="S01PACK") is None
    assert not any(body["permit_token"] == retained.token for _, body in fixture.controls)


@pytest.mark.asyncio
async def test_invalid_retained_artifact_cannot_hide_a_payload_path_conflict(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    retained = fixture.existing(
        2, root_name="shared", extra_files=("payload",), selected_video_only=True, group="OLD",
    )
    with sqlite3.connect(fixture.repo.path) as connection:
        connection.execute(
            "UPDATE torrent_artifacts SET content=? WHERE permit_id=?",
            (b"invalid metadata", retained.permit_id),
        )
    fixture.pack_offers = [fixture.release(
        "pack", [1, 2], root_name="shared", extra_files=("payload/notes.txt",),
    )]
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == ["single"]


@pytest.mark.asyncio
async def test_legacy_source_without_artifact_still_protects_selected_video(tmp_path):
    fixture = SeriesFixture(tmp_path, episode_count=2)
    retained = fixture.existing(2, root_name="shared")
    with sqlite3.connect(fixture.repo.path) as connection:
        connection.execute("DELETE FROM torrent_artifacts WHERE permit_id=?", (retained.permit_id,))
    fixture.pack_offers = [fixture.release("pack", [1, 2], root_name="shared")]
    fixture.episode_offers[1] = [fixture.release("single", [1])]
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.handler)) as client:
        assert await fixture.acquirer(client, prefer_season_pack=True).acquire(
            "season:tmdb:101:1", fixture.reservation,
        ) == "grabbed"
    assert fixture.posts == ["single"]
