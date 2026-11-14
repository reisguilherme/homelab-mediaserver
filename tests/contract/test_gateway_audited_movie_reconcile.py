"""Audited movie collections reconcile only their sealed exact native source."""

import copy
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


class AppliedMovieWithoutResponse:
    def __init__(self, native_proof):
        self.native_proof = native_proof
        self.payload = None
        self.effects = 0

    def add_torrent(self, payload):
        self.payload = payload
        self.effects += 1
        raise EffectUncertain("fixture applied without acknowledged response")

    def read(self, path, params=None):
        assert path == "/api/v2/torrents/info"
        native = {"hash": self.payload["infohash"], "category": "radarr",
                  "save_path": "/data/torrents", "total_size": 101}
        changes = {
            "native_category": {"category": "sonarr"},
            "native_destination": {"save_path": "/data/torrents/foreign"},
            "native_size": {"total_size": 102},
            "native_other_hash": {"hash": "b" * 40},
        }
        native.update(changes.get(self.native_proof, {}))
        if self.native_proof == "native_duplicate":
            return [native, copy.deepcopy(native)]
        return [native]


@pytest.mark.parametrize(
    "proof,expected_status",
    [("sealed", 200), ("unsealed", 403), ("unpublished", 403),
     ("unpersisted", 403), ("wrong_budget", 403), ("wrong_metadata", 403),
     ("wrong_candidate", 403), ("wrong_permit", 403), ("wrong_reservation", 403),
     ("foreign_category", 403), ("non_audited_collection", 403),
     ("foreign_media_key", 403), ("native_category", 409),
     ("native_destination", 409), ("native_size", 409), ("native_duplicate", 409),
     ("native_other_hash", 200)],
)
def test_audited_movie_reconcile_requires_exact_job_and_native_identity(
    tmp_path, proof, expected_status,
):
    torrent = (b"d4:infod6:lengthi101e4:name8:Film.mkv12:piece lengthi16384e"
               b"6:pieces20:aaaaaaaaaaaaaaaaaaaaee")
    parsed = inspect_torrent(torrent)
    candidate = sha256(("torrent:" + parsed.infohash).encode()).hexdigest()
    database = tmp_path / "control.sqlite"
    repository = ReservationRepository(database)
    repository.initialize()
    media_key = "collection:audited:" + candidate
    if proof == "non_audited_collection":
        media_key = "collection:unreviewed:" + candidate
    elif proof == "foreign_media_key":
        media_key = "series:tmdb:777270"
    reserved = repository.reserve(
        request_id="operator:audited:" + candidate, source_id="operator:uindex:42",
        media_key=media_key, filesystem_id="fixture-uuid", budget_bytes=0,
        free_bytes=100000, total_bytes=200000,
    )
    assert reserved.accepted
    permits = PermitRegistry(database)
    permit = permits.issue(
        infohash=parsed.infohash, metadata_sha256=parsed.metadata_sha256,
        destination="/data/torrents", category="radarr", scope_key=None,
        reservation_id=reserved.reservation_id, selected_files=("Film.mkv",),
        budget_bytes=101, reported_seeders=1, capacity=CapacityEvidence(100000, {}),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    artifacts = TorrentArtifactStore(database)
    artifacts.put(permit, torrent)
    job = {
        "job_id": "fixture-movie-job", "candidate_id": candidate,
        "entry_id": "movie-belfast", "permit_id": permit.permit_id,
        "reservation_id": permit.reservation_id, "category": "radarr",
        "tmdb_id": 777270, "tvdb_id": None, "metadata_sha256": parsed.metadata_sha256,
        "expected_payload_bytes": 101, "padding_bytes": 0,
        "release_title": "Belfast 2021 1080p BluRay",
        "payload_files": [{"path": "Film.mkv", "size_bytes": 101}],
        "files": [{"path": "Film.mkv", "size_bytes": 101,
                   "kind": "movie", "native_movie_id": 42}], "excluded_files": [],
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
    elif proof == "wrong_reservation":
        job["reservation_id"] = "other-reservation"
    elif proof == "foreign_category":
        job["category"] = "sonarr"
        job["files"] = [{"path": "Film.mkv", "size_bytes": 101, "kind": "episode",
                         "native_series_id": 30, "native_episode_ids": [40],
                         "canonical_episode_ids": [110413], "scopes": ["S01E01"]}]
    manifest = {"schema_version": 1, "mode": "audited_manifest",
                "manifest_id": "fixture-movie-manifest", "manifest_sha256": "c" * 64,
                "jobs": [job]}
    if proof not in {"unsealed", "unpersisted"}:
        AuditedImportStore(database).install_manifest(manifest)
    if proof not in {"unsealed", "unpublished"}:
        path = tmp_path / "audited-imports.json"
        path.write_text(json.dumps(manifest))
        os.chmod(path, 0o600)
    upstream = AppliedMovieWithoutResponse(proof)
    app = create_app(
        permits=permits, upstream=upstream, torrent_store=artifacts,
        arr_token="fixture-token", capacity_provider=lambda: CapacityEvidence(100000, {}),
        worker_health_provider=lambda: True,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        initial = client.post(
            "/api/v2/torrents/add",
            headers={"X-Arr-Token": "fixture-token", "X-Admission-Permit": permit.token},
            data={"category": "radarr", "savepath": "/data/torrents", "stopped": "false"},
            files={"torrents": ("fixture.torrent", torrent, "application/x-bittorrent")},
        )
        assert initial.status_code != 200
        assert permits.get(permit.token).state == "unknown"
        response = client.post(
            "/internal/reconcile-source", headers={"X-Arr-Token": "fixture-token"},
            json={"permit_token": permit.token},
        )
        assert response.status_code == expected_status, response.json()
        confirmed = proof == "sealed"
        assert permits.get(permit.token).state == ("confirmed" if confirmed else "unknown")
        if confirmed:
            assert response.json() == {"state": "confirmed"}
            repeated = client.post(
                "/internal/reconcile-source", headers={"X-Arr-Token": "fixture-token"},
                json={"permit_token": permit.token},
            )
            assert repeated.status_code == 200
        elif proof == "native_other_hash":
            assert response.json() == {"state": "missing"}
    assert upstream.effects == 1
