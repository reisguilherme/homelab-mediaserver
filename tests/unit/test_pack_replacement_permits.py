import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence


def encode(value):
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    return b"d" + b"".join(encode(key) + encode(item) for key, item in sorted(value.items())) + b"e"


def torrent(group):
    return encode({b"info": {
        b"name": group.encode(), b"piece length": 16384, b"pieces": b"a" * 20,
        b"files": [{b"length": 123, b"path": [f"Series.S01E0{n}.{group}.mkv".encode()]}
                   for n in (1, 2)],
    }})


class Storage:
    pool_ids = ("ssd", "hdd")

    def inspect(self, name, *, writable=False):
        return SimpleNamespace(filesystem_id=f"{name}-uuid", free_bytes=10_000)

    def prepare_destination(self, name, permit_id):
        return f"/data/torrents/.placements/{permit_id}"

    validate_destination = prepare_destination


def fixture(tmp_path):
    database = tmp_path / "control.sqlite"
    repo = ReservationRepository(database)
    repo.initialize()
    reserved = repo.reserve(
        request_id="seerr:1", source_id="1", media_key="season:tmdb:1:1",
        filesystem_id="ssd-uuid", budget_bytes=1, free_bytes=10_000, total_bytes=20_000,
    )
    permits = PermitRegistry(database, storage_registry=Storage())
    content = torrent("OLD")
    manifest = inspect_torrent(content)
    capacity = CapacityEvidence(10_000, {}, pools=tuple(
        PoolCapacityEvidence(pool, f"{pool}-uuid", 10_000, {}) for pool in ("ssd", "hdd")
    ))
    old = permits.issue(
        infohash=manifest.infohash, metadata_sha256=manifest.metadata_sha256,
        selected_files=tuple(file.path for file in manifest.files), budget_bytes=246,
        destination="/data/torrents", category="sonarr", scope_key="S01PACK",
        reservation_id=reserved.reservation_id, capacity=capacity,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    bindings = {f"S01E0{n}": (file.path,) for n, file in enumerate(manifest.files, 1)}
    permits.bind_season_pack(old.token, episode_files=bindings)
    permits.set_quality(old.token, (1, 1080))
    permits.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256, effect=lambda _: {"accepted": True},
    )
    TorrentArtifactStore(database).put(old, content)
    capacity = CapacityEvidence(10_000, {}, pools=tuple(
        PoolCapacityEvidence(pool, f"{pool}-uuid", 10_000,
                             {old.infohash: 200} if pool == "ssd" else {},
                             paused_hashes=frozenset({old.infohash}))
        for pool in ("ssd", "hdd")
    ))
    new_content = torrent("NEW")
    new_manifest = inspect_torrent(new_content)
    args = dict(
        infohash=new_manifest.infohash, metadata_sha256=new_manifest.metadata_sha256,
        selected_files=tuple(file.path for file in new_manifest.files), budget_bytes=246,
        capacity=capacity, expires_at=datetime.now(UTC) + timedelta(hours=1),
        torrent_content=new_content, quality_rank=(1, 1080),
        episode_files={f"S01E0{n}": (file.path,)
                       for n, file in enumerate(new_manifest.files, 1)},
    )
    return permits, old, args


def test_pack_replacement_atomically_rebinds_every_child_and_persists_metadata(tmp_path):
    permits, old, args = fixture(tmp_path)
    replacement = permits.replace_confirmed(old.token, **args)
    assert permits.get(old.token).state == "superseded"
    assert replacement.quality_rank == (1, 1080)
    assert permits.season_pack_bindings(replacement.token) == args["episode_files"]
    assert TorrentArtifactStore(permits._db_path).get(replacement) == args["torrent_content"]
    for scope, selected in args["episode_files"].items():
        child = permits.get_for_reservation(old.reservation_id, scope_key=scope)
        assert child.season_pack_parent_id == replacement.permit_id
        assert child.selected_files == selected


@pytest.mark.parametrize("guard", [
    "missing_child", "extra_child", "import", "tombstone", "quality",
])
def test_pack_replacement_refuses_partial_binding_or_started_import(tmp_path, guard):
    permits, old, args = fixture(tmp_path)
    if guard == "missing_child":
        args["episode_files"].pop("S01E02")
    elif guard == "extra_child":
        args["episode_files"]["S01E03"] = args["episode_files"]["S01E02"]
    elif guard == "quality":
        args["quality_rank"] = (1, 720)
    else:
        with sqlite3.connect(permits._db_path) as connection:
            if guard == "import":
                connection.execute(
                    "INSERT INTO episode_imports VALUES (?,'importing',NULL,'now')",
                    (f"{old.permit_id}:S01E01",),
                )
            else:
                connection.execute(
                    "INSERT INTO tombstones VALUES ('episode:tmdb:1:S01E02','now','fixture')",
                )
    with pytest.raises((PermissionError, ValueError)):
        permits.replace_confirmed(old.token, **args)
    assert permits.get(old.token).state == "confirmed"


def test_torrent_artifact_insert_failure_rolls_back_pack_handover(tmp_path):
    permits, old, args = fixture(tmp_path)
    with sqlite3.connect(permits._db_path) as connection:
        connection.execute("""CREATE TRIGGER fail_new_artifact BEFORE INSERT ON torrent_artifacts
            BEGIN SELECT RAISE(ABORT, 'fixture failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        permits.replace_confirmed(old.token, **args)
    assert permits.get(old.token).state == "confirmed"
    parent = permits.get_for_reservation(old.reservation_id, scope_key="S01PACK")
    assert parent.permit_id == old.permit_id
