"""Persist non-blocking soundtrack diagnostics for downloads selected as Dublado.

This module only records metadata. It cannot delete media, replace torrents,
modify playback tracks or start downloads. Missing/ambiguous tags are reported
as inconclusive, never as proof that a spoken language is absent.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from collections.abc import Mapping

from homeserver_control.domain.media_probe import MediaProbe
from homeserver_control.gateway.permits import Permit
from homeserver_control.persistence.db import ReservationRepository

from .audio_evidence import declared_language, preferred_audio_tier, release_audio_languages

_LOG = logging.getLogger(__name__)
_COMMENTARY = re.compile(r"\b(?:commentary|coment[aá]rios?)\b", re.I)


def _operation_id(kind: str, permit_id: str, infohash: str) -> str:
    identity = hashlib.sha256(f"{permit_id}:{infohash.lower()}".encode()).hexdigest()
    return f"{kind}:{identity}"


def record_audio_selection(
    repository: ReservationRepository, permit: Permit, release: Mapping[str, object],
    *, original_language: object = None, profile_name: str = "Dublado",
    preferences: tuple[str, ...] = ("dual", "pt-BR", "original"),
) -> dict[str, object] | None:
    """Snapshot only normalized claims after dispatch; never persist URLs/tokens."""
    if profile_name != "Dublado":
        return None
    languages = release_audio_languages(release)
    payload = {
        "profile": profile_name,
        "permit_id": permit.permit_id,
        "infohash": permit.infohash.lower(),
        "original_language": declared_language(original_language),
        "expected_languages": sorted(languages),
        "expected_tier": preferred_audio_tier(languages, original_language, preferences),
    }
    identifier = _operation_id("audio_selection", permit.permit_id, permit.infohash)
    try:
        return repository.record_operation(
            operation_id=identifier, idempotency_key=identifier, kind="audio_selection",
            payload=payload, state="complete",
        )
    except sqlite3.Error:
        _LOG.warning("Could not persist audio selection for permit %s", permit.permit_id)
        return None


def _probe_languages(probe: MediaProbe) -> tuple[set[str], int, int]:
    languages: set[str] = set()
    unknown = ignored = 0
    streams = probe.raw.get("streams")
    if not isinstance(streams, list):
        return languages, max(len(probe.audio_languages), 1), ignored
    for stream in streams:
        if not isinstance(stream, Mapping) or stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags")
        tags = {str(key).lower(): value for key, value in tags.items()} if isinstance(
            tags, Mapping,
        ) else {}
        title = tags.get("title")
        title = title if isinstance(title, str) else ""
        disposition = stream.get("disposition")
        if _COMMENTARY.search(title) or (
            isinstance(disposition, Mapping) and disposition.get("comment") in {1}
        ):
            ignored += 1
            continue
        language = declared_language(tags.get("language"))
        named = declared_language(title)
        if language == "pt" and (
            named in {"pt-br", "pt-pt"} or re.search(r"\b(?:brazilian|brasileiro)\b", title, re.I)
        ):
            language = named if named in {"pt-br", "pt-pt"} else "pt-br"
        elif language is None and named is not None:
            language = named
        if language:
            languages.add(language)
        else:
            unknown += 1
    return languages, unknown, ignored


def _audit_outcome(
    languages: set[str], unknown: int, *, expected_tier: object, original_language: object,
) -> str:
    if expected_tier not in {"dual", "pt-BR", "original"}:
        return "inconclusive"
    if preferred_audio_tier(languages, original_language, (expected_tier,)):
        return "matched"
    original = declared_language(original_language)
    if unknown or (
        "pt" in languages and (
            expected_tier in {"dual", "pt-BR"} or original in {"pt-br", "pt-pt"}
        )
    ) or expected_tier in {"dual", "original"} and original is None:
        return "inconclusive"
    return "mismatch"


def audit_download_audio(
    repository: ReservationRepository, permit: Permit, probe: MediaProbe,
) -> dict[str, object] | None:
    """Audit a downloaded movie or pack child once, without changing import eligibility."""
    identifier = _operation_id("audio_audit", permit.permit_id, permit.infohash)
    try:
        if existing := repository.get_operation(identifier):
            return existing
        selection = repository.get_operation(
            _operation_id("audio_selection", permit.permit_id, permit.infohash),
        )
        if selection is None and permit.season_pack_parent_id is not None:
            selection = repository.get_operation(
                _operation_id("audio_selection", permit.season_pack_parent_id, permit.infohash),
            )
        if selection is None or selection.get("profile") != "Dublado":
            return None
        languages, unknown, ignored = _probe_languages(probe)
        expected_tier = selection.get("expected_tier")
        original = selection.get("original_language")
        outcome = _audit_outcome(
            languages, unknown, expected_tier=expected_tier, original_language=original,
        )
        payload = {
            "profile": "Dublado",
            "permit_id": permit.permit_id,
            "infohash": permit.infohash.lower(),
            "scope_key": permit.scope_key,
            "original_language": original,
            "expected_languages": selection.get("expected_languages", []),
            "expected_tier": expected_tier,
            "actual_languages": sorted(languages),
            "unknown_audio_tracks": unknown,
            "ignored_commentary_tracks": ignored,
            "outcome": outcome,
        }
        result = repository.record_operation(
            operation_id=identifier, idempotency_key=identifier, kind="audio_audit",
            payload=payload, state="complete",
        )
    except sqlite3.Error:
        _LOG.warning("Could not persist audio audit for permit %s", permit.permit_id)
        return None
    if outcome != "matched":
        _LOG.warning(
            "Dublado audio %s for permit %s: expected=%s, actual=%s, unknown_tracks=%s; "
            "manual review only", outcome, permit.permit_id, expected_tier,
            ",".join(sorted(languages)) or "unknown", unknown,
        )
    return result
