"""Fill missing native collection posters without unlocking owner metadata.

Jellyfin 10.10.7 creates automatic BoxSets locked and without a TMDb identity.
Its member movies retain TmdbCollection, which permits an exact identity lookup.
Native image download works on locked items and changes only the requested image.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from copy import deepcopy
from urllib.parse import quote, urlsplit

import httpx

from homeserver_common.jellyfin_auth import jellyfin_headers


def _positive_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value)
    if not text.isascii() or not text.isdecimal() or int(text) <= 0:
        return None
    return str(int(text))


def _provider_id(item: dict, key: str) -> str | None:
    providers = item.get("ProviderIds")
    if not isinstance(providers, dict):
        return None
    values = [value for name, value in providers.items() if name.casefold() == key.casefold()]
    return _positive_id(values[0]) if len(values) == 1 else None


def _has_primary(item: dict) -> bool:
    return bool(item.get("ImageTags", {}).get("Primary"))


def _official_poster(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc == "image.tmdb.org"
        and parts.path.startswith("/t/p/")
        and not parts.query
        and not parts.fragment
    )


def _validate_collection(item: dict, item_id: str) -> None:
    if (
        item.get("Id") != item_id or item.get("Type") != "BoxSet"
        or type(item.get("LockData")) is not bool
        or not isinstance(item.get("Name"), str) or not item["Name"]
    ):
        raise ValueError("Incomplete native collection identity or metadata")


class JellyfinCollectionReconciler:
    """Reconcile only absent Primary images; reports contain counts, never credentials."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        admin_username: str,
        interval_seconds: float = 3600,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.admin_username = admin_username
        self.interval_seconds = max(0, interval_seconds)
        self.clock = clock
        self._next_run = float("-inf")
        self.last_report: dict[str, str | int] = {"status": "pending"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        response = await self.client.request(
            method, self.base_url + path,
            headers=jellyfin_headers(self.api_key), **kwargs,
        )
        response.raise_for_status()
        return response

    async def _get(self, path: str, **kwargs):
        return (await self._request("GET", path, **kwargs)).json()

    async def _items(self, path: str, params: dict) -> list[dict]:
        rows = []
        seen = set()
        while True:
            page = await self._get(path, params=params | {"StartIndex": len(rows), "Limit": 1000})
            items = page.get("Items")
            total = page.get("TotalRecordCount")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError("Unsupported native item page")
            if total is not None and (type(total) is not int or total < 0):
                raise ValueError("Unsupported native item count")
            if not items:
                if total is not None and len(rows) < total:
                    raise ValueError("Incomplete native item page")
                return rows
            for item in items:
                item_id = item.get("Id")
                if not isinstance(item_id, str) or not item_id or item_id in seen:
                    raise ValueError("Ambiguous native item page")
                seen.add(item_id)
            rows.extend(items)
            if (total is not None and len(rows) >= total) or (
                total is None and len(items) < 1000
            ):
                return rows

    async def reconcile(self, *, force: bool = False) -> dict[str, str | int]:
        now = self.clock()
        if not force and now < self._next_run:
            return self.last_report | {"status": "waiting"}
        self._next_run = now + self.interval_seconds
        report: dict[str, str | int] = {
            "status": "completed", "collections": 0, "missing_primary": 0,
            "repaired": 0, "preserved": 0, "unresolved": 0, "failed": 0,
        }
        if not self.base_url or not self.api_key or not self.admin_username:
            report["status"] = "unconfigured"
            self.last_report = report
            return dict(report)
        try:
            users = await self._get("/Users")
            owners = [user for user in users if user.get("Name") == self.admin_username]
            if len(owners) != 1 or not owners[0].get("Id"):
                raise ValueError("Ambiguous native owner")
            owner_id = quote(owners[0]["Id"], safe="")
            items = await self._items("/Items", {"IncludeItemTypes": "BoxSet", "Recursive": "true"})
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            report["status"] = "unavailable"
            report["failed"] = 1
        else:
            report["collections"] = len(items)
            for item in items:
                try:
                    if _has_primary(item):
                        report["preserved"] += 1
                        continue
                    report["missing_primary"] += 1
                    outcome = await self._repair(item["Id"], owner_id)
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    outcome = "failed"
                report[outcome] += 1
            if report["failed"]:
                report["status"] = "partial"
        self.last_report = report
        return dict(report)

    async def _repair(self, item_id: str, owner_id: str) -> str:
        item_path = "/Items/" + quote(item_id, safe="")
        detail_path = f"/Users/{owner_id}{item_path}"
        item = await self._get(detail_path)
        _validate_collection(item, item_id)
        if _has_primary(item):
            return "preserved"
        members = await self._items(
            f"/Users/{owner_id}/Items", {"ParentId": item_id, "Fields": "ProviderIds"},
        )
        member_ids = {_provider_id(member, "TmdbCollection") for member in members}
        if (
            len(member_ids) != 1 or None in member_ids
            or any(member.get("Type") != "Movie" for member in members)
        ):
            return "unresolved"
        collection_id = member_ids.pop()
        providers = item.get("ProviderIds", {})
        existing_keys = [key for key in providers if key.casefold() == "tmdb"]
        if existing_keys and _provider_id(item, "Tmdb") != collection_id:
            return "unresolved"

        lookup = {"Name": item.get("Name"), "ProviderIds": {"Tmdb": collection_id}}
        if item.get("PreferredMetadataLanguage"):
            lookup["MetadataLanguage"] = item["PreferredMetadataLanguage"]
        response = await self._request("POST", "/Items/RemoteSearch/BoxSet", json={
            "SearchInfo": lookup, "IncludeDisabledProviders": True,
        })
        matches = [row for row in response.json() if _provider_id(row, "Tmdb") == collection_id]
        if len(matches) != 1 or not _official_poster(matches[0].get("ImageUrl")):
            return "unresolved"
        poster_url = matches[0]["ImageUrl"]

        # Fetch the complete native DTO again so concurrent owner edits are retained.
        item = await self._get(detail_path)
        if _has_primary(item):
            return "preserved"
        _validate_collection(item, item_id)
        providers = item.get("ProviderIds", {})
        existing_keys = [key for key in providers if key.casefold() == "tmdb"]
        if existing_keys and _provider_id(item, "Tmdb") != collection_id:
            return "unresolved"
        if not existing_keys:
            # POST /Items replaces editable fields. Never send a partial DTO or
            # change LockData: changing it can propagate to a folder's children.
            payload = deepcopy(item)
            payload["ProviderIds"] = dict(providers, Tmdb=collection_id)
            await self._request("POST", item_path, json=payload)
            observed = await self._get(detail_path)
            if (
                observed.get("ProviderIds") != payload["ProviderIds"]
                or any(observed.get(key) != item.get(key)
                       for key in ("Name", "LockData", "LockedFields"))
            ):
                raise ValueError("Native collection update read-back drift")
            item = observed
        if _has_primary(item):
            return "preserved"
        # Download explicitly targets Primary and works on locked collections.
        # Unlike Identify/Apply, it cannot replace metadata or other image types.
        await self._request("POST", item_path + "/RemoteImages/Download", params={
            "type": "Primary", "imageUrl": poster_url,
        })
        observed = await self._get(detail_path)
        if not _has_primary(observed):
            raise ValueError("Native collection poster read-back drift")
        return "repaired"
