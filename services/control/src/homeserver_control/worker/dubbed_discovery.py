"""Bounded PT-BR discovery; new candidates still need Arr's native push decision."""

from __future__ import annotations

import asyncio
import copy
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .audio_evidence import description_audio_languages, qualified_title_audio_languages
from .release_quality import ReleasePolicy, _visual_quality, release_indexer, release_seeders

_DETAIL_HOSTS = {
    "uindex.org",
    "www.uindex.org",
    "1337x.to",
    "1337x.st",
    "1337x.ws",
    "1337x.eu",
    "1337x.se",
    "1337x.so",
    "1337x.cc",
    "x1337x.ws",
}


def _indexer(value):
    return release_indexer({"indexer": value})


def _blocked_indexers(statuses):
    if not isinstance(statuses, list):
        raise ValueError("invalid indexer status")
    blocked = set()
    for status in statuses:
        until = status.get("disabledTill") if isinstance(status, dict) else None
        if until:
            if not isinstance(until, str):
                raise ValueError("invalid indexer backoff")
            timestamp = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("invalid indexer backoff timezone")
            if timestamp > datetime.now(UTC):
                blocked.add(status.get("indexerId"))
    return blocked


def search_queries(
    media: dict, *, season: int | None = None, localized_title: str | None = None
) -> list[str]:
    """Keep native/original and a Brazilian alias, then explicit audio queries."""
    aliases = media.get("alternateTitles") or media.get("alternativeTitles") or []
    brazilian = [
        row.get("title")
        for row in aliases
        if isinstance(row, dict)
        and any(
            token in str(row.get("language", "")).casefold()
            for token in ("portugu", "brazil", "pt-br")
        )
    ]
    titles = list(
        dict.fromkeys(
            value.strip()
            for value in [
                media.get("title"), media.get("originalTitle"), localized_title, *brazilian,
            ]
            if isinstance(value, str) and value.strip()
        )
    )[:3]
    if not titles:
        return []
    suffix = f" S{season:02d}" if season is not None else ""
    dubbed_title = (
        localized_title.strip() if localized_title and localized_title.strip() else titles[-1]
    )
    queries = [title + suffix for title in titles]
    queries.extend(
        (
            titles[0] + suffix + " DUAL",
            dubbed_title + suffix + " DUBLADO",
            titles[0] + suffix + " PTBR",
            titles[0] + suffix + " PT-BR",
        )
    )
    return list(dict.fromkeys(queries))


def season_pack_queries(media: dict, season: int) -> tuple[str, ...]:
    """Try the public season wording before Sxx, with bounded Arr title aliases."""
    aliases = media.get("alternateTitles") or media.get("alternativeTitles") or []
    titles = {}
    for value in [media.get("title"), media.get("originalTitle"), *(
        row.get("title") for row in aliases if isinstance(row, dict)
    )]:
        if isinstance(value, str) and value.strip():
            titles.setdefault(value.strip().casefold(), value.strip())
    names = list(titles.values())[:3]
    return tuple(
        [f"{title} Season {season}" for title in names]
        + [f"{title} S{season:02d}" for title in names]
    )


@dataclass(frozen=True)
class DiscoveryResult:
    releases: list[dict]
    complete: bool
    truncated_queries: tuple[str, ...] = ()


def _narrow_capped_query(indexer: str, query: str, media: dict, season: int | None) -> list[str]:
    """Cardigann definitions do not forward offset; narrow without fabricating pages."""
    imdb = media.get("imdbId")
    if indexer == "amigosshare" and isinstance(imdb, str) and re.fullmatch(r"tt[0-9]+", imdb):
        return [imdb] if query != imdb else []
    year = media.get("year")
    if indexer == "uindex" and season is None and type(year) is int:
        if not re.search(rf"(?<![0-9]){year}(?![0-9])", query):
            return [f"{query} {year}"]
    # Explicit audio variants cover the dual search, including an ASC title with
    # no resolution badge. We do not claim a resolution-filtered query is exhaustive.
    if not re.search(r"(?i)(?<![a-z])(?:dual|dublado|ptbr|pt-br)(?![a-z])", query):
        return [f"{query} {token}" for token in ("DUAL", "DUBLADO", "PTBR", "PT-BR")]
    return []



class _TemporaryUnavailable(ValueError):
    """A timed-out request stays incomplete without monopolizing the next cycle."""


def _keys(row):
    keys = set()
    infohash = row.get("infoHash")
    if isinstance(infohash, str) and re.fullmatch(r"[0-9a-fA-F]{40}", infohash):
        keys.add(("hash", infohash.lower()))
    for field in ("guid", "downloadUrl"):
        if isinstance(row.get(field), str) and row[field]:
            keys.add((field, row[field]))
    if row.get("title"):
        keys.add(("title", _indexer(row.get("indexer")), row["title"].casefold()))
    return keys


def _identity(parsed, media, *, season):
    """Require Arr's resolved identity, never just a fuzzy title or a category."""
    kind = "movie" if season is None else "series"
    matched = parsed.get(kind)
    detail = parsed.get("parsedMovieInfo" if season is None else "parsedEpisodeInfo")
    if not isinstance(matched, dict) or not isinstance(detail, dict):
        return False
    if (type(media.get("id")) is not int or media["id"] <= 0
            or type(matched.get("id")) is not int or matched["id"] <= 0
            or matched["id"] != media.get("id")):
        return False
    for field in ("tmdbId", "tvdbId"):
        if media.get(field) and matched.get(field) != media[field]:
            # Sonarr may lack TMDB while an authenticated identity resolver has
            # supplied it. Only its same positive native ID + TVDB proves that
            # absence; a positive conflict, bool or malformed ID stays rejected.
            if (season is not None and field == "tmdbId"
                    and type(media[field]) is int and media[field] > 0
                    and (matched.get(field) is None
                         or type(matched.get(field)) is int and matched[field] == 0)
                    and type(media.get("tvdbId")) is int and media["tvdbId"] > 0
                    and type(matched.get("tvdbId")) is int
                    and matched["tvdbId"] == media["tvdbId"]):
                continue
            return False
    if season is None:
        return type(media.get("year")) is int and detail.get("year") == media["year"]
    episodes = parsed.get("episodes")
    return (
        detail.get("seasonNumber") == season
        and isinstance(episodes, list)
        and bool(episodes)
        and all(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and row.get("seriesId") == media["id"]
            and row.get("seasonNumber") == season
            for row in episodes
        )
    )


def _complete_season_identity(parsed, media, season, catalog):
    if (
        not _identity(parsed, media, season=season)
        or parsed["parsedEpisodeInfo"].get("fullSeason") is not True
        or any(parsed["parsedEpisodeInfo"].get(field) for field in (
            "isPartialSeason", "isMultiSeason", "isSeasonExtra",
        ))
        or parsed["parsedEpisodeInfo"].get("seasonNumbers", [season]) != [season]
        or not isinstance(catalog, list)
    ):
        return False
    expected = [
        (row.get("id"), row.get("episodeNumber")) for row in catalog
        if isinstance(row, dict) and row.get("seasonNumber") == season
    ]
    if (
        not expected
        or any(type(value) is not int or value <= 0 for pair in expected for value in pair)
        or len({pair[0] for pair in expected}) != len(expected)
        or len({pair[1] for pair in expected}) != len(expected)
    ):
        return False
    actual = [(row["id"], row.get("episodeNumber")) for row in parsed["episodes"]]
    return (
        len(actual) == len(expected)
        and all(type(value) is int and value > 0 for pair in actual for value in pair)
        and set(actual) == set(expected)
    )


def _localized_series_parse_title(row, parsed, media, localized_title, season):
    """An exact authenticated alias may obtain proof; never rebind a known series."""
    detail = parsed.get("parsedEpisodeInfo")
    if (season is None or _indexer(row.get("indexer")) != "amigosshare"
            or parsed.get("series") is not None or parsed.get("episodes") not in (None, [])
            or not isinstance(detail, dict) or detail.get("seasonNumber") != season
            or not isinstance(localized_title, str) or not localized_title
            or not isinstance(media.get("title"), str) or not media["title"].strip()
            or any(type(media.get(field)) is not int or media[field] <= 0
                   for field in ("id", "tmdbId", "tvdbId"))):
        return None
    for field in ("tmdbId", "tvdbId"):
        value = row.get(field)
        if value is not None and (type(value) is not int or value not in (0, media[field])):
            return None
    if row.get("imdbId") and row["imdbId"] != media.get("imdbId"):
        return None
    # ASC can label each season with its release year. Sonarr treats that as
    # the series premiere year, so omit only this bounded token in parse proof.
    # The original title/year and all season/quality/audio suffix bytes survive
    # unchanged in the candidate sent to Sonarr's normal identifier-aware push.
    prefix = re.escape(localized_title).replace(r"\ ", r"[ ._-]+")
    match = re.fullmatch(
        prefix + r"[ ._-]+(?:(?:19|20)[0-9]{2}[ ._-]+)?"
        r"(?P<suffix>S(?P<season>[0-9]{1,2})(?=[ ._-]|$).*)",
        row["title"], re.IGNORECASE,
    )
    if match is None or int(match["season"]) != season:
        return None
    return media["title"].strip() + " " + match["suffix"]


def _same_series_release_facts(original, canonical):
    """Only title identity may differ between the original and proof parse."""
    identity_fields = {"releaseTitle", "seriesTitle", "seriesTitleInfo"}
    return (
        isinstance(canonical, dict)
        and {key: value for key, value in original.items() if key not in identity_fields}
        == {key: value for key, value in canonical.items() if key not in identity_fields}
    )


def _native_alias_retry(row):
    """Only an unresolved ASC identity veto can request a fresh native decision."""
    return (
        _indexer(row.get("indexer")) == "amigosshare"
        and row.get("rejected") is True
        and row.get("rejections") == ["Unknown Series"]
        and row.get("fullSeason") is True
        and all(row.get(field) is None or type(row.get(field)) is int and row[field] == 0
                for field in ("seriesId", "mappedSeriesId"))
        and all(row.get(field) is None or row[field] == []
                for field in ("episodeIds", "episodeNumbers", "mappedEpisodeNumbers",
                              "mappedEpisodeInfo"))
    )


def _possible_candidate(row: dict, policy: ReleasePolicy, *, parsed_quality: bool) -> bool:
    """Filter known download vetoes without requiring any audio evidence yet."""
    seeds = release_seeders(row)
    if (
        release_indexer(row) not in policy.preferred_indexers
        or seeds is None
        or seeds < policy.minimum_seeders
        or type(row.get("size")) is not int
        or row["size"] <= 0
    ):
        return False
    return not parsed_quality or _visual_quality(row, policy) is not None


class DubbedDiscovery:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        prowlarr_url: str,
        prowlarr_api_key: str,
        interval_seconds: float = 2,
        max_queries: int = 12,
        max_candidates: int = 30,
        max_descriptions: int = 8,
        byparr_url: str | None = None,
        seerr_url: str | None = None,
        seerr_api_key: str | None = None,
        localized_title_timeout_seconds: float = 3,
        total_timeout_seconds: float = 30,
        query_timeout_seconds: float = 15,
        description_timeout_seconds: float = 12,
        description_runtime_seconds: float = 70,
    ):
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                total_timeout_seconds,
                query_timeout_seconds,
                description_timeout_seconds,
                description_runtime_seconds,
                localized_title_timeout_seconds,
            )
        ):
            raise ValueError("discovery timeouts must be finite and positive")
        self.client = client
        self.prowlarr_url = prowlarr_url.rstrip("/")
        self.headers = {"X-Api-Key": prowlarr_api_key}
        self.interval_seconds = max(0, interval_seconds)
        self.max_queries = max(0, max_queries)
        self.max_candidates = max(1, max_candidates)
        self.max_descriptions = max(0, max_descriptions)
        self.byparr_url = byparr_url.rstrip("/") if byparr_url else None
        self.seerr_url = seerr_url.rstrip("/") if seerr_url else None
        self.seerr_headers = {"X-Api-Key": seerr_api_key} if seerr_api_key else None
        self.localized_title_timeout_seconds = localized_title_timeout_seconds
        self.total_timeout_seconds = total_timeout_seconds
        self.query_timeout_seconds = query_timeout_seconds
        self.description_timeout_seconds = description_timeout_seconds
        self.description_runtime_seconds = description_runtime_seconds
        self._description_tasks: dict[str, asyncio.Task[set[str]]] = {}
        self._closed = False
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._cooldowns: dict[tuple, float] = {}
        self._query_cursors: dict[tuple, int] = {}
        self._description_cursors: dict[tuple, int] = {}
        self._next_indexer = 0
        self._search_lock = asyncio.Lock()
        self._last_search = 0.0

    async def _get_localized_title(self, media, *, season, deadline):
        """Cache only an identity-checked title; metadata is never audio evidence."""
        if self.seerr_url is None:
            return None
        tmdb_id = media.get("tmdbId")
        if type(tmdb_id) is not int or tmdb_id <= 0 or not self.seerr_headers:
            raise ValueError("localized title lookup is not configured for this identity")
        kind = "movie" if season is None else "tv"
        key = ("localized-title", kind, tmdb_id, "pt-BR")
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        timeout = min(self.localized_title_timeout_seconds, deadline - time.monotonic())
        if timeout <= 0:
            raise _TemporaryUnavailable("localized title deadline reached")
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.get(
                    f"{self.seerr_url}/api/v1/{kind}/{tmdb_id}",
                    headers=self.seerr_headers,
                    params={"language": "pt-BR"},
                    timeout=timeout,
                    follow_redirects=False,
                )
                response.raise_for_status()
                payload = response.json()
        except (TimeoutError, httpx.TimeoutException) as error:
            raise _TemporaryUnavailable("localized title timed out") from error
        if (
            not isinstance(payload, dict)
            or type(payload.get("id")) is not int
            or payload["id"] != tmdb_id
        ):
            raise ValueError("localized title identity is invalid")
        title = payload.get("title" if kind == "movie" else "name")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("localized title is missing")
        title = title.strip()
        if len(self._cache) >= 512:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (time.monotonic() + 43200, title)
        return title

    async def _get(
        self,
        url,
        *,
        headers=None,
        params=None,
        ttl=3600,
        throttle=False,
        refresh=False,
        deadline=None,
    ):
        key = (url, tuple(sorted((params or {}).items())))
        cached = self._cache.get(key)
        if not refresh and cached and cached[0] > time.monotonic():
            return copy.deepcopy(cached[1])
        if self._cooldowns.get(key, 0) > time.monotonic():
            raise _TemporaryUnavailable("request temporarily deferred")
        timeout = self.query_timeout_seconds
        if deadline is not None:
            timeout = min(timeout, max(0, deadline - time.monotonic()))
        try:
            async with asyncio.timeout(timeout), self._search_lock:
                if throttle:
                    await asyncio.sleep(
                        max(0, self._last_search + self.interval_seconds - time.monotonic())
                    )
                    self._last_search = time.monotonic()
                response = await self.client.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=self.query_timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
        except (TimeoutError, httpx.TimeoutException) as error:
            self._defer(key)
            raise _TemporaryUnavailable("request timed out") from error
        if len(self._cache) >= 512:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (time.monotonic() + ttl, payload)
        return copy.deepcopy(payload)

    async def _description(self, url):
        if self._closed:
            raise _TemporaryUnavailable("description discovery is closed")
        key = ("description", url)
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return set(cached[1])
        if self._cooldowns.get(key, 0) > time.monotonic():
            raise _TemporaryUnavailable("description temporarily deferred")
        task = self._description_tasks.get(url)
        if task is None:
            # One shared browser request may outlive the current discovery. Do
            # not build an unbounded queue while the solver is still occupied.
            if self._description_tasks:
                raise _TemporaryUnavailable("description solver is busy")
            task = asyncio.create_task(self._run_description(url))
            self._description_tasks[url] = task
            task.add_done_callback(lambda finished: self._description_finished(url, finished))
        try:
            async with asyncio.timeout(self.description_timeout_seconds):
                return set(await asyncio.shield(task))
        except TimeoutError as error:
            # The caller's budget is shorter than a normal browser challenge.
            # Preserve the task/cache for another cycle without treating a
            # still-running request as either failure or absence of PT-BR.
            raise _TemporaryUnavailable("description still pending") from error

    async def _run_description(self, url):
        try:
            async with asyncio.timeout(self.description_runtime_seconds):
                return await self._load_description(url)
        except (TimeoutError, httpx.TimeoutException) as error:
            raise _TemporaryUnavailable("description timed out") from error

    def _description_finished(self, url, task):
        if self._description_tasks.get(url) is task:
            self._description_tasks.pop(url)
        # Retrieving the exception also covers failures after every waiting
        # enrich call has returned or been cancelled.
        if not task.cancelled() and task.exception() is not None:
            self._defer(("description", url))

    async def aclose(self):
        """Stop shared requests before their owning HTTP client is closed."""
        self._closed = True
        tasks = list(self._description_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._description_tasks.clear()

    def _defer(self, key):
        if len(self._cooldowns) >= 256:
            self._cooldowns.pop(next(iter(self._cooldowns)))
        self._cooldowns[key] = time.monotonic() + 300

    async def _load_description(self, url):
        key = ("description", url)
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (ValueError, TypeError):
            parsed = None
            port = None
        if (
            parsed is None
            or parsed.scheme not in {"https", "http"}
            or parsed.hostname not in _DETAIL_HOSTS
            or parsed.username
            or parsed.password
            or port not in {None, 80, 443}
            or not (parsed.path == "/details.php" or parsed.path.startswith("/torrent/"))
        ):
            if len(self._cache) >= 512:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic() + 43200, set())
            return set()
        cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return set(cached[1])
        # No Arr/Prowlarr headers go to an external release page. Redirects cannot
        # expand the host allowlist or reach an internal URL.
        response = await self.client.get(url, timeout=20, follow_redirects=False)
        if response.status_code in (403, 503) and self.byparr_url:
            response = await self.client.post(
                f"{self.byparr_url}/v1",
                json={
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": 45000,
                },
                timeout=50,
            )
            response.raise_for_status()
            payload = response.json()
            solution = payload.get("solution", {}) if isinstance(payload, dict) else {}
            if solution.get("status") != 200 or not isinstance(solution.get("response"), str):
                raise ValueError("release description unavailable")
            target = urlsplit(solution.get("url", ""))
            if (
                target.hostname not in _DETAIL_HOSTS
                or target.scheme not in {"http", "https"}
                or target.port not in {None, 80, 443}
                or target.username
                or target.password
            ):
                raise ValueError("release description redirected outside approved indexers")
            text = solution["response"][:1_000_000]
        else:
            response.raise_for_status()
            text = response.text[:1_000_000]
        languages = description_audio_languages(text)
        self._cache[key] = (time.monotonic() + 43200, languages)
        return languages

    async def enrich(
        self,
        media: dict,
        native: list[dict],
        *,
        arr_url: str,
        arr_headers: dict,
        season: int | None = None,
        allow_supplemental: bool = True,
        release_policy: ReleasePolicy | None = None,
        rejection_allowed: Callable[[dict], bool] | None = None,
        queries_override: Sequence[str] | None = None,
        require_complete_season_pack: bool = False,
    ) -> DiscoveryResult:
        native = [
            dict(row, audioLanguagesInferred=True, audioEvidence=[])
            for row in native
            if isinstance(row, dict)
        ]
        # Leave a third of the wall-clock budget for parsing/description evidence.
        # Completed cache entries survive cancellation; the mutable partial result
        # never treats an unfinished search as permission for original fallback.
        search_deadline = time.monotonic() + self.total_timeout_seconds * 2 / 3
        try:
            async with asyncio.timeout(self.total_timeout_seconds):
                return await self._enrich(
                    media,
                    native,
                    arr_url=arr_url,
                    arr_headers=arr_headers,
                    season=season,
                    allow_supplemental=allow_supplemental,
                    search_deadline=search_deadline,
                    release_policy=release_policy or ReleasePolicy(profile_name="Dublado"),
                    rejection_allowed=rejection_allowed,
                    queries_override=queries_override,
                    require_complete_season_pack=require_complete_season_pack,
                )
        except TimeoutError:
            return DiscoveryResult(native, False)

    async def _enrich(
        self,
        media,
        native,
        *,
        arr_url,
        arr_headers,
        season,
        allow_supplemental,
        search_deadline,
        release_policy,
        rejection_allowed,
        queries_override,
        require_complete_season_pack,
    ) -> DiscoveryResult:
        complete = True
        truncated_queries = []
        localized_title = None
        try:
            localized_title = await self._get_localized_title(
                media, season=season, deadline=search_deadline,
            )
        except (httpx.HTTPError, ValueError):
            complete = False
        queries = (
            list(dict.fromkeys(queries_override)) if queries_override is not None
            else search_queries(media, season=season, localized_title=localized_title)
        )
        seen = set().union(*(_keys(row) for row in native)) if native else set()
        raw = []
        try:
            indexers = await self._get(
                f"{self.prowlarr_url}/api/v1/indexer", headers=self.headers, ttl=60
            )
            statuses = await self._get(
                f"{self.prowlarr_url}/api/v1/indexerstatus", headers=self.headers, ttl=30
            )
            if not isinstance(indexers, list) or not isinstance(statuses, list):
                raise ValueError("invalid indexer state")
            blocked = _blocked_indexers(statuses)
            required_indexers = release_policy.preferred_indexers
            allowed = [
                row
                for row in indexers
                if isinstance(row, dict)
                and _indexer(row.get("name")) in required_indexers
                and row.get("enable") is True
                and type(row.get("id")) is int
            ]
            if not set(required_indexers).issubset({_indexer(row["name"]) for row in allowed}):
                complete = False
            budget = self.max_queries
            allowed.sort(key=lambda row: required_indexers.index(_indexer(row["name"])))
            offset = self._next_indexer % len(allowed) if allowed else 0
            ordered_indexers = allowed[offset:] + allowed[:offset]
            for indexer in ordered_indexers:
                if time.monotonic() >= search_deadline:
                    complete = False
                    break
                self._next_indexer = (allowed.index(indexer) + 1) % len(allowed)
                if indexer["id"] in blocked:
                    complete = False
                    continue
                indexer_queries = queries
                imdb = media.get("imdbId")
                if (_indexer(indexer["name"]) == "amigosshare"
                        and isinstance(imdb, str) and re.fullmatch(r"tt[0-9]+", imdb)):
                    indexer_queries = list(dict.fromkeys([imdb, *queries]))
                cursor_key = (indexer["id"], tuple(indexer_queries))
                offset = self._query_cursors.get(cursor_key, 0)
                ordered_queries = indexer_queries[offset:] + indexer_queries[:offset]
                for query in ordered_queries:
                    if time.monotonic() >= search_deadline:
                        complete = False
                        break
                    search_params = {
                        "query": query,
                        "type": "search",
                        "indexerIds": str(indexer["id"]),
                    }
                    search_key = (
                        f"{self.prowlarr_url}/api/v1/search",
                        tuple(sorted(search_params.items())),
                    )
                    cached = self._cache.get(search_key)
                    now = time.monotonic()
                    if (
                        (not cached or cached[0] <= now)
                        and self._cooldowns.get(search_key, 0) <= now
                    ):
                        if budget <= 0:
                            complete = False
                            continue
                        budget -= 1
                    if len(self._query_cursors) >= 128 and cursor_key not in self._query_cursors:
                        self._query_cursors.pop(next(iter(self._query_cursors)))
                    if query in indexer_queries:
                        self._query_cursors[cursor_key] = (
                            indexer_queries.index(query) + 1
                        ) % len(indexer_queries)
                    try:
                        rows = await self._get(
                            f"{self.prowlarr_url}/api/v1/search",
                            headers=self.headers,
                            params=search_params,
                            throttle=True,
                            deadline=search_deadline,
                        )
                        if not isinstance(rows, list):
                            raise ValueError("invalid indexer search")
                        source = _indexer(indexer["name"])
                        cap = {"amigosshare": 96, "uindex": 100}.get(source)
                        if cap is not None and len(rows) >= cap:
                            truncated_queries.append(f"{source}: {query}")
                            narrow = _narrow_capped_query(source, query, media, season)
                            # Narrow text/ID searches discover more candidates;
                            # they cannot prove absence in the capped broad sample.
                            complete = False
                            for value in narrow:
                                if value not in ordered_queries:
                                    ordered_queries.append(value)
                        for row in rows:
                            if not isinstance(row, dict):
                                continue
                            row = {**row, "indexer": indexer["name"]}
                            if not _possible_candidate(row, release_policy, parsed_quality=False):
                                continue
                            keys = _keys(row)
                            if keys & seen:
                                continue
                            seen.update(keys)
                            raw.append(row)
                        statuses = await self._get(
                            f"{self.prowlarr_url}/api/v1/indexerstatus",
                            headers=self.headers,
                            ttl=30,
                            refresh=True,
                            deadline=search_deadline,
                        )
                        blocked = _blocked_indexers(statuses)
                        if indexer["id"] in blocked:
                            self._cache.pop(search_key, None)
                            complete = False
                            break
                    except _TemporaryUnavailable:
                        self._cache.pop(search_key, None)
                        complete = False
                        continue
                    except asyncio.CancelledError:
                        # A 200 response without its final health check must not
                        # become a cached proof that no dubbed result exists.
                        self._cache.pop(search_key, None)
                        raise
                    except (httpx.HTTPError, ValueError):
                        self._cache.pop(search_key, None)
                        complete = False
                        break

        except (httpx.HTTPError, ValueError):
            complete = False
        for row in native:
            if (_native_alias_retry(row)
                    and _possible_candidate(row, release_policy, parsed_quality=True)
                    and not any(other is not row and _keys(row) & _keys(other)
                                for other in native)):
                # Keep the original veto in output/seen. A separate copy may
                # obtain exact alias/catalog proof and normal identifier-aware
                # push review; mixed vetoes and resolved identities never retry.
                raw.append(copy.deepcopy(row))
        native_indexers = []
        episode_catalog = None
        if raw and allow_supplemental:
            try:
                native_indexers = await self._get(
                    f"{arr_url}/api/v3/indexer", headers=arr_headers, ttl=60
                )
                if not isinstance(native_indexers, list):
                    raise ValueError("invalid native indexers")
                if require_complete_season_pack:
                    episode_catalog = await self._get(
                        f"{arr_url}/api/v3/episode", headers=arr_headers,
                        params={"seriesId": media["id"]}, ttl=60,
                    )
                    if not isinstance(episode_catalog, list):
                        raise ValueError("invalid native episode catalog")
            except (httpx.HTTPError, ValueError):
                complete = False
                raw = []
        elif not allow_supplemental:
            raw = []
        # Keep every native decision in `seen`/output, including rejections, so
        # supplemental duplicates cannot bypass them. Only spend inspection
        # budgets on rows whose known quality/seeds/rejection permit a download.
        candidates = [
            (False, row)
            for row in native
            if _possible_candidate(row, release_policy, parsed_quality=True)
            and (
                row.get("rejected") is False
                or rejection_allowed is not None
                and rejection_allowed(row)
            )
        ] + [
            (True, row)
            for row in raw
            if _possible_candidate(row, release_policy, parsed_quality=False)
        ]
        candidates.sort(
            key=lambda pair: (
                2
                if re.search(r"pt[ ._-]*br|dublado", str(pair[1].get("title", "")), re.I)
                else int(bool(re.search(r"dual|multi", str(pair[1].get("title", "")), re.I))),
                pair[1].get("seeders") if type(pair[1].get("seeders")) is int else 0,
            ),
            reverse=True,
        )
        cursor_key = (arr_url, str(media.get("id")), season)
        offset = self._description_cursors.get(cursor_key, 0) % len(candidates) if candidates else 0
        indexed_candidates = list(enumerate(candidates))
        indexed_candidates = indexed_candidates[offset:] + indexed_candidates[:offset]
        output = native
        description_budget = self.max_descriptions
        parse_budget = self.max_candidates
        for candidate_index, (supplemental, row) in indexed_candidates:
            if not isinstance(row.get("title"), str):
                continue
            try:
                parse_key = (f"{arr_url}/api/v3/parse", (("title", row["title"]),))
                cached = self._cache.get(parse_key)
                if not cached or cached[0] <= time.monotonic():
                    if parse_budget <= 0:
                        complete = False
                        continue
                    parse_budget -= 1
                parsed = await self._get(
                    f"{arr_url}/api/v3/parse",
                    headers=arr_headers,
                    params={"title": row["title"]},
                    ttl=43200,
                )
                if not isinstance(parsed, dict):
                    raise ValueError("invalid native parse")
                detail = parsed.get("parsedMovieInfo" if season is None else "parsedEpisodeInfo")
                if not isinstance(detail, dict):
                    continue
                if supplemental:
                    alias_title = _localized_series_parse_title(
                        row, parsed, media, localized_title, season,
                    )
                    if _native_alias_retry(row) and alias_title is None:
                        continue
                    if alias_title is not None:
                        parse_key = (f"{arr_url}/api/v3/parse", (("title", alias_title),))
                        cached = self._cache.get(parse_key)
                        if not cached or cached[0] <= time.monotonic():
                            if parse_budget <= 0:
                                complete = False
                                continue
                            parse_budget -= 1
                        proof = await self._get(
                            f"{arr_url}/api/v3/parse", headers=arr_headers,
                            params={"title": alias_title}, ttl=43200,
                        )
                        if not isinstance(proof, dict) or not _same_series_release_facts(
                            detail, proof.get("parsedEpisodeInfo"),
                        ):
                            continue
                        parsed = proof
                        detail = parsed["parsedEpisodeInfo"]
                    if not _identity(parsed, media, season=season):
                        continue
                    if require_complete_season_pack or alias_title is not None:
                        # Alias packs can enter the early per-season cache before
                        # a complete-pack search. Prove their whole episode set
                        # now so a later call cannot mistake them for native rows.
                        if episode_catalog is None:
                            episode_catalog = await self._get(
                                f"{arr_url}/api/v3/episode", headers=arr_headers,
                                params={"seriesId": media["id"]}, ttl=60,
                            )
                        if not _complete_season_identity(
                            parsed, media, season, episode_catalog,
                        ):
                            continue
                    if not _possible_candidate(
                        row | {"quality": detail.get("quality")},
                        release_policy,
                        parsed_quality=True,
                    ):
                        continue
                    mapping = next(
                        (
                            value
                            for value in native_indexers
                            if isinstance(value, dict)
                            and _indexer(value.get("name")) == _indexer(row.get("indexer"))
                            and type(value.get("id")) is int
                        ),
                        None,
                    )
                    if mapping is None:
                        complete = False
                        continue
                    row.update(
                        indexerId=mapping["id"],
                        indexer=mapping["name"],
                        quality=detail.get("quality"),
                        rejected=False,
                        rejections=[],
                        requiresNativeDecision=True,
                        audioLanguagesInferred=True,
                    )
                    if season is None:
                        row["movieId"] = media["id"]
                    else:
                        row.update(
                            seriesId=media["id"],
                            seasonNumber=season,
                            episodeIds=[item["id"] for item in parsed["episodes"]],
                            episodeNumbers=[item["episodeNumber"] for item in parsed["episodes"]],
                            fullSeason=detail.get("fullSeason", False),
                        )
                        if alias_title is not None:
                            row["tvdbId"] = media["tvdbId"]
                            imdb = media.get("imdbId")
                            if isinstance(imdb, str) and re.fullmatch(r"tt[0-9]+", imdb):
                                row["imdbId"] = imdb
                    output.append(row)
                row["audioEvidence"] = [
                    {"source": "arr-parser", "languages": detail.get("languages", [])}
                ]
                if _indexer(row.get("indexer")) == "amigosshare":
                    languages = qualified_title_audio_languages(
                        row, original_language=media.get("originalLanguage"),
                    )
                    if languages:
                        row["audioEvidence"].append(
                            {"source": "title", "languages": sorted(languages)}
                        )
                    # ASC metadata arrives through the authenticated indexer.
                    # Its private pages do not consume public description work.
                    continue
                description_url = row.get("infoUrl") or row.get("commentUrl")
                if description_url:
                    cached = self._cache.get(("description", description_url))
                    if not cached or cached[0] <= time.monotonic():
                        if (
                            self._cooldowns.get(("description", description_url), 0)
                            > time.monotonic()
                        ):
                            complete = False
                            continue
                        if description_url not in self._description_tasks:
                            if self._description_tasks or description_budget <= 0:
                                complete = False
                                continue
                            description_budget -= 1
                            if (len(self._description_cursors) >= 128
                                    and cursor_key not in self._description_cursors):
                                self._description_cursors.pop(next(iter(self._description_cursors)))
                            self._description_cursors[cursor_key] = (
                                candidate_index + 1
                            ) % len(candidates)
                    languages = await self._description(description_url)
                    if languages:
                        row["audioEvidence"].append(
                            {"source": "description", "languages": sorted(languages)}
                        )
            except (httpx.HTTPError, ValueError, KeyError):
                complete = False
        return DiscoveryResult(output, complete, tuple(dict.fromkeys(truncated_queries)))
