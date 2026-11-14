"""Read-only descriptor lookup validates fresh bindings, not cached authority."""

import sqlite3
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore

TORRENT = (
    b"d4:infod6:lengthi10e4:name9:video.mkv12:piece lengthi16384e6:pieces20:" + b"a" * 20 + b"ee"
)


def prepared(tmp_path, content=TORRENT):
    database = tmp_path / "control.sqlite"
    ReservationRepository(database).initialize()
    permits = PermitRegistry(database)
    inspected = inspect_torrent(content)
    permit = permits.issue(
        infohash=inspected.infohash,
        destination="/data/torrents",
        category="radarr",
        budget_bytes=inspected.total_bytes,
        metadata_sha256=inspected.metadata_sha256,
        selected_files=tuple(file.path for file in inspected.files),
        reported_seeders=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    store = TorrentArtifactStore(database)
    store.put(permit, content)
    return store, permits, permit, database


def test_exact_lookup_is_read_only_and_reuses_only_cryptographic_parse(tmp_path, monkeypatch):
    store, _, permit, database = prepared(tmp_path)
    connect = sqlite3.connect
    sql = []

    def traced(path, *args, **kwargs):
        assert str(path).endswith("?mode=ro") and kwargs["uri"] is True
        result = connect(path, *args, **kwargs)
        result.set_trace_callback(sql.append)
        return result

    monkeypatch.setattr(sqlite3, "connect", traced)
    assert store.verified_for_infohash(permit.infohash) == TORRENT
    assert store.verified_for_infohash(permit.infohash) == TORRENT
    assert sql and all(row.startswith(("BEGIN", "SELECT")) for row in sql)


@pytest.mark.parametrize(
    "mutation", ["blob", "artifact_digest", "permit_digest", "permit_v1", "budget", "files"]
)
def test_cached_crypto_does_not_hide_fresh_binding_drift(tmp_path, mutation):
    store, _, permit, database = prepared(tmp_path)
    assert store.verified_for_infohash(permit.infohash) == TORRENT
    with sqlite3.connect(database) as connection:
        if mutation == "blob":
            connection.execute("UPDATE torrent_artifacts SET content=x'61'")
        elif mutation == "artifact_digest":
            connection.execute("UPDATE torrent_artifacts SET metadata_sha256=?", ("f" * 64,))
        elif mutation == "permit_digest":
            connection.execute("UPDATE gateway_permits SET metadata_sha256=?", ("f" * 64,))
        elif mutation == "permit_v1":
            connection.execute("UPDATE gateway_permits SET infohash=?", ("f" * 40,))
        elif mutation == "budget":
            connection.execute("UPDATE gateway_permits SET budget_bytes=1")
        else:
            connection.execute("UPDATE gateway_permits SET selected_files_json='[]'")
    with pytest.raises(ValueError):
        store.verified_for_infohash(permit.infohash)


def test_two_different_sealed_descriptors_for_same_v1_are_ambiguous(tmp_path):
    store, permits, permit, database = prepared(tmp_path)
    other = b"d8:announce20:https://fixture.test" + TORRENT[1:]
    # Different tracker fields do not change the exact info dictionary.
    inspected = inspect_torrent(other)
    assert (
        inspected.infohash == permit.infohash
        and sha256(other).hexdigest() != permit.metadata_sha256
    )
    another = permits.issue(
        infohash=permit.infohash,
        destination="/data/torrents",
        category="radarr",
        budget_bytes=10,
        metadata_sha256=inspected.metadata_sha256,
        selected_files=("video.mkv",),
        reported_seeders=1,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    store.put(another, other)
    with pytest.raises(ValueError):
        store.verified_for_infohash(permit.infohash)


def test_identity_lookup_is_not_an_authorization_cache(tmp_path):
    store, _, permit, database = prepared(tmp_path)
    assert store.verified_for_infohash(permit.infohash) == TORRENT
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE gateway_permits SET state='failed'")
    # Gateway permit guards still decide authority for each operation.
    assert store.verified_for_infohash(permit.infohash) == TORRENT
    assert store.verified_for_infohash("f" * 40) is None
