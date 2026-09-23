"""Fetch a bounded SRT for a verified media release from SubDL."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

import httpx

from homeserver_control.domain.subtitle_content import MAX_SRT_BYTES, normalize_srt

_DOWNLOAD_PATH = re.compile(r"/subtitle/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+")
_EPISODE_TAG = re.compile(r"(?<![a-z0-9])s(\d{1,2})e(\d{1,3})(?![a-z0-9])", re.I)


def _release_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _episode_parts(value: str) -> tuple[str, int, int, str] | None:
    matches = list(_EPISODE_TAG.finditer(value))
    if len(matches) != 1:
        return None
    tag = matches[0]
    return (
        _release_key(value[:tag.start()]),
        int(tag.group(1)), int(tag.group(2)), value[tag.end():].lower(),
    )


def _source_family(release_tail: str) -> str | None:
    for family, pattern in (
        ("webrip", r"(?<![a-z0-9])web[._ -]?rip(?![a-z0-9])"),
        ("web", r"(?<![a-z0-9])web(?:[._ -]?dl)?(?![a-z0-9])"),
        ("bluray", r"(?<![a-z0-9])(?:blu[._ -]?ray|bd(?:rip|remux))(?![a-z0-9])"),
        ("hdtv", r"(?<![a-z0-9])(?:hd|p)d?tv(?![a-z0-9])"),
        ("dvd", r"(?<![a-z0-9])dvd(?![a-z0-9])"),
    ):
        if re.search(pattern, release_tail):
            return family
    return None


def _compatible_episode_release(
    candidate: str, requested: str, season: int, episode: int
) -> bool:
    wanted = _episode_parts(requested)
    offered = _episode_parts(candidate)
    if wanted is None or offered is None:
        return False
    title, wanted_season, wanted_episode, wanted_tail = wanted
    offered_title, offered_season, offered_episode, offered_tail = offered
    family = _source_family(wanted_tail)
    return (
        bool(title) and offered_title == title
        and (wanted_season, wanted_episode) == (season, episode)
        and (offered_season, offered_episode) == (season, episode)
        and family is not None and _source_family(offered_tail) == family
    )


class SubDLSource:
    def __init__(self, *, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("SubDL API key is required")
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        self._search_cache: dict[
            tuple[int, int | None, int | None, str], tuple[float, list[object]]
        ] = {}

    async def fetch(
        self, *, tmdb_id: int, release_title: str,
        season: int | None = None, episode: int | None = None,
        language: str = "BR_PT",
    ) -> bytes | None:
        if language not in {"BR_PT", "EN"}:
            raise ValueError("unsupported SubDL language")
        is_tv = season is not None or episode is not None
        if tmdb_id <= 0 or not release_title or is_tv and (
            season is None or episode is None or season < 0 or episode <= 0
        ):
            raise ValueError("invalid SubDL media identity")
        params: dict[str, str | int] = {
            "api_key": self.api_key, "tmdb_id": tmdb_id,
            "type": "tv" if is_tv else "movie", "languages": language,
            "subs_per_page": 30, "unpack": 1, "releases": 1,
        }
        if is_tv:
            params.update(season_number=season, episode_number=episode)
        cache_key = (tmdb_id, season, episode, language)
        cached = self._search_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            subtitles = cached[1]
        else:
            try:
                response = await self.client.get(
                    "https://api.subdl.com/api/v1/subtitles", params=params,
                    timeout=20.0, follow_redirects=False,
                )
                if response.status_code != 200:
                    return None
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                return None
            if not isinstance(payload, dict) or payload.get("status") is not True:
                return None
            results = payload.get("results")
            subtitles = payload.get("subtitles")
            if (
                not isinstance(results, list) or not results
                or not isinstance(results[0], dict)
                or results[0].get("tmdb_id") != tmdb_id
                or results[0].get("type") != ("tv" if is_tv else "movie")
                or not isinstance(subtitles, list)
            ):
                return None
            self._search_cache[cache_key] = (time.monotonic() + 900, subtitles)
        wanted = _release_key(release_title)
        candidates: list[tuple[int, int, str]] = []
        for subtitle in subtitles:
            if not isinstance(subtitle, dict) or subtitle.get("language") not in {None, language}:
                continue
            if is_tv and (
                subtitle.get("season") != season or subtitle.get("episode") != episode
            ):
                continue
            files = subtitle.get("unpack_files")
            if not isinstance(files, list):
                continue
            for item in files:
                if not isinstance(item, dict) or item.get("language") != language:
                    continue
                if is_tv and (
                    item.get("season") != season or item.get("episode") != episode
                ):
                    continue
                name = item.get("release_name")
                size = item.get("size")
                url = item.get("url")
                if (
                    not isinstance(name, str)
                    or item.get("format") != "srt"
                    or not isinstance(size, int) or isinstance(size, bool)
                    or not 30 <= size <= MAX_SRT_BYTES
                    or not isinstance(url, str)
                ):
                    continue
                parsed = urlsplit(url)
                if (
                    parsed.scheme or parsed.netloc or parsed.fragment
                    or not _DOWNLOAD_PATH.fullmatch(parsed.path)
                ):
                    continue
                exact = _release_key(name) == wanted
                if exact or (is_tv and _compatible_episode_release(
                    name, release_title, season, episode
                )):
                    candidates.append((0 if exact else 1, size, parsed.path))
        for _, size, path in sorted(candidates, key=lambda candidate: candidate[0]):
            try:
                async with self.client.stream(
                    "GET", "https://dl.subdl.com" + path,
                    follow_redirects=False, timeout=20.0,
                ) as download:
                    if download.status_code != 200:
                        continue
                    content_length = download.headers.get("content-length")
                    if content_length and int(content_length) > MAX_SRT_BYTES:
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in download.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_SRT_BYTES:
                            break
                        chunks.append(chunk)
                content = b"".join(chunks)
                if total == size:
                    normalized = normalize_srt(content)
                    if normalized is not None:
                        return normalized
            except (httpx.HTTPError, ValueError):
                continue
        return None
