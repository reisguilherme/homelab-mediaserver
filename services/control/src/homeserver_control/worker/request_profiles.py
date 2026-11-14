"""Apply each explicit Seerr profile choice once before native acquisition."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from homeserver_common.download_profiles import PROFILE_NAMES
from homeserver_control.adapters.http import ContractError, endpoint, request_json
from homeserver_control.configuration.quality_profiles import profile_readback_matches

from .series_identity import SeriesIdentityResolver

LOGGER = logging.getLogger(__name__)


class _AuditNativeProfileConflict(ContractError):
    """An explicit request would change an audited series' native profile."""


class RequestProfileSynchronizer:
    def __init__(
        self, *, database: str | Path, source, services: dict[str, tuple[str, str]],
        series_identity: SeriesIdentityResolver | None = None,
    ):
        self.database = str(database)
        self.source = source
        self.services = services
        self.series_identity = series_identity or SeriesIdentityResolver()
        self.blocked_reasons: dict[str, str] = {}
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS request_profile_choices (
                media_key TEXT PRIMARY KEY, request_id INTEGER NOT NULL,
                profile_id INTEGER, server_id INTEGER, state TEXT NOT NULL,
                native_id INTEGER, previous_profile_id INTEGER)""")

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _identity(raw):
        parts = str(raw.get("media_key", "")).split(":")
        if (
            len(parts) not in (3, 4)
            or parts[1] != "tmdb"
            or parts[0] not in ("movie", "season")
            or not parts[2].isdecimal()
            or int(parts[2]) <= 0
        ):
            raise ContractError("Request profile media identity unsupported")
        service = "radarr" if parts[0] == "movie" else "sonarr"
        return f"{service}:tmdb:{parts[2]}", service, int(parts[2])

    @staticmethod
    def _request_id(raw):
        identifier = str(raw.get("source_id", "")).split(":")[0]
        if not identifier.isdecimal() or int(identifier) <= 0:
            raise ContractError("Request profile request identity unsupported")
        return int(identifier)

    def _claim(self, media_key, request_id, profile_id, server_id):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM request_profile_choices WHERE media_key=?", (media_key,)
            ).fetchone()
            same_choice = current and (
                current["request_id"] == request_id
                and current["profile_id"] == profile_id
                and current["server_id"] == server_id
            )
            if current and (
                current["request_id"] > request_id
                or (same_choice and current["state"] == "applied")
            ):
                connection.commit()
                return None
            if not same_choice:
                connection.execute(
                    "INSERT INTO request_profile_choices "
                    "VALUES (?, ?, ?, ?, 'pending', NULL, NULL) "
                    "ON CONFLICT(media_key) DO UPDATE SET request_id=excluded.request_id, "
                    "profile_id=excluded.profile_id, server_id=excluded.server_id, "
                    "state='pending', "
                    "native_id=NULL, previous_profile_id=NULL",
                    (media_key, request_id, profile_id, server_id),
                )
            row = connection.execute(
                "SELECT * FROM request_profile_choices WHERE media_key=?", (media_key,)
            ).fetchone()
            connection.commit()
            return dict(row)

    def _remember_native(self, row, media):
        with self._connect() as connection:
            connection.execute(
                "UPDATE request_profile_choices SET native_id=?, previous_profile_id=? "
                "WHERE media_key=? AND request_id=? AND state='pending' AND native_id IS NULL",
                (media["id"], media["qualityProfileId"], row["media_key"], row["request_id"]),
            )

    def _complete(self, row):
        with self._connect() as connection:
            connection.execute(
                "UPDATE request_profile_choices SET state='applied' WHERE media_key=? "
                "AND request_id=?",
                (row["media_key"], row["request_id"]),
            )

    async def reconcile(
        self,
        requests: list[dict],
        *,
        preserve_native_series: frozenset[int] = frozenset(),
    ) -> set[str]:
        self.series_identity.update_requests(requests)
        grouped = {}
        blocked = set()
        self.blocked_reasons = {}
        for raw in requests:
            try:
                identity, _, _ = self._identity(raw)
            except ContractError:
                if "profile_id" in raw:
                    blocked.add(raw["media_key"])
                continue
            grouped.setdefault(identity, []).append(raw)
        for media_key, rows in grouped.items():
            _, service, tmdb_id = self._identity(rows[0])
            preserve_native = service == "sonarr" and tmdb_id in preserve_native_series
            if not preserve_native and not any("profile_id" in row for row in rows):
                continue  # Compatibility with sources that do not expose choices.
            try:
                latest_id = max(self._request_id(row) for row in rows)
                latest = [row for row in rows if self._request_id(row) == latest_id]
                choice = latest[0]
                selection = (
                    choice.get("profile_id"),
                    choice.get("server_id"),
                    choice.get("is_4k", False),
                )
                if any(
                    (row.get("profile_id"), row.get("server_id"), row.get("is_4k", False))
                    != selection
                    for row in latest
                ):
                    raise ContractError("Conflicting profile selections for one request")
                profile_id, server_id, is_4k = selection
                if profile_id is not None and (
                    type(profile_id) is not int
                    or profile_id <= 0
                    or type(server_id) is not int
                    or server_id < 0
                    or type(is_4k) is not bool
                ):
                    raise ContractError("Request profile selection is incomplete")
                if preserve_native:
                    # Recheck every cycle, even when this choice was already applied.
                    # A read-only check must never consume the one-time mutation record.
                    await self._apply(
                        service,
                        tmdb_id,
                        {"profile_id": profile_id, "server_id": server_id, "native_id": None},
                        is_4k,
                        preserve_native=True,
                    )
                    continue
                row = self._claim(media_key, latest_id, profile_id, server_id)
                if row is None:
                    continue
                if profile_id is not None:
                    _, service, tmdb_id = self._identity(choice)
                    await self._apply(service, tmdb_id, row, is_4k)
                self._complete(row)
            except Exception as error:
                # Never log upstream response bodies, URLs or credential fields.
                LOGGER.error("request profile synchronization deferred (%s)", type(error).__name__)
                blocked.update(row["media_key"] for row in rows)
                reason = (
                    "audit_native_profile_conflict"
                    if isinstance(error, _AuditNativeProfileConflict)
                    else "request_profile_unverified"
                )
                self.blocked_reasons.update({row["media_key"]: reason for row in rows})
        return blocked

    @staticmethod
    def _validate_profile(profiles, profile_id):
        selected = [profile for profile in profiles if profile["id"] == profile_id]
        if (
            len(selected) != 1
            or selected[0]["name"] not in PROFILE_NAMES
            or sum(profile["name"] == selected[0]["name"] for profile in profiles) != 1
        ):
            raise ContractError("Selected native profile is unavailable or ambiguous")

    async def _apply(self, service, tmdb_id, choice, is_4k, *, preserve_native=False):
        if service not in self.services:
            raise ContractError("Selected Arr service is not configured")
        url, api_key = self.services[service]
        if choice["profile_id"] is not None:
            parts = urlsplit(url)
            servers = await self.source.list_arr_servers(service)
            matches = [
                server
                for server in servers
                if type(server.get("id")) is int and server["id"] == choice["server_id"]
            ]
            if len(matches) != 1:
                raise ContractError("Selected Seerr server is unavailable")
            server = matches[0]
            connection = (
                server.get("hostname"), server.get("port"), server.get("useSsl", False),
                server.get("baseUrl", "").rstrip("/"),
            )
            configured = (
                parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                parts.scheme == "https", parts.path.rstrip("/"),
            )
            # The fixed proxy identifies the same native service; arbitrary aliases
            # and cross-service paths must still fail before a profile is changed.
            integration = ("integration-proxy", 8787, False, f"/{service}")
            if (
                type(server.get("useSsl", False)) is not bool
                or connection not in (configured, integration)
                or server.get("is4k", False) is not is_4k
            ):
                raise ContractError("Selected Seerr server does not match configured Arr")
        headers = {"X-Api-Key": api_key}

        async def get(path):
            return await request_json(
                self.source.client, "GET", endpoint(url, path), headers=headers
            )

        profiles = await get("/api/v3/qualityprofile")
        if not isinstance(profiles, list) or any(
            not isinstance(profile, dict)
            or type(profile.get("id")) is not int
            or not isinstance(profile.get("name"), str)
            for profile in profiles
        ):
            raise ContractError("Native profiles are invalid")
        if choice["profile_id"] is not None:
            self._validate_profile(profiles, choice["profile_id"])
        resource = "/api/v3/movie" if service == "radarr" else "/api/v3/series"
        catalog = await get(resource)
        if not isinstance(catalog, list):
            raise ContractError("Native catalog is invalid")
        if service == "sonarr":
            resolved = self.series_identity.resolve(catalog, tmdb_id)
            matches = [resolved] if resolved is not None else []
        else:
            matches = [
                media for media in catalog
                if isinstance(media, dict) and media.get("tmdbId") == tmdb_id
            ]
        if len(matches) != 1 or type(matches[0].get("id")) is not int:
            raise ContractError("Requested native title is unavailable or ambiguous")
        native_id = matches[0]["id"]
        if choice["native_id"] is not None and native_id != choice["native_id"]:
            raise ContractError("Native title identity changed during profile synchronization")
        path = resource + f"/{native_id}"
        media = await get(path)
        verified = (
            self.series_identity.resolve([media], tmdb_id)
            if service == "sonarr" and isinstance(media, dict)
            else media
        )
        if (
            not isinstance(verified, dict)
            or verified.get("id") != native_id
            or verified.get("tmdbId") != tmdb_id
            or type(media.get("qualityProfileId")) is not int
        ):
            raise ContractError("Native title profile is invalid")
        if preserve_native:
            self._validate_profile(profiles, media["qualityProfileId"])
            if choice["profile_id"] not in (None, media["qualityProfileId"]):
                raise _AuditNativeProfileConflict("Audited series native profile is preserved")
            return
        if media["qualityProfileId"] == choice["profile_id"]:
            return
        if (
            choice["previous_profile_id"] is not None
            and media["qualityProfileId"] != choice["previous_profile_id"]
        ):
            # An operator changed the native profile after an interrupted attempt.
            # Consume the old request instead of overwriting that newer decision.
            return
        self._remember_native(choice, media)
        payload = media | {"qualityProfileId": choice["profile_id"]}
        try:
            response = await self.source.client.put(
                endpoint(url, path), headers=headers, json=payload
            )
            response.raise_for_status()
        except httpx.TransportError:
            pass  # The following GET decides whether the mutation was accepted.
        observed = await get(path)
        if (
            (service == "sonarr" and self.series_identity.resolve([observed], tmdb_id) is None)
            or not profile_readback_matches(service, observed, payload)
        ):
            raise ContractError("Native request profile read-back drift")
