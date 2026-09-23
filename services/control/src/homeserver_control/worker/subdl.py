"""Fetch only an exact-release Brazilian Portuguese SRT from SubDL."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

import httpx

from homeserver_control.domain.subtitle_content import MAX_SRT_BYTES, valid_srt

_DOWNLOAD_PATH = re.compile(r"/subtitle/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+")


def _release_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


class SubDLSource:
    def __init__(self, *, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        if not api_key:
            raise ValueError("SubDL API key is required")
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
        self._search_cache: dict[
            tuple[int, int | None, int | None], tuple[float, list[object]]
        ] = {}

    async def fetch(
        self, *, tmdb_id: int, release_title: str,
        season: int | None = None, episode: int | None = None,
    ) -> bytes | None:
        is_tv = season is not None or episode is not None
        if tmdb_id <= 0 or not release_title or is_tv and (
            season is None or episode is None or season < 0 or episode <= 0
        ):
            raise ValueError("invalid SubDL media identity")
        params: dict[str, str | int] = {
            "api_key": self.api_key, "tmdb_id": tmdb_id,
            "type": "tv" if is_tv else "movie", "languages": "BR_PT",
            "subs_per_page": 30, "unpack": 1, "releases": 1,
        }
        if is_tv:
            params.update(season_number=season, episode_number=episode)
        cache_key = (tmdb_id, season, episode)
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
        for subtitle in subtitles:
            if not isinstance(subtitle, dict) or subtitle.get("language") != "BR_PT":
                continue
            if is_tv and (
                subtitle.get("season") != season or subtitle.get("episode") != episode
            ):
                continue
            files = subtitle.get("unpack_files")
            if not isinstance(files, list):
                continue
            for item in files:
                if not isinstance(item, dict) or item.get("language") != "BR_PT":
                    continue
                if is_tv and (
                    item.get("season") != season or item.get("episode") != episode
                ):
                    continue
                name = item.get("release_name")
                size = item.get("size")
                url = item.get("url")
                if (
                    not isinstance(name, str) or _release_key(name) != wanted
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
                try:
                    async with self.client.stream(
                        "GET", "https://dl.subdl.com" + parsed.path,
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
                    if total == size and valid_srt(content):
                        return content
                except (httpx.HTTPError, ValueError):
                    continue
        return None
