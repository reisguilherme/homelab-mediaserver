"""Derived target publication, crash recovery and exact native-copy inode."""

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from test_audited_imports import FixtureHelpers
from test_audited_remux import create_part

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.audited_imports import AuditedImportStore
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.audited_imports import AuditedManifestImporter
from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence
from homeserver_control.worker.subtitle_language import SubtitlePolicy

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not shutil.which("ffmpeg"), reason="Linux physical derived import"
)


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(v) for v in value) + b"e"
    return b"d" + b"".join(encode(k) + encode(v) for k, v in sorted(value.items())) + b"e"


@pytest.fixture
def fixture(tmp_path):
    root = tmp_path / "torrents" / "Show"
    root.mkdir(parents=True)
    a = create_part(root, "part1")
    b = create_part(root, "part2", color="blue")
    payload = [{"path": "Show/" + p.name, "size_bytes": p.stat().st_size} for p in (a, b)]
    data = encode(
        {
            b"info": {
                b"name": b"Show",
                b"piece length": 16384,
                b"pieces": b"a" * (20 * ((sum(r["size_bytes"] for r in payload) + 16383) // 16384)),
                b"files": [
                    {b"length": row["size_bytes"], b"path": [Path(row["path"]).name.encode()]}
                    for row in payload
                ],
            }
        }
    )
    parsed = inspect_torrent(data)
    db = tmp_path / "control.sqlite"
    repo = ReservationRepository(db)
    repo.initialize()
    rid = repo.reserve(
        request_id="audited",
        source_id="exact",
        media_key="collection:audited:" + "a" * 64,
        filesystem_id="fixture",
        budget_bytes=0,
        free_bytes=10**8,
        total_bytes=2 * 10**8,
    ).reservation_id
    permits = PermitRegistry(db)
    permit = permits.issue(
        infohash=parsed.infohash,
        metadata_sha256=parsed.metadata_sha256,
        destination="/data/torrents",
        category="sonarr",
        reservation_id=rid,
        scope_key=None,
        selected_files=tuple(row["path"] for row in payload),
        budget_bytes=parsed.total_bytes,
        reported_seeders=1,
        capacity=CapacityEvidence(10**8, {}),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    # A fixture source already completed via its exact admitted parent.
    with permits._session() as c:
        c.execute("UPDATE gateway_permits SET state='confirmed',filesystem_id='fixture'")
    permit = permits.get(permit.token)
    store = AuditedImportStore(db)
    artifacts = TorrentArtifactStore(db)
    artifacts.put(permit, data)
    target = payload[0] | {
        "kind": "episode",
        "native_series_id": 1,
        "native_episode_ids": [11],
        "canonical_episode_ids": [389875],
        "scopes": ["S05E01"],
        "source_parts": payload,
        "derivation": {
            "recipe": "concat_mp4_streamcopy_v1",
            "max_output_bytes": 2 * parsed.total_bytes,
        },
    }
    job = {
        "job_id": "office",
        "candidate_id": "a" * 64,
        "permit_id": permit.permit_id,
        "reservation_id": rid,
        "category": "sonarr",
        "metadata_sha256": parsed.metadata_sha256,
        "entry_id": "series-office",
        "tmdb_id": 2316,
        "tvdb_id": 73244,
        "release_title": "The Office Season 5",
        "expected_payload_bytes": parsed.total_bytes,
        "padding_bytes": 0,
        "payload_files": payload,
        "files": [target],
        "excluded_files": [],
    }
    manifest = {
        "schema_version": 1,
        "mode": "audited_manifest",
        "manifest_id": "fixture",
        "manifest_sha256": "c" * 64,
        "jobs": [job],
    }
    store.install_manifest(manifest)
    fid = store.pending_files()[0]["file_id"]
    permits.reserve_audited_derivation(fid, CapacityEvidence(10**8, {}))
    state = SimpleNamespace(posts=[], native=None, copy=False, lose=False, refreshes=0)

    def handler(request):
        route = request.url.path
        if route == "/api/v2/torrents/info":
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": permit.infohash,
                        "category": "sonarr",
                        "save_path": "/data/torrents",
                        "content_path": "/data/torrents/Show",
                        "progress": 1,
                        "amount_left": 0,
                    }
                ],
            )
        if route == "/api/v2/torrents/files":
            return httpx.Response(
                200,
                json=[{"name": r["path"], "size": r["size_bytes"], "progress": 1} for r in payload],
            )
        if route == "/api/v3/series/1":
            return httpx.Response(
                200, json={"id": 1, "tmdbId": 2316, "tvdbId": 73244, "path": "/data/media/tv/Show"}
            )
        if route == "/api/v3/episode/11":
            return httpx.Response(
                200,
                json={
                    "id": 11,
                    "seriesId": 1,
                    "seasonNumber": 5,
                    "episodeNumber": 1,
                    "hasFile": state.native is not None,
                    "episodeFileId": 111 if state.native else 0,
                },
            )
        if route == "/api/v3/episodefile/111":
            return httpx.Response(200, json={"path": "/data/media/tv/" + state.native.name})
        if route == "/api/v3/config/mediamanagement":
            return httpx.Response(200, json={"copyUsingHardlinks": True, "importExtraFiles": False})
        if route == "/api/v3/manualimport":
            folder = Path(
                request.url.params["folder"].replace(
                    "/data/torrents", str(tmp_path / "torrents"), 1
                )
            )
            return httpx.Response(
                200,
                json=[
                    {
                        "path": "/data/torrents/" + str(p.relative_to(tmp_path / "torrents")),
                        "quality": {"quality": {"id": 0}},
                    }
                    for p in folder.glob("*.mp4")
                ],
            )
        if route == "/api/v3/command" and request.method == "POST":
            body = json.loads(request.content)
            state.posts.append(body)
            path = Path(
                body["files"][0]["path"].replace("/data/torrents", str(tmp_path / "torrents"), 1)
            )
            state.native = tmp_path / "sonarr" / "Weight Loss.mp4"
            if state.copy:
                shutil.copyfile(path, state.native)
            else:
                os.link(path, state.native)
            if state.lose:
                raise httpx.ReadTimeout("fixture lost response")
            return httpx.Response(201, json={"id": 1})
        raise AssertionError((request.method, route))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    helper = FixtureHelpers(tmp_path, client, "sonarr")
    helper.subtitle_policy = SubtitlePolicy(languages=("en-US",), skip_original_audio_languages=())

    class Storage:
        def inspect(self, pool, writable=False):
            return SimpleNamespace(filesystem_id="fixture", free_bytes=10**8)

        def resolve(self, pool, logical, writable=False):
            assert pool == "ssd"
            return tmp_path / "torrents" / Path(logical).relative_to("/data/torrents")

    helper.storage_registry = Storage()

    async def refresh():
        state.refreshes += 1
        return CapacityEvidence(10**8, {}, pools=(PoolCapacityEvidence("ssd", "fixture", 10**8),))

    worker = AuditedManifestImporter(
        store=store,
        manifest_loader=lambda: manifest,
        permits=permits,
        torrent_store=artifacts,
        helpers={"sonarr": helper},
        gateway_url="http://gateway",
        arr_token="fixture",
    )
    worker.capacity_provider = refresh
    return worker, state, store, permit, helper, fid, [a, b]


@pytest.mark.asyncio
async def test_parts_derive_once_import_one_target_and_keep_all_originals(fixture):
    worker, state, store, permit, helper, fid, parts = fixture
    identities = [(p.stat().st_ino, p.read_bytes()) for p in parts]
    await worker.run_once()
    assert len(state.posts) == 1 and state.posts[0]["files"][0]["episodeIds"] == [11]
    assert state.posts[0]["importMode"] == "Copy"
    row = store.derivation(fid)
    assert row["state"] == "published"
    assert [(p.stat().st_ino, p.read_bytes()) for p in parts] == identities
    assert row["receipt"]["output"]["inode"] == state.native.stat().st_ino
    assert state.refreshes == 1 and not store.pending_files(now=1e20)
    await worker.run_once()
    assert len(state.posts) == 1


@pytest.mark.asyncio
async def test_lost_native_response_recovers_exact_output_without_second_remux_or_post(fixture):
    worker, state, store, permit, helper, fid, parts = fixture
    state.lose = True
    await worker.run_once()
    assert len(state.posts) == 1
    row = store.derivation(fid)
    assert row["state"] == "published"
    # Deferred retry deliberately emulates restart; recorded output is recovered.
    with store._connect() as c:
        c.execute("UPDATE audited_import_files SET next_attempt=0")
    await worker.run_once()
    assert len(state.posts) == 1
    assert store.derivation(fid)["receipt"] == row["receipt"]


@pytest.mark.asyncio
async def test_copied_native_file_is_not_accepted_as_hardlink(fixture):
    worker, state, store, permit, helper, fid, parts = fixture
    state.copy = True
    await worker.run_once()
    assert len(state.posts) == 1
    assert store.pending_files(now=1e20)[0]["state"] != "complete"
    assert all(p.is_file() for p in parts)


@pytest.mark.asyncio
async def test_unknown_partial_stays_reserved_and_never_posts(fixture):
    worker, state, store, permit, helper, fid, parts = fixture
    root = helper.torrent_root / ".audited-derived" / fid
    root.mkdir(parents=True)
    (root / "output.partial.mp4").write_bytes(b"unknown")
    store.begin_derivation(fid)
    await worker.run_once()
    assert not state.posts and store.derivation(fid)["state"] == "writing"
    assert (root / "output.partial.mp4").read_bytes() == b"unknown"


@pytest.mark.asyncio
async def test_crash_after_publication_retains_budget_until_fresh_sample_and_recovers(fixture):
    worker, state, store, permit, helper, fid, parts = fixture
    refresh = worker.capacity_provider

    async def interrupted():
        raise RuntimeError("fixture capacity unavailable")

    worker.capacity_provider = interrupted
    await worker.run_once()
    row = store.derivation(fid)
    assert row["state"] == "verified" and not state.posts
    assert worker.permits.pending_bytes(CapacityEvidence(10**8, {})) >= row["budget_bytes"]
    inode = row["receipt"]["output"]["inode"]
    worker.capacity_provider = refresh
    with store._connect() as c:
        c.execute("UPDATE audited_import_files SET next_attempt=0")
    await worker.run_once()
    assert len(state.posts) == 1
    assert store.derivation(fid)["state"] == "published"
    assert state.native.stat().st_ino == inode
    assert worker.permits.pending_bytes(CapacityEvidence(10**8, {})) < row["budget_bytes"]


@pytest.mark.asyncio
async def test_missing_extra_ledger_cannot_import_only_the_anchor_part(fixture):
    worker, state, store, permit, helper, fid, parts = fixture
    with store._connect() as c:
        c.execute("DELETE FROM audited_derivations")
    await worker.run_once()
    assert not state.posts
    assert all(p.is_file() for p in parts)
