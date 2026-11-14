"""Localize Brazilian movies by native country and exact TMDb identity.

Only the movie's display name, metadata locale and Primary image are changed.
Jellyfin 10.10.7 replaces editable metadata on POST /Items: use a fresh complete
DTO and retain provider IDs, paths, owner locks and custom sort preferences.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from copy import deepcopy
from urllib.parse import quote

import httpx

from .jellyfin_collections import JellyfinCollectionReconciler, _official_poster, _provider_id


def _brazilian(item: dict) -> bool:
    countries = item.get("ProductionLocations")
    return isinstance(countries, list) and any(
        isinstance(country, str) and country.strip().casefold() in {"brazil", "brasil", "br"}
        for country in countries
    )


def _validate_movie(item: dict, item_id: str) -> None:
    if (
        not isinstance(item, dict) or item.get("Id") != item_id or item.get("Type") != "Movie"
        or type(item.get("LockData")) is not bool
        or not isinstance(item.get("Name"), str) or not item["Name"].strip()
        or not isinstance(item.get("Path"), str) or not item["Path"]
        or _provider_id(item, "Tmdb") is None or not _brazilian(item)
        or not isinstance(item.get("ImageTags"), dict)
        or (item.get("LockedFields") is not None and (
            not isinstance(item["LockedFields"], list)
            or any(not isinstance(field, str) for field in item["LockedFields"])
        ))
    ):
        raise ValueError("Incomplete native Brazilian movie identity or metadata")


def _bound(item: dict, previous: dict) -> None:
    _validate_movie(item, previous["Id"])
    if any(item.get(key) != previous.get(key) for key in ("Path", "ProviderIds")):
        raise ValueError("Native movie identity drift")


def _primary_tag(item: dict) -> str | None:
    tag = item.get("ImageTags", {}).get("Primary")
    if tag is not None and (not isinstance(tag, str) or not tag):
        raise ValueError("Incomplete native movie image identity")
    return tag


def _cache_key(item: dict) -> tuple:
    return (
        item["Path"], tuple(sorted(item["ProviderIds"].items())), item["Name"],
        item.get("PreferredMetadataLanguage"), item.get("PreferredMetadataCountryCode"),
        _primary_tag(item),
    )


_PRESERVED_FIELDS = (
    "Path", "ProviderIds", "LockData", "LockedFields", "OriginalTitle", "ForcedSortName",
    "Overview", "Genres", "Tags", "People", "ProductionLocations", "ProductionYear",
    "Studios", "OfficialRating", "CustomRating", "CommunityRating", "CriticRating", "PremiereDate",
)


def _readback(observed: dict, expected: dict) -> None:
    _bound(observed, expected)
    if any(observed.get(key) != expected.get(key) for key in (
        *_PRESERVED_FIELDS, "Name", "PreferredMetadataLanguage", "PreferredMetadataCountryCode",
    )):
        raise ValueError("Native localized movie metadata read-back drift")


class JellyfinBrazilianMetadataReconciler(JellyfinCollectionReconciler):
    """Reuse authenticated pagination; no collection reconciliation is invoked."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        admin_username: str,
        interval_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ):
        super().__init__(
            client, base_url=base_url, api_key=api_key, admin_username=admin_username,
            interval_seconds=interval_seconds, clock=clock,
        )
        self._confirmed: dict[str, tuple] = {}
        self._poster_urls: dict[str, str] = {}

    async def reconcile(self, *, force: bool = False) -> dict[str, str | int]:
        now = self.clock()
        if not force and now < self._next_run:
            return self.last_report | {"status": "waiting"}
        self._next_run = now + self.interval_seconds
        report: dict[str, str | int] = {
            "status": "completed", "movies": 0, "brazilian": 0,
            "repaired": 0, "preserved": 0, "unresolved": 0, "failed": 0,
        }
        if not self.base_url or not self.api_key or not self.admin_username:
            report["status"] = "unconfigured"
            self.last_report = report
            return dict(report)
        try:
            users = await self._get("/Users")
            owners = [user for user in users if user.get("Name") == self.admin_username]
            if len(owners) != 1 or not isinstance(owners[0].get("Id"), str) or not owners[0]["Id"]:
                raise ValueError("Ambiguous native owner")
            owner_id = quote(owners[0]["Id"], safe="")
            items = await self._items("/Items", {
                "IncludeItemTypes": "Movie", "Recursive": "true",
                "Fields": "ProviderIds,ProductionLocations,Path",
                "CollapseBoxSetItems": "false", "UserId": owners[0]["Id"],
            })
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            report.update(status="unavailable", failed=1)
        else:
            report["movies"] = len(items)
            for item in items:
                if item.get("Type") != "Movie" or not _brazilian(item):
                    report["preserved"] += 1
                    continue
                report["brazilian"] += 1
                try:
                    outcome = await self._localize(item["Id"], owner_id)
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    self._confirmed.pop(item["Id"], None)
                    self._poster_urls.pop(item["Id"], None)
                    outcome = "failed"
                report[outcome] += 1
            if report["failed"]:
                report["status"] = "partial"
        self.last_report = report
        return dict(report)

    async def _portuguese_poster(self, item_path: str, preferred_url: object) -> str | None:
        rows = []
        while True:
            page = await self._get(item_path + "/RemoteImages", params={
                "type": "Primary", "includeAllLanguages": "true",
                "startIndex": len(rows), "limit": 1000,
            })
            images, total = page.get("Images"), page.get("TotalRecordCount")
            if (
                not isinstance(images, list) or any(not isinstance(row, dict) for row in images)
                or type(total) is not int or total < 0
                or (not images and len(rows) < total)
            ):
                raise ValueError("Incomplete native movie image page")
            rows.extend(images)
            if len(rows) >= total:
                break
        eligible = [row for row in rows if (
            row.get("Language") in {"pt", "pt-BR"}
            and row.get("ProviderName") == "TheMovieDb" and row.get("Type") == "Primary"
            and _official_poster(row.get("Url"))
            and type(row.get("Width")) is int and row["Width"] > 0
            and (row.get("Height") is None or (
                type(row["Height"]) is int and row["Height"] > 0
            ))
        )]
        if not eligible:
            return None
        preferred = [row for row in eligible if row["Url"] == preferred_url]
        if preferred:
            return preferred[0]["Url"]
        # The item-bound native query is authoritative. Prefer highest available
        # resolution and break ties by URL so provider order cannot change policy.
        eligible.sort(key=lambda row: (
            -(row["Width"] * (row.get("Height") or 1)), -row["Width"], row["Url"],
        ))
        return eligible[0]["Url"]

    async def _localize(self, item_id: str, owner_id: str) -> str:
        item_path = "/Items/" + quote(item_id, safe="")
        detail_path = f"/Users/{owner_id}{item_path}"
        original = await self._get(detail_path)
        _validate_movie(original, item_id)
        if self._confirmed.get(item_id) == _cache_key(original):
            return "preserved"
        tmdb_id = _provider_id(original, "Tmdb")
        response = await self._request("POST", "/Items/RemoteSearch/Movie", json={
            "SearchInfo": {
                "ProviderIds": {"Tmdb": tmdb_id}, "MetadataLanguage": "pt-BR",
                "MetadataCountryCode": "BR",
            },
            "IncludeDisabledProviders": True,
        })
        results = response.json()
        if not isinstance(results, list) or any(not isinstance(row, dict) for row in results):
            raise ValueError("Unsupported native movie search")
        matches = [row for row in results if _provider_id(row, "Tmdb") == tmdb_id]
        if len(matches) != 1:
            return "unresolved"
        localized_name = matches[0].get("Name")
        if not isinstance(localized_name, str) or not localized_name.strip():
            return "unresolved"
        poster_url = await self._portuguese_poster(item_path, matches[0].get("ImageUrl"))

        # Remote calls may take time. Renew identity and preserve fresh owner edits.
        item = await self._get(detail_path)
        _bound(item, original)
        payload = deepcopy(item)
        payload.update(Name=localized_name, PreferredMetadataLanguage="pt-BR",
                       PreferredMetadataCountryCode="BR")
        if payload != item:
            await self._request("POST", item_path, json=payload)
            observed = await self._get(detail_path)
            _readback(observed, payload)
            item = observed

        # A missing Portuguese poster leaves only artwork pending; the exact
        # localized title/locale stay applied, and no image proof is cached.
        if poster_url is None:
            return "unresolved"

        # Renew all identity/metadata gates immediately before the image effect.
        fresh = await self._get(detail_path)
        _readback(fresh, item)
        previous_tag = _primary_tag(fresh)
        await self._request("POST", item_path + "/RemoteImages/Download", params={
            "type": "Primary", "imageUrl": poster_url,
        })
        observed = await self._get(detail_path)
        _readback(observed, fresh)
        current_tag = _primary_tag(observed)
        previously_proven = (
            self._confirmed.get(item_id) == _cache_key(fresh)
            and self._poster_urls.get(item_id) == poster_url
        )
        # A changed Name/locale can invalidate the whole cache while the prior
        # Portuguese Primary remains intact. Only a prior exact path/IDs/tag/URL
        # proof can accept an unchanged tag; HTTP presence alone proves no locale.
        confirmed = self._confirmed.get(item_id)
        if confirmed is not None and self._poster_urls.get(item_id) == poster_url:
            previously_proven = (
                confirmed[:2] == _cache_key(fresh)[:2] and confirmed[-1] == previous_tag
            )
        if not current_tag or (current_tag == previous_tag and not previously_proven):
            raise ValueError("Native localized poster read-back not proven")
        response = await self._request("GET", item_path + "/Images/Primary", params={
            "tag": current_tag,
        })
        if (
            response.status_code != 200 or not response.content
            or not response.headers.get("Content-Type", "").lower().startswith("image/")
        ):
            raise ValueError("Native localized poster HTTP read-back unavailable")
        self._confirmed[item_id] = _cache_key(observed)
        self._poster_urls[item_id] = poster_url
        return "repaired"
