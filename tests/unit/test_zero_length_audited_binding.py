"""Empty auxiliaries stay in the whole payload and must exist physically."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_zero_length_torrent_bytes import multifile

from homeserver_control.domain.torrent_bytes import inspect_torrent
from homeserver_control.gateway.permits import Permit
from homeserver_control.persistence.audited_imports import validate_manifest
from homeserver_control.persistence.torrent_artifacts import TorrentArtifactStore
from homeserver_control.worker.audited_imports import AuditedManifestImporter
from homeserver_control.worker.validation import ValidationError


def fixture_job(metadata):
    return {
        "job_id": "fixture-job",
        "permit_id": "fixture-permit",
        "reservation_id": "fixture-reserve",
        "candidate_id": "b" * 64,
        "metadata_sha256": metadata.metadata_sha256,
        "category": "radarr",
        "entry_id": "movie:1",
        "tmdb_id": 1,
        "release_title": "Fixture.1080p.WEB-DL",
        "expected_payload_bytes": 123,
        "padding_bytes": 0,
        "payload_files": [
            {"path": file.path, "size_bytes": file.length} for file in metadata.files
        ],
        "files": [
            {
                "path": "fixture/video.mkv",
                "size_bytes": 123,
                "kind": "movie",
                "native_movie_id": 1,
                "canonical_episode_ids": [],
                "scopes": [],
            }
        ],
        "excluded_files": [{"path": "fixture/aux.nfo", "size_bytes": 0, "reason": "nonvideo"}],
    }


def test_audited_partition_retains_zero_aux_but_refuses_zero_import_video():
    raw, _ = multifile(
        [{b"length": 0, b"path": [b"aux.nfo"]}, {b"length": 123, b"path": [b"video.mkv"]}]
    )
    job = fixture_job(inspect_torrent(raw))
    data = {
        "schema_version": 1,
        "mode": "audited_manifest",
        "manifest_id": "fixture",
        "manifest_sha256": "a" * 64,
        "jobs": [job],
    }
    checked = validate_manifest(data)["jobs"][0]
    assert checked["payload_files"][0] == {"path": "fixture/aux.nfo", "size_bytes": 0}
    assert checked["excluded_files"] == job["excluded_files"]
    job["payload_files"][1]["size_bytes"] = job["files"][0]["size_bytes"] = 0
    job["payload_files"].append({"path": "fixture/extra.txt", "size_bytes": 123})
    job["excluded_files"].append(
        {"path": "fixture/extra.txt", "size_bytes": 123, "reason": "nonvideo"}
    )
    with pytest.raises(ValueError):
        validate_manifest(data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed", [None, "physical_absent", "gateway_absent", "gateway_zero_progress"]
)
async def test_entire_zero_aux_payload_is_required_from_gateway_and_disk(tmp_path, changed):
    raw, _ = multifile(
        [{b"length": 0, b"path": [b"aux.nfo"]}, {b"length": 123, b"path": [b"video.mkv"]}]
    )
    metadata = inspect_torrent(raw)
    job = fixture_job(metadata)
    permit = Permit(
        job["permit_id"],
        "fixture-token",
        metadata.infohash,
        "/data/torrents",
        datetime.now(UTC) + timedelta(hours=1),
        category="radarr",
        reservation_id=job["reservation_id"],
        metadata_sha256=metadata.metadata_sha256,
        selected_files=tuple(file.path for file in metadata.files),
        budget_bytes=123,
        state="confirmed",
    )
    artifacts = TorrentArtifactStore(tmp_path / "control.sqlite")
    artifacts.put(permit, raw)
    root = tmp_path / "torrents"
    (root / "fixture").mkdir(parents=True)
    for file in metadata.files:
        if changed != "physical_absent" or file.length != 0:
            (root / file.path).write_bytes(b"x" * file.length)

    def handler(request):
        if request.url.path.endswith("/info"):
            return httpx.Response(
                200,
                json=[
                    {
                        "hash": permit.infohash,
                        "category": "radarr",
                        "save_path": "/data/torrents",
                        "content_path": "/data/torrents/fixture",
                        "progress": 1,
                        "amount_left": 0,
                    }
                ],
            )
        return httpx.Response(
            200,
            json=[
                {
                    "name": file.path,
                    "size": file.length,
                    "progress": 0 if changed == "gateway_zero_progress" and file.length == 0 else 1,
                }
                for file in metadata.files
                if changed != "gateway_absent" or file.length != 0
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        helper = SimpleNamespace(
            torrent_root=root,
            client=client,
            _source_path=lambda _permit, path: root / path,
            _content_path=lambda _permit, _raw: root / "fixture",
            _physical=lambda path, _permit: path,
        )
        permits = SimpleNamespace(
            get_for_reservation=lambda *_a, **_kw: permit,
            probe_reservation_active=lambda _p: True,
            placement_valid=lambda _p, **_kw: True,
        )
        worker = AuditedManifestImporter(
            store=None,
            manifest_loader=lambda: {},
            permits=permits,
            torrent_store=artifacts,
            helpers={},
            gateway_url="http://fixture.invalid",
            arr_token="fixture",
        )
        assert worker._permit(job) is permit
        assert "fixture/aux.nfo" in permit.selected_files
        if changed is None:
            await worker._download(helper, permit, job)
        else:
            with pytest.raises(ValidationError):
                await worker._download(helper, permit, job)
        permit = replace(permit, selected_files=("fixture/video.mkv",))
        with pytest.raises(ValidationError):
            worker._permit(job)
