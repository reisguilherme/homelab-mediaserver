from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Any

import httpx

from homeserver_control.domain.magnet import magnet_infohash
from homeserver_control.domain.torrent_bytes import TorrentBytesError, _Decoder, inspect_torrent

from .http import ContractError, CredentialError, EffectUncertain, UpstreamError, endpoint


class QBittorrentAdapter:
    """Fixed-subset qBittorrent client used behind the admission gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        client: httpx.Client | None = None,
        metadata_provider: Callable[[str], bytes | None] | None = None,
    ) -> None:
        if not username or not password:
            raise ValueError("qBittorrent credentials are required")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = client or httpx.Client(timeout=httpx.Timeout(15.0))
        self._logged_in = False
        self.metadata_provider = metadata_provider
        self._identities: dict[str, tuple[str, str | None]] = {}
        # Immutable identity knowledge is never authority. It only prevents a
        # known hybrid reverting to a blind v1 request when fresh proof is lost.
        self._known_hybrids: dict[str, str] = {}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, endpoint(self.base_url, path), **kwargs)
        except httpx.TimeoutException as error:
            raise EffectUncertain("qBittorrent request timed out") from error
        except httpx.HTTPError as error:
            raise UpstreamError("qBittorrent request failed") from error
        if response.status_code in (401, 403):
            raise CredentialError("qBittorrent credential rejected")
        if response.status_code >= 400:
            raise UpstreamError(f"qBittorrent returned HTTP {response.status_code}")
        return response

    def _login(self) -> None:
        response = self._request(
            "POST",
            "/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
        )
        if response.text.strip().lower() not in {"ok.", "ok"}:
            raise ContractError("qBittorrent login response is incompatible")
        self._logged_in = True

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self._login()

    def _metadata_identity(self, content: bytes, *, opaque_legacy=False):
        digest = sha256(content).hexdigest()
        if digest in self._identities:
            return self._identities[digest]
        try:
            inspected = inspect_torrent(content)
        except TorrentBytesError as error:
            if opaque_legacy:
                # The original low-level adapter accepts bytes already verified by
                # its gateway. Preserve opaque legacy clients, never derive aliases.
                return None
            raise ContractError("sealed qBittorrent metadata is invalid") from error
        v2 = None
        if inspected.hybrid:
            # Inspection already validated the complete root. Slice the original
            # info bytes rather than re-encoding; tracker fields never affect IDs.
            decoder = _Decoder(content)
            decoder.offset = 1
            while content[decoder.offset : decoder.offset + 1] != b"e":
                key = decoder.parse(1)
                start = decoder.offset
                decoder.parse(1)
                if key == b"info":
                    v2 = sha256(content[start : decoder.offset]).hexdigest()
                    break
        identity = (inspected.infohash, v2)
        if v2 is not None:
            previous = self._known_hybrids.get(inspected.infohash)
            if previous is not None and previous != v2:
                raise ContractError("sealed qBittorrent hybrid identity changed")
            self._known_hybrids[inspected.infohash] = v2
        if len(self._identities) >= 1024:
            self._identities.clear()
        self._identities[digest] = identity
        return identity

    def _binding(self, infohash):
        if self.metadata_provider is None:
            if infohash in self._known_hybrids:
                raise ContractError("sealed qBittorrent identity proof unavailable")
            return None
        try:
            content = self.metadata_provider(infohash)
        except (OSError, ValueError) as error:
            raise ContractError("sealed qBittorrent identity proof unavailable") from error
        if content is None:
            if infohash in self._known_hybrids:
                raise ContractError("sealed qBittorrent identity proof unavailable")
            return None
        identity = self._metadata_identity(content)
        if identity[0] != infohash:
            raise ContractError("sealed qBittorrent v1 identity changed")
        if infohash in self._known_hybrids and identity[1] != self._known_hybrids[infohash]:
            raise ContractError("sealed qBittorrent hybrid identity changed")
        return identity

    @staticmethod
    def _canonical_row(row, identity):
        v1, v2 = identity
        native = row.get("hash")
        reported_v1, reported_v2 = row.get("infohash_v1"), row.get("infohash_v2")
        if native == v1 and reported_v2 in (None, ""):
            if reported_v1 not in (None, "", v1):
                raise ContractError("qBittorrent v1 identity changed")
        elif v2 is None or native != v2[:40] or reported_v1 != v1 or reported_v2 != v2:
            raise ContractError("qBittorrent hybrid identity changed")
        return dict(row, hash=v1)

    def _canonical_rows(self, rows, expected=None):
        result = []
        seen = set()
        native_seen = set()
        expected = expected or {}
        by_native = {
            identity[1][:40] if identity[1] else v1: identity for v1, identity in expected.items()
        }
        if len(by_native) != len(expected):
            raise ContractError("ambiguous qBittorrent native identity")
        for item in rows:
            row = dict(item)
            native = row.get("hash")
            if isinstance(native, str):
                if native in native_seen:
                    raise ContractError("ambiguous qBittorrent native identity")
                native_seen.add(native)
            v1 = row.get("infohash_v1")
            identity = by_native.get(native) or expected.get(v1)
            if (
                identity is None
                and isinstance(v1, str)
                and re.fullmatch(r"[0-9a-f]{40}", v1)
                and native != v1
            ):
                identity = self._binding(v1)
            if identity is not None:
                row = self._canonical_row(row, identity)
            # Preserve native unapproved rows, but never collapse two bindings.
            key = row.get("hash")
            if key in seen:
                raise ContractError("ambiguous qBittorrent torrent identity")
            seen.add(key)
            result.append(row)
        return result

    def _find(self, infohash, identity=None, *, timeout=None):
        self._ensure_login()
        native = identity[1][:40] if identity is not None and identity[1] else infohash
        options = {"timeout": timeout} if timeout is not None else {}
        response = self._request(
            "GET", "/api/v2/torrents/info", params={"hashes": native}, **options
        )
        try:
            rows = response.json()
        except ValueError as error:
            raise ContractError("qBittorrent info response is not JSON") from error
        if not isinstance(rows, list) or any(not isinstance(item, Mapping) for item in rows):
            raise ContractError("qBittorrent info response is incompatible")
        if identity is not None:
            normalized = [self._canonical_row(item, identity) for item in rows]
            if len(normalized) > 1:
                raise ContractError("ambiguous qBittorrent torrent identity")
        else:
            normalized = self._canonical_rows(rows)
        return [dict(item) for item in rows], normalized

    def _native_id(self, infohash):
        identity = self._binding(infohash)
        if identity is None or identity[1] is None:
            return infohash
        raw, _ = self._find(infohash, identity)
        if len(raw) != 1:
            raise ContractError("verified qBittorrent alias is unavailable")
        return raw[0]["hash"]

    def add_torrent(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_login()
        infohash = payload.get("infohash")
        destination = payload.get("savepath")
        if not isinstance(infohash, str) or not isinstance(destination, str):
            raise ContractError("qBittorrent payload identity is incomplete")
        if not destination.startswith("/data/"):
            raise ContractError("qBittorrent destination is outside /data")
        torrent_bytes = payload.get("torrent_bytes")
        magnet = payload.get("magnet_url")
        if isinstance(torrent_bytes, bytes) and torrent_bytes and magnet is None:
            files = {"torrents": ("approved.torrent", torrent_bytes, "application/x-bittorrent")}
        elif torrent_bytes is None and magnet_infohash(magnet) == infohash.lower():
            files = {"urls": (None, magnet)}
        else:
            raise ContractError("verified torrent bytes or permitted magnet are required")
        identity = None
        if isinstance(torrent_bytes, bytes):
            identity = self._metadata_identity(torrent_bytes, opaque_legacy=True)
            if identity is not None and identity[0] != infohash.lower():
                raise ContractError("qBittorrent metadata v1 identity differs from payload")
        else:
            identity = self._binding(infohash.lower())
        category = payload.get("category")
        if not isinstance(category, str) or category not in {"sonarr", "radarr"}:
            raise ContractError("unsupported torrent category")
        response = self._request(
            "POST",
            "/api/v2/torrents/add",
            data={
                "savepath": destination,
                "category": category,
                "stopped": "false",
                "autoTMM": "false",
                "useDownloadPath": "false",
                "downloadPath": destination,
            },
            files=files,
        )
        text = response.text.strip()
        if text.lower() not in {"ok.", "ok", ""}:
            try:
                result = response.json()
            except ValueError as error:
                raise ContractError("qBittorrent add response is incompatible") from error
            if not isinstance(result, dict) or result.get("success_count", 0) < 1:
                raise ContractError("qBittorrent did not accept the torrent")
        # An accepted add can precede the torrent's appearance in the info API.
        deadline = time.monotonic() + 1.0
        for attempt in range(5):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            matches = [
                item
                for item in self._find(infohash.lower(), identity, timeout=remaining)[1]
                if isinstance(item.get("hash"), str)
                and item["hash"].lower() == infohash.lower()
                and item.get("category") == category
                and isinstance(item.get("save_path"), str)
                and item["save_path"].rstrip("/") == destination.rstrip("/")
            ]
            if len(matches) == 1:
                return {"accepted": True, "infohash": infohash.lower()}
            if attempt < 4:
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        raise EffectUncertain("qBittorrent accepted but torrent was not visible")

    def read(self, path: str, params: dict[str, str] | None = None) -> object:
        allowed = {
            "/api/v2/app/webapiVersion",
            "/api/v2/app/version",
            "/api/v2/app/preferences",
            "/api/v2/torrents/categories",
            "/api/v2/torrents/info",
            "/api/v2/torrents/properties",
            "/api/v2/torrents/files",
        }
        if path not in allowed:
            raise ContractError("qBittorrent path is not allowlisted")
        self._ensure_login()
        params = dict(params) if params is not None else None
        expected = {}
        if path == "/api/v2/torrents/info" and params and "hashes" in params:
            values = params["hashes"].split("|")
            if any(not re.fullmatch(r"[0-9a-fA-F]{40}", value) for value in values):
                raise ContractError("invalid canonical qBittorrent identity")
            if len(set(value.lower() for value in values)) != len(values):
                raise ContractError("ambiguous canonical qBittorrent identity")
            native_ids = []
            for value in values:
                canonical = value.lower()
                proof = self._binding(canonical)
                if proof is not None:
                    expected[canonical] = proof
                native_ids.append(proof[1][:40] if proof is not None and proof[1] else canonical)
            if len(set(native_ids)) != len(native_ids):
                raise ContractError("ambiguous qBittorrent native identity")
            params["hashes"] = "|".join(native_ids)
        identity = None
        if path in {"/api/v2/torrents/files", "/api/v2/torrents/properties"} and params:
            value = params.get("hash")
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", value):
                raise ContractError("invalid canonical qBittorrent identity")
            identity = self._binding(value.lower())
            if identity is not None and identity[1] is not None:
                raw, _ = self._find(value.lower(), identity)
                if len(raw) != 1:
                    raise ContractError("verified qBittorrent alias is unavailable")
                params["hash"] = raw[0]["hash"]
        response = self._request("GET", path, params=params)
        if path in {"/api/v2/app/webapiVersion", "/api/v2/app/version"}:
            return response.text.strip()
        try:
            result = response.json()
            if path == "/api/v2/torrents/info":
                if not isinstance(result, list) or any(
                    not isinstance(row, Mapping) for row in result
                ):
                    raise ContractError("qBittorrent info response is incompatible")
                return self._canonical_rows(result, expected)
            if path == "/api/v2/torrents/properties" and identity and identity[1]:
                if not isinstance(result, Mapping):
                    raise ContractError("qBittorrent properties response is incompatible")
                return self._canonical_row(result, identity)
            return result
        except ValueError as error:
            raise ContractError("qBittorrent JSON response is incompatible") from error

    def find_by_infohash(
        self, infohash: str, *, timeout: float | None = None
    ) -> list[dict[str, Any]]:
        canonical = infohash.lower()
        return self._find(canonical, self._binding(canonical), timeout=timeout)[1]

    def set_running(self, infohash: str, *, running: bool) -> None:
        if not isinstance(infohash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
            raise ValueError("invalid infohash")
        if not isinstance(running, bool):
            raise ValueError("invalid torrent state")
        self._ensure_login()
        action = "start" if running else "stop"
        response = self._request(
            "POST",
            f"/api/v2/torrents/{action}",
            data={"hashes": self._native_id(infohash.lower())},
        )
        if response.text.strip().lower() not in {"", "ok", "ok."}:
            raise ContractError("qBittorrent queue response is incompatible")

    def top_priority(self, infohash: str) -> None:
        if not isinstance(infohash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
            raise ValueError("invalid infohash")
        self._ensure_login()
        response = self._request(
            "POST",
            "/api/v2/torrents/topPrio",
            data={"hashes": self._native_id(infohash.lower())},
        )
        if response.text.strip().lower() not in {"", "ok", "ok."}:
            raise ContractError("qBittorrent queue response is incompatible")

    def delete_torrent(self, infohash: str, *, delete_files: bool) -> None:
        """Delete one exact source; caller must verify ownership and manifest first."""
        if not isinstance(infohash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
            raise ValueError("invalid infohash")
        if delete_files is not True:
            raise ValueError("delete_files must be true")
        self._ensure_login()
        response = self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": self._native_id(infohash.lower()), "deleteFiles": "true"},
        )
        if response.text.strip().lower() not in {"", "ok", "ok."}:
            raise ContractError("qBittorrent delete response is incompatible")
