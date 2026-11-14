"""Prove canonical series identity from native IDs and current Seerr requests."""

from __future__ import annotations

import re
from copy import deepcopy

_SEASON_KEY = re.compile(r"season:tmdb:([1-9][0-9]*):[0-9]+")


def _positive_id(value: object) -> bool:
    return type(value) is int and value > 0


class SeriesIdentityResolver:
    """Keep only unambiguous bindings from the current authenticated request list."""

    def __init__(self) -> None:
        self._bindings: dict[int, dict[str, int]] = {}
        self._invalid: set[int] = set()
        self._titles: dict[int, str] = {}

    def update_requests(self, requests: list[dict]) -> None:
        bindings: dict[int, dict[str, int]] = {}
        invalid: set[int] = set()
        titles: dict[int, set[str]] = {}
        invalid_titles: set[int] = set()
        for request in requests:
            if not isinstance(request, dict):
                continue
            key = request.get("media_key")
            match = _SEASON_KEY.fullmatch(key) if isinstance(key, str) else None
            if match is None or request.get("kind") != "season":
                continue
            tmdb_id = int(match.group(1))
            if "title" in request:
                title = request["title"]
                if not isinstance(title, str) or not title.strip():
                    invalid_titles.add(tmdb_id)
                else:
                    titles.setdefault(tmdb_id, set()).add(title.strip())
            binding = bindings.setdefault(tmdb_id, {})
            for field in ("tvdb_id", "external_service_id"):
                if field not in request:
                    continue
                value = request[field]
                if not _positive_id(value):
                    invalid.add(tmdb_id)
                elif field in binding and binding[field] != value:
                    invalid.add(tmdb_id)
                else:
                    binding[field] = value
        for field in ("tvdb_id", "external_service_id"):
            owners: dict[int, set[int]] = {}
            for tmdb_id, binding in bindings.items():
                if field in binding:
                    owners.setdefault(binding[field], set()).add(tmdb_id)
            for ids in owners.values():
                if len(ids) > 1:
                    invalid.update(ids)
        # Replace the snapshot so withdrawn or edited requests cannot leave stale proof.
        self._bindings = {key: value for key, value in bindings.items() if value}
        self._invalid = invalid
        self._titles = {
            key: next(iter(names)) for key, names in titles.items()
            if len(names) == 1 and key not in invalid_titles
        }

    def subtitle_titles(self, catalog: list[dict], tmdb_id: int) -> tuple[str, ...]:
        """Expose current Seerr names only after the native identity is proved."""
        if self.resolve(catalog, tmdb_id) is None:
            return ()
        title = self._titles.get(tmdb_id)
        return (title,) if title is not None else ()

    def resolve(self, catalog: list[dict], tmdb_id: int) -> dict | None:
        if not _positive_id(tmdb_id) or tmdb_id in self._invalid:
            return None
        binding = self._bindings.get(tmdb_id, {})
        candidates = []
        fields = {"tvdb_id": "tvdbId", "external_service_id": "id"}
        for row in catalog:
            if not isinstance(row, dict):
                continue
            direct = _positive_id(row.get("tmdbId")) and row["tmdbId"] == tmdb_id
            bound = any(
                _positive_id(row.get(fields[field])) and row[fields[field]] == value
                for field, value in binding.items()
            )
            if direct or bound:
                candidates.append(row)
        if len(candidates) != 1:
            return None
        selected = candidates[0]
        if not _positive_id(selected.get("id")):
            return None
        for field in ("id", "tvdbId"):
            value = selected.get(field)
            if _positive_id(value) and sum(
                isinstance(row, dict) and _positive_id(row.get(field)) and row[field] == value
                for row in catalog
            ) != 1:
                return None
        if any(
            not _positive_id(selected.get(fields[field])) or selected[fields[field]] != value
            for field, value in binding.items()
        ):
            return None
        native_tmdb = selected.get("tmdbId")
        if _positive_id(native_tmdb):
            if native_tmdb != tmdb_id:
                return None
        elif (
            "tvdb_id" not in binding
            or (native_tmdb is not None and (type(native_tmdb) is not int or native_tmdb != 0))
        ):
            return None
        resolved = deepcopy(selected)
        resolved["tmdbId"] = tmdb_id
        return resolved
