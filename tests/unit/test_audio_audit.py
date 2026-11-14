import importlib
import importlib.util
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import Permit
from homeserver_control.persistence.db import ReservationRepository


def _module():
    name = "homeserver_control.worker.audio_audit"
    assert importlib.util.find_spec(name), "downloaded audio is not audited"
    return importlib.import_module(name)


@pytest.fixture
def repository(tmp_path):
    result = ReservationRepository(tmp_path / "control.sqlite")
    result.initialize()
    return result


@pytest.fixture
def permit():
    return Permit(
        permit_id="fixture-permit", token="never-persist-this-token", infohash="a" * 40,
        destination="/data/torrents", expires_at=datetime.now(UTC),
        selected_files=("private-title.mkv",), reservation_id="fixture-reservation",
    )


def _select(repository, permit, languages=("pt-BR", "en"), *, original="en"):
    release = {
        "title": "Fixture",
        "audioEvidence": [{"source": "description", "languages": languages}],
        "downloadUrl": "https://private.invalid?apikey=never-persist-key",
    }
    return _module().record_audio_selection(
        repository, permit, release, original_language=original,
    )


def _probe(*streams):
    return MediaProbe(
        1920, 1080,
        tuple(stream.get("tags", {}).get("language", "und") for stream in streams), (),
        {"streams": [{"codec_type": "audio", **stream} for stream in streams]},
    )


def _track(language, title="", disposition=None):
    return {"tags": {"language": language, "title": title}, "disposition": disposition or {}}


def test_dual_selection_requires_brazilian_and_original_real_audio(repository, permit):
    _select(repository, permit)
    audit = _module().audit_download_audio(
        repository, permit, _probe(_track("por", "Português Brasileiro"), _track("eng")),
    )
    assert audit["outcome"] == "matched"
    assert audit["actual_languages"] == ["en", "pt-br"]
    assert audit["expected_tier"] == "dual"


@pytest.mark.parametrize("track", [
    _track("por"), _track("und"), {"tags": {}},
])
def test_unknown_portuguese_region_or_language_is_inconclusive(repository, permit, track):
    _select(repository, permit)
    audit = _module().audit_download_audio(repository, permit, _probe(track, _track("eng")))
    assert audit["outcome"] == "inconclusive"


@pytest.mark.parametrize("title,disposition", [
    ("Director Commentary", {}), ("Comentários do diretor", {}), ("", {"comment": 1}),
])
def test_commentary_is_not_the_original_audio(repository, permit, title, disposition):
    _select(repository, permit)
    audit = _module().audit_download_audio(
        repository, permit,
        _probe(_track("pt-br"), _track("eng", title, disposition)),
    )
    assert audit["outcome"] == "mismatch"
    assert audit["actual_languages"] == ["pt-br"]
    assert audit["ignored_commentary_tracks"] == 1


def test_original_fallback_is_valid_without_ptbr(repository, permit):
    _select(repository, permit, ("jpn",), original="ja")
    audit = _module().audit_download_audio(repository, permit, _probe(_track("jpn")))
    assert audit["outcome"] == "matched"
    assert audit["expected_tier"] == "original"


def test_japanese_original_cannot_be_satisfied_by_english_dub(repository, permit):
    _select(repository, permit, ("pt-br", "ja"), original="ja")
    audit = _module().audit_download_audio(
        repository, permit, _probe(_track("pt-br"), _track("eng")),
    )
    assert audit["outcome"] == "mismatch"


def test_european_portuguese_is_not_brazilian(repository, permit):
    _select(repository, permit, ("pt-br",))
    audit = _module().audit_download_audio(
        repository, permit, _probe(_track("pt-PT"), _track("eng")),
    )
    assert audit["outcome"] == "mismatch"


def test_pack_children_inherit_selection_but_get_independent_audits(repository, permit):
    _select(repository, permit)
    first = replace(
        permit, permit_id="episode-one", scope_key="S01E01",
        season_pack_parent_id=permit.permit_id,
    )
    second = replace(first, permit_id="episode-two", scope_key="S01E02")
    good = _module().audit_download_audio(
        repository, first, _probe(_track("pt-br"), _track("eng")),
    )
    bad = _module().audit_download_audio(repository, second, _probe(_track("eng")))
    assert good["outcome"] == "matched"
    assert bad["outcome"] == "mismatch"
    assert good["operation_id"] != bad["operation_id"]
    assert good["scope_key"] == "S01E01"
    assert bad["scope_key"] == "S01E02"


def test_audio_audit_is_persisted_once_without_raw_inventory_or_secrets(
    repository, permit, caplog,
):
    _select(repository, permit)
    with caplog.at_level(logging.WARNING):
        first = _module().audit_download_audio(repository, permit, _probe(_track("eng")))
        repeated = _module().audit_download_audio(repository, permit, _probe(_track("eng")))
    assert repeated == first
    reopened = ReservationRepository(repository.path)
    assert reopened.get_operation(first["operation_id"]) == first
    payload = json.dumps(first)
    assert "never-persist" not in payload
    assert "private-title" not in payload
    assert "/data/" not in payload
    assert len(caplog.records) == 1
    assert "mismatch" in caplog.records[0].message


def test_legacy_permit_without_selection_does_not_guess_profile(repository, permit):
    assert _module().audit_download_audio(repository, permit, _probe(_track("eng"))) is None


def test_selection_is_dubbed_only_and_respects_configured_preference_order(repository, permit):
    module = _module()
    release = {"languages": ["pt-br", "en"]}
    assert module.record_audio_selection(
        repository, permit, release, original_language="en", profile_name="Standard",
    ) is None
    selection = module.record_audio_selection(
        repository, permit, release, original_language="en",
        preferences=("original", "dual", "pt-BR"),
    )
    assert selection["expected_tier"] == "original"
