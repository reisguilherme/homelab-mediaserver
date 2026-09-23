from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import sleep

import pytest

from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.worker.capacity_evidence import CapacityEvidence


def test_concurrent_authorization_runs_external_effect_once() -> None:
    registry = PermitRegistry()
    permit = registry.issue(
        infohash="a" * 40,
        destination="/data/torrents",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    effect_started = Event()
    release_effect = Event()
    calls: list[str] = []

    def effect(_permit: object) -> dict[str, bool]:
        calls.append("called")
        effect_started.set()
        release_effect.wait(timeout=2)
        return {"accepted": True}

    def authorize() -> dict[str, bool]:
        return registry.authorize(
            token=permit.token,
            infohash=permit.infohash,
            destination=permit.destination,
            effect=effect,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(authorize)
        assert effect_started.wait(timeout=2)
        second = pool.submit(authorize)
        sleep(0.05)
        release_effect.set()
        assert first.result(timeout=2) == {"accepted": True}
        assert second.result(timeout=2) == {"accepted": True}

    assert calls == ["called"]


def test_sqlite_registry_preserves_confirmed_result_across_instances() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "control.sqlite"
        first = PermitRegistry(database)
        permit = first.issue(
            infohash="b" * 40,
            destination="/data/torrents",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        calls: list[str] = []
        result = first.authorize(
            token=permit.token,
            infohash=permit.infohash,
            destination=permit.destination,
            effect=lambda _permit: calls.append("first") or {"accepted": True},
        )

        second = PermitRegistry(database)
        repeated = second.authorize(
            token=permit.token,
            infohash=permit.infohash,
            destination=permit.destination,
            effect=lambda _permit: calls.append("second") or {"accepted": False},
        )

        assert result == repeated == {"accepted": True}
        assert calls == ["first"]


def test_late_dispatch_error_cannot_demote_reconciled_replacement() -> None:
    registry = PermitRegistry()
    old = registry.issue(
        infohash="a" * 40, metadata_sha256="b" * 64,
        destination="/data/torrents", category="radarr",
        reservation_id="movie-reservation", selected_files=("old.mkv",),
        budget_bytes=1_000,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    registry.authorize(
        token=old.token, infohash=old.infohash, destination=old.destination,
        metadata_sha256=old.metadata_sha256,
        effect=lambda _: {"accepted": True, "infohash": old.infohash},
    )
    new = registry.replace_confirmed(
        old.token, infohash="c" * 40, metadata_sha256="d" * 64,
        selected_files=("new.mkv",), budget_bytes=1_000,
        capacity=CapacityEvidence(
            free_bytes=2_000, remaining_by_hash={old.infohash: 800},
            paused_hashes=frozenset({old.infohash}),
        ), expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    def late_failure(_permit):
        registry.confirm_replacement(new.token)
        raise RuntimeError("late upstream error")

    with pytest.raises(RuntimeError, match="late upstream error"):
        registry.authorize(
            token=new.token, infohash=new.infohash, destination=new.destination,
            metadata_sha256=new.metadata_sha256, effect=late_failure,
        )
    assert registry.get(new.token).state == "confirmed"
