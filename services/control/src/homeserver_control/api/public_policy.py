"""Fail-closed route and native credential parsing for the public ingress."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlsplit

from starlette.datastructures import Headers

_TOKEN = re.compile(r'[A-Za-z0-9._~+\-]{1,2048}\Z')
_AUTH_PART = re.compile(r'\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*"([^"\\\r\n]*)"\s*')
_STATIC = re.compile(r'.+\.(?:js|css|png|jpg|jpeg|svg|ico|webp|woff2?|ttf|json|wasm)\Z')
IMAGE_COOKIE_NAME = '__Host-hs-jf-image'
_ITEM_IMAGE = re.compile(
    r'/items/(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})'
    r'/images/(?:primary|backdrop|banner|thumb|logo|art|disc|box|boxrear|profile|screenshot|menu)'
    r'(?:/[0-9]+)?\Z')


class PublicDenied(Exception):
    def __init__(self, status: int = 403) -> None:
        self.status = status
        super().__init__('Public request denied')


def single_header(headers: Headers, name: str) -> str:
    values = headers.getlist(name)
    if len(values) > 1 or any(ord(c) < 32 or ord(c) == 127 for v in values for c in v):
        raise PublicDenied()
    return values[0] if values else ''


def target(raw: str, service: str) -> tuple[str, list[tuple[str, str]]]:
    if (not raw.startswith('/') or raw.startswith('//') or len(raw) > 16384
            or any(ord(c) < 33 or ord(c) == 127 for c in raw) or '#' in raw):
        raise PublicDenied()
    parsed = urlsplit(raw)
    # Encoded separators/dots and recursive escapes can normalize differently in
    # Nginx, ASP.NET and Express. Reject those forms instead of guessing a route.
    if re.search(r'%(?:2f|5c|2e|25|00)', parsed.path, re.IGNORECASE):
        raise PublicDenied()
    path = unquote(parsed.path, errors='strict')
    if ('\\' in path or ';' in path or '//' in path
            or any(part in {'.', '..'} for part in path.split('/'))
            or any(ord(c) < 32 for c in path)):
        raise PublicDenied()
    path = path.rstrip('/').lower() or '/'
    if service == 'jellyfin' and (path == '/jellyfin' or path.startswith('/jellyfin/')):
        path = path[len('/jellyfin'):] or '/'
    try:
        query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=256)
    except ValueError as error:
        raise PublicDenied() from error
    return path, query


def jellyfin_token(headers: Headers, query: list[tuple[str, str]]) -> str | None:
    tokens: list[str] = []
    for name in ('authorization', 'x-emby-authorization'):
        auth = single_header(headers, name)
        if not auth:
            continue
        if not auth.lower().startswith('mediabrowser '):
            raise PublicDenied()
        seen: set[str] = set()
        remaining = auth[len('MediaBrowser '):]
        while remaining:
            match = _AUTH_PART.match(remaining)
            if not match or match[1].lower() in seen:
                raise PublicDenied()
            key, value = match[1].lower(), match[2]
            seen.add(key)
            if key == 'token':
                tokens.append(value)
            remaining = remaining[match.end():]
            if remaining:
                if not remaining.startswith(',') or not remaining[1:].strip():
                    raise PublicDenied()
                remaining = remaining[1:]
    legacy = single_header(headers, 'x-emby-token')
    if legacy:
        tokens.append(legacy)
    query_tokens = [value for key, value in query if key in {'api_key', 'ApiKey'}]
    if len(query_tokens) > 1 or any(
        key.lower().replace('_', '').replace('-', '') in {'apikey', 'token', 'accesstoken'}
        and key not in {'api_key', 'ApiKey'} for key, _ in query
    ):
        raise PublicDenied()
    tokens.extend(query_tokens)
    if any(not _TOKEN.fullmatch(value) for value in tokens) or len(set(tokens)) > 1:
        raise PublicDenied()
    return tokens[0] if tokens else None


def token_header(token: str) -> dict[str, str]:
    if not _TOKEN.fullmatch(token):
        raise PublicDenied()
    return {'Authorization': 'MediaBrowser Client="HomeServer public access", '
            f'Device="Public ingress", DeviceId="public-access", Version="1", Token="{token}"'}


def jellyfin_image(path: str, method: str) -> bool:
    """Only native item image reads can consume the browser image cookie."""
    return method in {'GET', 'HEAD'} and bool(_ITEM_IMAGE.fullmatch(path))


def jellyfin_image_cookie(headers: Headers) -> str | None:
    raw = single_header(headers, 'cookie')
    if len(raw) > 8192:
        raise PublicDenied()
    tokens = []
    for part in raw.split(';'):
        name, separator, value = part.strip().partition('=')
        if name.strip() != IMAGE_COOKIE_NAME:
            continue
        if name != IMAGE_COOKIE_NAME or not separator or not _TOKEN.fullmatch(value):
            raise PublicDenied()
        tokens.append(value)
    if len(tokens) > 1:
        raise PublicDenied()
    return tokens[0] if tokens else None


def anonymous(service: str, path: str, method: str) -> bool:
    if method not in {'GET', 'HEAD'}:
        return False
    if service == 'jellyfin':
        return path in {
            '/', '/web', '/web/index.html', '/system/info/public', '/system/ping',
            '/brand/favicon.ico', '/web/manifest.json',
            # Exact read-only theme assets; no Branding configuration or HTML wildcard.
            '/branding/css', '/web/ui/spotlight.html',
        } or (path.startswith('/web/') and bool(_STATIC.fullmatch(path)))
    return (path in {'/', '/login', '/api/v1/settings/public', '/favicon.ico',
                     '/manifest.json', '/site.webmanifest', '/logo_full.svg', '/logo_stacked.svg'}
            or (path.startswith(('/_next/static/', '/images/'))
                and bool(_STATIC.fullmatch(path))))


def permitted(service: str, path: str, method: str) -> bool:
    if anonymous(service, path, method):
        return True
    if service == 'jellyfin':
        if path == '/users/public':
            return False
        if method in {'GET', 'HEAD'}:
            return bool(re.fullmatch(
                r'/(?:items(?:/[^/]+(?:/(?:images/[^/]+(?:/\d+)?|intros|localtrailers|'
                r'specialfeatures|playbackinfo|ancestors|similar|criticreviews|theme(?:songs|videos)))?)?'
                r'|users/[^/]+(?:/(?:items(?:/.*)?|views|groupingoptions|images/[^/]+))?'
                r'|userviews(?:/groupingoptions)?'
                r'|useritems/resume|shows/(?:nextup|upcoming|[^/]+/(?:episodes|seasons))'
                r'|videos/[^/]+/(?:stream(?:\.[a-z0-9]+)?|master\.m3u8|main\.m3u8|live\.m3u8|'
                r'hls[1]?/.*|[^/]+/subtitles/\d+/(?:\d+/)?(?:stream\.[a-z0-9]+|subtitles\.m3u8)'
                r'|subtitles/.*|additionalparts|attachments/.*|trickplay/.*)'
                r'|audio/[^/]+/(?:stream(?:\.[a-z0-9]+)?|master\.m3u8|main\.m3u8|hls[1]?/.*)'
                r'|fallbackfont/fonts(?:/[^/]+)?|sessions|socket'
                r'|artists(?:/.*)?|genres(?:/.*)?|musicgenres(?:/.*)?'
                r'|studios(?:/.*)?|persons(?:/.*)?|years|search/hints|suggestions'
                r'|displaypreferences/[^/]+|localization/(?:cultures|countries|options)'
                r'|channels(?:/[^/]+/items)?|livetv/(?:channels(?:/[^/]+)?|'
                r'programs(?:/[^/]+)?|recordings(?:/[^/]+)?))', path))
        if method == 'POST':
            return bool(re.fullmatch(
                r'/(?:items/[^/]+/playbackinfo|sessions/(?:playing(?:/(?:progress|stopped))?'
                r'|capabilities(?:/full)?|logout)|livestreams/(?:open|close)'
                r'|users/[^/]+/(?:playeditems|favoriteitems)/[^/]+'
                r'|displaypreferences/[^/]+)', path))
        if method == 'DELETE':
            # Stop-transcoding and unmark played/favorite do not delete media.
            return bool(re.fullmatch(
                r'/(?:videos/activencodings|users/[^/]+/(?:playeditems|favoriteitems)/[^/]+)',
                path))
        return False
    if method in {'GET', 'HEAD'}:
        return bool(re.fullmatch(
            r'/(?:discover(?:/.*)?|requests|movie/\d+|tv/\d+|person/\d+|search|profile'
            r'|avatarproxy/[a-z0-9-]+'
            r'|imageproxy/(?:tmdb|tvdb)/[a-z0-9_./-]+\.(?:jpg|jpeg|png|webp)'
            r'|api/v1/(?:auth/me|search|settings/discover|media|discover(?:/.*)?|movie/\d+(?:/.*)?'
            r'|tv/\d+(?:/.*)?|person/\d+(?:/.*)?|request(?:/(?:\d+|count))?'
            r'|service/(?:(?:radarr|sonarr)(?:/\d+)?|sonarr/lookup/\d+)'
            r'|user/\d+(?:/(?:requests|quota|watchlist|settings/main))?'
            r'|genres(?:/.*)?|languages|regions|watchproviders(?:/.*)?'
            r'|collection/\d+|studio/\d+|network/\d+))', path))
    if method == 'POST' and re.fullmatch(r'/api/v1/request/[1-9][0-9]*/(?:approve|decline)', path):
        # Native Seerr still requires MANAGE_REQUESTS on these two actions.
        return True
    return method == 'POST' and path in {
        '/api/v1/request', '/api/v1/auth/logout', '/api/v1/overriderule/advancedrequest',
    }
