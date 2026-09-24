"""Fetch a bounded SRT for a verified media release from SubDL."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Sequence
from typing import Literal
from urllib.parse import urlsplit

import httpx

from homeserver_control.domain.subtitle_content import MAX_SRT_BYTES, normalize_srt

_DOWNLOAD_PATH = re.compile(r"/subtitle/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+")
_EPISODE_TAG = re.compile(r"(?<![a-z0-9])s(\d{1,2})e(\d{1,3})(?![a-z0-9])", re.I)
_MOVIE_EN_US_SUFFIX = re.compile(r"[._ -]+en[._ -]?us$", re.I)
_MOVIE_LANGUAGE_SUFFIX = {
    "BR_PT": re.compile(r"[._ -]+(?:pt[._ -]?br|br[._ -]?pt)$", re.I),
    "EN": re.compile(r"[._ -]+(?:en(?:[._ -]?us)?|eng|english)$", re.I),
}
_MOVIE_EDITION_PATTERNS = (
    ("extended", re.compile(r"(?<![a-z0-9])extended(?![a-z0-9])", re.I)),
    ("directors_cut", re.compile(
        r"(?<![a-z0-9])director(?:s|'s)?[._ -]+cut(?![a-z0-9])", re.I
    )),
    ("theatrical", re.compile(r"(?<![a-z0-9])theatrical(?![a-z0-9])", re.I)),
    ("unrated", re.compile(r"(?<![a-z0-9])unrated(?![a-z0-9])", re.I)),
    ("uncut", re.compile(r"(?<![a-z0-9])uncut(?![a-z0-9])", re.I)),
    ("special_edition", re.compile(
        r"(?<![a-z0-9])special[._ -]+edition(?![a-z0-9])", re.I
    )),
    ("final_cut", re.compile(r"(?<![a-z0-9])final[._ -]+cut(?![a-z0-9])", re.I)),
    ("ultimate_cut", re.compile(r"(?<![a-z0-9])ultimate[._ -]+cut(?![a-z0-9])", re.I)),
)
_MOVIE_TIMING_LINE = re.compile(
    r"[ \t]*(\d{2}:\d{2}:\d{2}[,.]\d{3})[ \t]*-->[ \t]*"
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})[ \t]*"
)


def _release_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _movie_editions(value: str) -> frozenset[str]:
    editions = frozenset(
        name for name, pattern in _MOVIE_EDITION_PATTERNS if pattern.search(value)
    )
    return frozenset() if editions == {"theatrical"} else editions


def _positive_finite(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _srt_seconds(value: str) -> float | None:
    hours, minutes, rest = value.split(":")
    seconds, milliseconds = re.split(r"[,.]", rest)
    if int(minutes) >= 60 or int(seconds) >= 60:
        return None
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def _movie_timing_plausible(srt: bytes, duration: float) -> bool:
    text = srt.decode("utf-8-sig")
    latest_start = -1.0
    first_start: float | None = None
    last_end = 0.0
    cues = 0
    middle_cue = False
    upper = duration + 120
    for line in text.splitlines():
        if "-->" not in line:
            continue
        timing = _MOVIE_TIMING_LINE.fullmatch(line)
        if timing is None:
            return False
        start = _srt_seconds(timing.group(1))
        end = _srt_seconds(timing.group(2))
        if (
            start is None or end is None or start < latest_start
            or end <= start or end > upper
        ):
            return False
        if first_start is None:
            first_start = start
        middle_cue |= duration * 0.35 <= start <= duration * 0.65
        latest_start = start
        last_end = end
        cues += 1
    return (
        cues >= 10 and first_start is not None and first_start <= duration * 0.20
        and middle_cue
        and max(duration * 0.85, duration - 480) <= last_end <= upper
    )


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

    async def fetch_movie(
        self, *, tmdb_id: int, release_titles: Sequence[str],
        language: str = "BR_PT", match_mode: Literal["exact", "same_duration"] = "exact",
        movie_duration_seconds: float | None = None, movie_fps: float | None = None,
    ) -> bytes | None:
        """Fetch an SRT for the verified TMDb movie in the requested match mode."""
        if language not in {"BR_PT", "EN"}:
            raise ValueError("unsupported SubDL language")
        if match_mode not in {"exact", "same_duration"}:
            raise ValueError("unsupported movie subtitle match mode")
        if isinstance(tmdb_id, bool) or not isinstance(tmdb_id, int) or tmdb_id <= 0:
            raise ValueError("invalid SubDL media identity")
        if isinstance(release_titles, str) or any(
            not isinstance(title, str) for title in release_titles
        ):
            raise ValueError("invalid movie release titles")
        duration = _positive_finite(movie_duration_seconds)
        if match_mode == "same_duration" and duration is None:
            return None
        wanted = {_release_key(title) for title in release_titles if title}
        editions = frozenset().union(*(_movie_editions(title) for title in release_titles))
        sources = {
            family for title in release_titles
            if (family := _source_family(title.lower())) is not None
        }
        fps = _positive_finite(movie_fps)

        cache_key = (tmdb_id, None, None, language)
        cached = self._search_cache.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            subtitles = cached[1]
        else:
            try:
                response = await self.client.get(
                    "https://api.subdl.com/api/v1/subtitles",
                    params={
                        "api_key": self.api_key, "tmdb_id": tmdb_id, "type": "movie",
                        "languages": language, "subs_per_page": 30, "unpack": 1,
                        "releases": 1,
                    },
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
                or results[0].get("type") != "movie"
                or not isinstance(subtitles, list)
            ):
                return None
            self._search_cache[cache_key] = (time.monotonic() + 900, subtitles)

        candidates: list[tuple[tuple[float, ...], int, str]] = []
        for subtitle in subtitles:
            if not isinstance(subtitle, dict) or subtitle.get("language") not in {None, language}:
                continue
            files = subtitle.get("unpack_files")
            if not isinstance(files, list):
                continue
            for item in files:
                if not isinstance(item, dict) or item.get("language") != language:
                    continue
                size = item.get("size")
                url = item.get("url")
                name = item.get("release_name")
                if (
                    item.get("format") != "srt"
                    or not isinstance(size, int) or isinstance(size, bool)
                    or not 30 <= size <= MAX_SRT_BYTES
                    or not isinstance(url, str)
                    or name is not None and not isinstance(name, str)
                ):
                    continue
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    continue
                if (
                    parsed.scheme or parsed.netloc or parsed.fragment
                    or not _DOWNLOAD_PATH.fullmatch(parsed.path)
                ):
                    continue
                other_language = "EN" if language == "BR_PT" else "BR_PT"
                if name and _MOVIE_LANGUAGE_SUFFIX[other_language].search(name):
                    continue
                bare_name = _MOVIE_LANGUAGE_SUFFIX[language].sub("", name) if name else ""
                exact = bool(name) and (
                    _release_key(name) in wanted or _release_key(bare_name) in wanted
                )
                if match_mode == "exact" and not exact:
                    continue
                if _movie_editions(name or "") != editions:
                    continue
                family = _source_family(name.lower()) if name else None
                candidate_fps = _positive_finite(item.get("fps"))
                if candidate_fps is None:
                    candidate_fps = _positive_finite(subtitle.get("fps"))
                score = (
                    0 if exact else 1,
                    0 if language == "EN" and name and _MOVIE_EN_US_SUFFIX.search(name) else 1,
                    0 if family is not None and family in sources else 1,
                    0 if fps is not None and candidate_fps is not None else 1,
                    abs(fps - candidate_fps) if fps is not None and candidate_fps is not None
                    else math.inf,
                )
                candidates.append((score, size, parsed.path))
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
                if total != size:
                    continue
                normalized = normalize_srt(b"".join(chunks))
                if normalized is not None and (
                    match_mode == "exact" or _movie_timing_plausible(normalized, duration)
                ):
                    return normalized
            except (httpx.HTTPError, ValueError):
                continue
        return None

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
                bare_name = (
                    _MOVIE_LANGUAGE_SUFFIX[language].sub("", name) if not is_tv else name
                )
                exact = _release_key(name) == wanted or (
                    not is_tv and bare_name != name and _release_key(bare_name) == wanted
                )
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
