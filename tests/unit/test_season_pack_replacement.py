import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.worker.capacity_evidence import CapacityEvidence, PoolCapacityEvidence


@pytest.fixture
def setup_pack(tmp_path):
    repo = ReservationRepository(tmp_path / "control.sqlite")
    repo.initialize()
    reservation = repo.reserve(
        request_id="seerr:1:2", source_id="1:2", media_key="season:tmdb:123:2",
        filesystem_id="fixture", budget_bytes=0, free_bytes=50_000, total_bytes=100_000,
    )
    registry = PermitRegistry(repo.path)

    def issue(hash_char, scope, files, budget):
        return registry.issue(
            infohash=hash_char * 40, metadata_sha256="f" * 64,
            reservation_id=reservation.reservation_id, scope_key=scope,
            category="sonarr", destination="/data/torrents", selected_files=files,
            budget_bytes=budget, capacity=CapacityEvidence(50_000, {}),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    bindings = {f"S02E{n:02d}": (f"pack/Show.S02E{n:02d}.mkv",) for n in range(1, 4)}
    parent = issue("a", "S02PACK", tuple(path for files in bindings.values() for path in files),
                   12_000)
    olds = [issue(char, f"S02E{n:02d}", (f"single/Show.S02E{n:02d}.mkv",), 1000)
            for n, char in enumerate(("b", "c"), 1)]
    for old in olds:
        registry.authorize(token=old.token, infohash=old.infohash, destination=old.destination,
                           metadata_sha256=old.metadata_sha256, effect=lambda _: {"accepted": True})
    evidence = CapacityEvidence(50_000, {old.infohash: 600 for old in olds},
                                paused_hashes=frozenset(old.infohash for old in olds))
    return repo, registry, parent, olds, bindings, evidence


def bind(setup_pack, **overrides):
    _, registry, parent, olds, bindings, evidence = setup_pack
    arguments = {"episode_files": bindings, "replacement_tokens": tuple(x.token for x in olds),
                 "capacity": evidence}
    registry.bind_season_pack(parent.token, **(arguments | overrides))


def assert_unchanged(setup_pack):
    repo, registry, parent, olds, _, _ = setup_pack
    assert [registry.get(old.token).state for old in olds] == ["confirmed", "confirmed"]
    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("SELECT count(*) FROM season_pack_episodes").fetchone()[0] == 0
    assert registry.get(parent.token).state == "authorized"


def test_paused_incomplete_sources_rebind_atomically_and_preserve_new_missing_slots(setup_pack):
    repo, _, parent, olds, bindings, evidence = setup_pack
    bind(setup_pack)
    registry = PermitRegistry(repo.path)
    assert [registry.get(old.token).state for old in olds] == ["superseded", "superseded"]
    for scope, files in bindings.items():
        child = registry.get_for_reservation(parent.reservation_id, scope_key=scope)
        assert child.season_pack_parent_id == parent.permit_id
        assert child.selected_files == files
    assert registry.get(parent.token).state == "authorized"
    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("SELECT budget_bytes FROM reservations").fetchone()[0] == 12_000
        assert connection.execute("SELECT count(*) FROM gateway_permits").fetchone()[0] == 3
    # Superseded audit rows retain the original metadata and admitted paths.
    assert registry.get(olds[0].token).selected_files == olds[0].selected_files
    assert evidence.remaining_by_hash[olds[0].infohash] == 600


@pytest.mark.parametrize("change", ["running", "complete", "unknown", "omitted", "duplicate"])
def test_replacement_requires_explicit_paused_incomplete_sources(setup_pack, change):
    _, _, _, olds, _, evidence = setup_pack
    overrides = {}
    if change == "running":
        overrides["capacity"] = CapacityEvidence(50_000, evidence.remaining_by_hash)
    elif change in {"complete", "unknown"}:
        remaining = dict(evidence.remaining_by_hash)
        if change == "complete":
            remaining[olds[0].infohash] = 0
        else:
            remaining.pop(olds[0].infohash)
        overrides["capacity"] = CapacityEvidence(50_000, remaining,
                                                  paused_hashes=evidence.paused_hashes)
    elif change == "omitted":
        overrides["replacement_tokens"] = (olds[0].token,)
    else:
        overrides["replacement_tokens"] = (olds[0].token, olds[0].token)
    with pytest.raises((ValueError, PermissionError)):
        bind(setup_pack, **overrides)
    assert_unchanged(setup_pack)


@pytest.mark.parametrize("guard", ["import", "protected", "probe", "handover", "history"])
def test_import_protection_probes_handovers_and_other_history_block_replacement(setup_pack, guard):
    repo, registry, _, olds, _, _ = setup_pack
    with sqlite3.connect(repo.path) as connection:
        if guard == "import":
            connection.execute("INSERT INTO episode_imports(permit_id,state,updated_at) "
                               "VALUES (?, 'claimed', 'now')", (olds[0].permit_id,))
        elif guard == "protected":
            connection.execute("CREATE TABLE protected_sources (infohash TEXT, reason TEXT)")
            connection.execute("INSERT INTO protected_sources VALUES (?, 'manual')",
                               (olds[0].infohash,))
        elif guard == "handover":
            connection.execute("INSERT INTO source_handovers VALUES ('candidate', ?, 'pending')",
                               (olds[0].permit_id,))
    if guard in {"probe", "history"}:
        extra = registry.issue(infohash="d" * 40, destination="/data/torrents",
                               expires_at=datetime.now(UTC) + timedelta(hours=1))
        with sqlite3.connect(repo.path) as connection:
            connection.execute(
                "UPDATE gateway_permits SET reservation_id=?,scope_key=?,state=?,probe_parent_id=? "
                "WHERE permit_id=?",
                (olds[0].reservation_id, olds[0].scope_key,
                 "authorized" if guard == "probe" else "superseded",
                 olds[0].permit_id if guard == "probe" else None, extra.permit_id),
            )
    with pytest.raises(PermissionError):
        bind(setup_pack)
    assert_unchanged(setup_pack)


def test_old_sources_remaining_capacity_is_not_freed_by_superseding(setup_pack):
    _, _, _, _, _, evidence = setup_pack
    with pytest.raises(PermissionError, match="waiting_space"):
        bind(setup_pack, capacity=CapacityEvidence(12_500, evidence.remaining_by_hash,
                                                 paused_hashes=evidence.paused_hashes))
    assert_unchanged(setup_pack)


def test_insert_failure_rolls_back_superseding_and_all_bindings(setup_pack):
    repo, _, _, _, _, _ = setup_pack
    with sqlite3.connect(repo.path) as connection:
        connection.execute("CREATE TRIGGER fail_bind BEFORE INSERT ON season_pack_episodes "
                           "BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        bind(setup_pack)
    assert_unchanged(setup_pack)


@pytest.mark.parametrize("state", ["dispatching", "confirmed", "unknown"])
def test_parent_already_dispatched_cannot_replace_old_sources(setup_pack, state):
    repo, _, parent, olds, _, _ = setup_pack
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE gateway_permits SET state=? WHERE permit_id=?",
                           (state, parent.permit_id))
    with pytest.raises(PermissionError):
        bind(setup_pack)
    assert [PermitRegistry(repo.path).get(old.token).state for old in olds] == ["confirmed"] * 2


def test_default_binding_still_rejects_existing_episode_permits(setup_pack):
    _, registry, parent, _, bindings, _ = setup_pack
    with pytest.raises(PermissionError, match="episode_already_permitted"):
        registry.bind_season_pack(parent.token, episode_files=bindings)
    assert_unchanged(setup_pack)


@pytest.mark.parametrize("guard", ["cancelled", "expired", "already_bound", "tombstoned_missing"])
def test_parent_guards_leave_every_old_source_unchanged(setup_pack, guard):
    repo, registry, parent, olds, bindings, _ = setup_pack
    with sqlite3.connect(repo.path) as connection:
        if guard == "cancelled":
            connection.execute("UPDATE requests SET state='cancel_requested'")
        elif guard == "expired":
            connection.execute("UPDATE gateway_permits SET expires_at=? WHERE permit_id=?",
                               ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                                parent.permit_id))
        elif guard == "already_bound":
            connection.execute("INSERT INTO season_pack_episodes VALUES (?, 'S02E03', ?)",
                               (parent.permit_id, '["pack/Show.S02E03.mkv"]'))
        else:
            connection.execute("INSERT INTO tombstones VALUES "
                               "('episode:tmdb:123:S02E03', 'now', 'fixture')")
    with pytest.raises((ValueError, PermissionError)):
        bind(setup_pack)
    assert [registry.get(old.token).state for old in olds] == ["confirmed"] * 2
    assert registry.get(parent.token).state == "authorized"
    if guard != "already_bound":
        with sqlite3.connect(repo.path) as connection:
            assert not connection.execute("SELECT * FROM season_pack_episodes").fetchall()
    assert bindings["S02E03"] == ("pack/Show.S02E03.mkv",)


@pytest.mark.parametrize("remaining", [0, 600])
def test_registered_capacity_requires_each_old_physical_pool(setup_pack, monkeypatch, remaining):
    repo, registry, parent, olds, _, _ = setup_pack
    with sqlite3.connect(repo.path) as connection:
        connection.execute("UPDATE gateway_permits SET filesystem_id='ssd-id'")
        connection.execute("UPDATE gateway_permits SET pool_id='hdd',filesystem_id='hdd-id' "
                           "WHERE permit_id=?", (olds[0].permit_id,))
    # Pure placement fixture: no real mount or payload is accessed.
    monkeypatch.setattr(registry, "placement_valid", lambda _: True)
    evidence = CapacityEvidence(50_000, {}, pools=(
        PoolCapacityEvidence("ssd", "ssd-id", 50_000, {olds[1].infohash: 600},
                             paused_hashes=frozenset({olds[1].infohash})),
        PoolCapacityEvidence("hdd", "hdd-id", 50_000, {olds[0].infohash: remaining},
                             paused_hashes=frozenset({olds[0].infohash})),
    ))
    if remaining == 0:
        with pytest.raises(PermissionError, match="paused_incomplete_source_required"):
            bind(setup_pack, capacity=evidence)
        assert_unchanged(setup_pack)
    else:
        bind(setup_pack, capacity=evidence)
        assert registry.get(olds[0].token).pool_id == "hdd"
        assert registry.get(olds[0].token).state == "superseded"
        assert registry.get_for_reservation(parent.reservation_id,
                                            scope_key="S02E01").pool_id == "ssd"


@pytest.mark.parametrize("state", ["superseded", "probe_rejected"])
@pytest.mark.parametrize("history_evidence", ["paused", "zero", "queued", "unknown"])
def test_historical_payload_remains_without_blocking_only_when_stopped_or_zero(
    setup_pack, state, history_evidence
):
    repo, registry, _, olds, _, evidence = setup_pack
    historical = registry.issue(infohash="d" * 40, destination="/data/torrents",
                                expires_at=datetime.now(UTC) + timedelta(hours=1))
    with sqlite3.connect(repo.path) as connection:
        connection.execute(
            "UPDATE gateway_permits SET reservation_id=?,scope_key=?,state=?,probe_parent_id=? "
            "WHERE permit_id=?",
            (olds[0].reservation_id, olds[0].scope_key, state,
             olds[0].permit_id if state == "probe_rejected" else None, historical.permit_id),
        )
        # A rejected gateway probe may leave its old observing record behind.
        connection.execute("CREATE TABLE source_probes "
                           "(candidate_id TEXT,parent_id TEXT,decision TEXT)")
        connection.execute("INSERT INTO source_probes VALUES (?, ?, 'observing')",
                           (historical.permit_id, olds[0].permit_id))
    remaining = dict(evidence.remaining_by_hash)
    paused = set(evidence.paused_hashes)
    if history_evidence == "paused":
        paused.add(historical.infohash)
    elif history_evidence in {"zero", "queued"}:
        remaining[historical.infohash] = 0 if history_evidence == "zero" else 500
    capacity = CapacityEvidence(50_000, remaining, paused_hashes=frozenset(paused))
    if history_evidence in {"paused", "zero"}:
        bind(setup_pack, capacity=capacity)
        assert registry.get(historical.token).state == state
        assert registry.get(olds[0].token).state == "superseded"
    else:
        with pytest.raises(PermissionError, match="historical_source_not_stopped"):
            bind(setup_pack, capacity=capacity)
        assert_unchanged(setup_pack)


@pytest.mark.parametrize("guard", ["unbound", "cancelled", "season_deleted", "episode_deleted"])
def test_pack_authorization_refuses_inactive_or_unbound_parent_without_effect(setup_pack, guard):
    repo, registry, parent, _, _, _ = setup_pack
    if guard != "unbound":
        bind(setup_pack)
    with sqlite3.connect(repo.path) as connection:
        if guard == "cancelled":
            connection.execute("UPDATE requests SET state='cancel_requested'")
        elif guard in {"season_deleted", "episode_deleted"}:
            key = "season:tmdb:123:2" if guard == "season_deleted" else "episode:tmdb:123:S02E22"
            connection.execute("INSERT INTO tombstones VALUES (?, 'now', 'fixture')", (key,))
    effects = []
    with pytest.raises(PermissionError):
        registry.authorize(token=parent.token, infohash=parent.infohash,
                           destination=parent.destination, metadata_sha256=parent.metadata_sha256,
                           effect=lambda _: effects.append("called") or {"accepted": True})
    assert effects == []
    assert registry.get(parent.token).state == "authorized"


def test_pack_authorization_rechecks_cancellation_before_external_effect(setup_pack, monkeypatch):
    repo, registry, parent, _, _, _ = setup_pack
    bind(setup_pack)
    session = registry._session

    @contextmanager
    def cancellation_between_transactions():
        with sqlite3.connect(repo.path) as connection:
            connection.execute("UPDATE requests SET state='cancel_requested'")
        with session() as connection:
            yield connection

    monkeypatch.setattr(registry, "_session", cancellation_between_transactions)
    effects = []
    with pytest.raises(PermissionError, match="reservation_required"):
        registry.authorize(token=parent.token, infohash=parent.infohash,
                           destination=parent.destination, metadata_sha256=parent.metadata_sha256,
                           effect=lambda _: effects.append("called") or {"accepted": True})
    assert effects == []
    assert registry.get(parent.token).state == "unknown"


def test_cancel_unused_pack_removes_only_its_metadata_artifact(setup_pack):
    from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore

    repo, registry, parent, olds, _, _ = setup_pack
    TorrentArtifactStore(repo.path)
    with sqlite3.connect(repo.path) as connection:
        for permit in (parent, olds[0]):
            connection.execute("INSERT INTO torrent_artifacts VALUES (?, ?, ?, ?)",
                               (permit.permit_id, permit.infohash, permit.metadata_sha256,
                                b"metadata-only-fixture"))
    assert registry.cancel_authorized(parent.token)
    with sqlite3.connect(repo.path) as connection:
        assert connection.execute("SELECT permit_id FROM torrent_artifacts").fetchall() == [
            (olds[0].permit_id,)
        ]
    assert registry.get(olds[0].token).state == "confirmed"


def test_pack_quality_cannot_regress_known_old_rank(setup_pack):
    _, registry, parent, olds, _, _ = setup_pack
    registry.set_quality(olds[0].token, (2, 1080, 2))
    registry.set_quality(parent.token, (1, 1080, 3))
    with pytest.raises(PermissionError, match="quality_regression"):
        bind(setup_pack)
    assert_unchanged(setup_pack)
