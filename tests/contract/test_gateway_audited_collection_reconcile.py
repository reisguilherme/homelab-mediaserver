"""A real gateway and SQLite must recover only a sealed exact audited parent."""

import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient

from homeserver_control.adapters.http import EffectUncertain
from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.app import create_app
from homeserver_control.gateway.permits import PermitRegistry
from homeserver_control.persistence.audited_imports import AuditedImportStore
from homeserver_control.persistence.db import ReservationRepository
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.capacity_evidence import CapacityEvidence


def encode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    return b"d" + b"".join(encode(k) + encode(v) for k, v in sorted(value.items())) + b"e"


class AppliedButUncertain:
    def __init__(self):
        self.payload = None
        self.effects = 0

    def add_torrent(self, payload):
        self.payload = payload
        self.effects += 1
        raise EffectUncertain("fixture applied without acknowledged response")

    def read(self, path, params=None):
        assert path == "/api/v2/torrents/info"
        return [
            {
                "hash": self.payload["infohash"],
                "category": "sonarr",
                "save_path": "/data/torrents",
                "total_size": 101,
            }
        ]


@pytest.mark.parametrize(
    "proof",
    [
        "sealed",
        "unsealed",
        "unpublished",
        "unpersisted",
        "wrong_budget",
        "wrong_metadata",
        "wrong_candidate",
        "wrong_permit",
    ],
)
def test_unknown_collection_requires_published_and_persisted_exact_job(tmp_path, proof):
    torrent = encode(
        {
            b"info": {
                b"name": b"Office.S01E01.mkv",
                b"length": 101,
                b"piece length": 16384,
                b"pieces": b"a" * 20,
            }
        }
    )
    parsed = inspect_torrent(torrent)
    cid = sha256(("torrent:" + parsed.infohash).encode()).hexdigest()
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    reserved = repository.reserve(
        request_id="operator:audited:" + cid,
        source_id="operator:uindex:42",
        media_key="collection:audited:" + cid,
        filesystem_id="fixture-uuid",
        budget_bytes=0,
        free_bytes=100000,
        total_bytes=200000,
    )
    assert reserved.accepted
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash=parsed.infohash,
        metadata_sha256=parsed.metadata_sha256,
        destination="/data/torrents",
        category="sonarr",
        scope_key=None,
        reservation_id=reserved.reservation_id,
        selected_files=("Office.S01E01.mkv",),
        budget_bytes=101,
        reported_seeders=1,
        capacity=CapacityEvidence(100000, {}),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    artifacts = TorrentArtifactStore(database)
    artifacts.put(permit, torrent)
    job = {
        "job_id": "fixture-job",
        "candidate_id": cid,
        "entry_id": "series-office",
        "permit_id": permit.permit_id,
        "reservation_id": permit.reservation_id,
        "category": "sonarr",
        "tmdb_id": 2316,
        "tvdb_id": 73244,
        "metadata_sha256": parsed.metadata_sha256,
        "expected_payload_bytes": 101,
        "padding_bytes": 0,
        "release_title": "The Office US Season 1",
        "payload_files": [{"path": "Office.S01E01.mkv", "size_bytes": 101}],
        "files": [
            {
                "path": "Office.S01E01.mkv",
                "size_bytes": 101,
                "kind": "episode",
                "native_series_id": 30,
                "native_episode_ids": [40],
                "canonical_episode_ids": [110413],
                "scopes": ["S01E01"],
            }
        ],
        "excluded_files": [],
    }
    if proof == "wrong_budget":
        job["expected_payload_bytes"] = 102
        job["payload_files"][0]["size_bytes"] = 102
        job["files"][0]["size_bytes"] = 102
    elif proof == "wrong_metadata":
        job["metadata_sha256"] = "a" * 64
    elif proof == "wrong_candidate":
        job["candidate_id"] = "b" * 64
    elif proof == "wrong_permit":
        job["permit_id"] = "other-permit"
    manifest = {
        "schema_version": 1,
        "mode": "audited_manifest",
        "manifest_id": "fixture-manifest",
        "manifest_sha256": "c" * 64,
        "jobs": [job],
    }
    if proof not in {"unsealed", "unpersisted"}:
        AuditedImportStore(database).install_manifest(manifest)
    if proof not in {"unsealed", "unpublished"}:
        path = tmp_path / "audited-imports.json"
        path.write_text(json.dumps(manifest))
        os.chmod(path, 0o600)
    upstream = AppliedButUncertain()
    app = create_app(
        permits=permits,
        upstream=upstream,
        torrent_store=artifacts,
        arr_token="fixture-token",
        capacity_provider=lambda: CapacityEvidence(100000, {}),
        worker_health_provider=lambda: True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        initial = client.post(
            "/api/v2/torrents/add",
            headers={"X-Arr-Token": "fixture-token", "X-Admission-Permit": permit.token},
            data={"category": "sonarr", "savepath": "/data/torrents", "stopped": "false"},
            files={"torrents": ("fixture.torrent", torrent, "application/x-bittorrent")},
        )
        assert initial.status_code != 200
        assert permits.get(permit.token).state == "unknown"
        response = client.post(
            "/internal/reconcile-source",
            headers={"X-Arr-Token": "fixture-token"},
            json={"permit_token": permit.token},
        )
        if proof == "sealed":
            assert response.status_code == 200, response.json()
            assert response.json() == {"state": "confirmed"}
            assert permits.get(permit.token).state == "confirmed"
            again = client.post(
                "/internal/reconcile-source",
                headers={"X-Arr-Token": "fixture-token"},
                json={"permit_token": permit.token},
            )
            assert again.status_code == 200
        else:
            assert response.status_code == 403
            assert permits.get(permit.token).state == "unknown"
    assert upstream.effects == 1
