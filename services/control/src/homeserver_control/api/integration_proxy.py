"""Bounded Seerr v3.5 integration with fixed native Arr APIs.

Contracts were checked against seerr-team/seerr v3.5.0 server/api/servarr.
This process has no credentials of its own and never loads project settings.
Run with --factory --no-proxy-headers --no-access-log on internal port 8787.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

UPSTREAMS = {
    'radarr': 'http://radarr:7878/radarr/api/v3',
    'sonarr': 'http://sonarr:8989/sonarr/api/v3',
}
ROOTS = {'radarr': '/data/media/movies', 'sonarr': '/data/media/tv'}
MAX_BODY = 128 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
MOVIE_ADD = frozenset({
    'title', 'tmdbId', 'year', 'titleSlug', 'qualityProfileId', 'profileId',
    'minimumAvailability', 'rootFolderPath', 'monitored', 'tags', 'addOptions',
})
SERIES_ADD = frozenset({
    'title', 'tvdbId', 'qualityProfileId', 'languageProfileId', 'rootFolderPath',
    'monitored', 'monitorNewItems', 'seasonFolder', 'seriesType', 'seasons', 'tags', 'addOptions',
})


class Denied(Exception):
    def __init__(self, status: int = 403) -> None:
        self.status = status
        super().__init__('Integration request denied')


def positive(value) -> int:
    if type(value) is not int or not 0 < value < 2**31:
        raise Denied()
    return value


def numeric(value: str) -> str:
    if not re.fullmatch(r'[1-9][0-9]{0,9}', value):
        raise Denied()
    positive(int(value))
    return value


def ids(values, *, limit: int = 1024) -> list[int]:
    if not isinstance(values, list) or len(values) > limit:
        raise Denied()
    result = [positive(value) for value in values]
    if len(set(result)) != len(result):
        raise Denied()
    return result


def object_json(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        result = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise Denied(400) from error
    if not isinstance(result, dict):
        raise Denied(400)
    return result


def parse_request(request: Request) -> tuple[str, str, dict[str, str], str]:
    try:
        raw = request.scope.get('raw_path', b'').decode('ascii')
        query = request.scope.get('query_string', b'').decode('ascii')
    except UnicodeError as error:
        raise Denied() from error
    if len(raw) > 256 or len(query) > 4096:
        raise Denied()
    match = re.fullmatch(r'/(radarr|sonarr)/api/v3/([A-Za-z]+(?:/[A-Za-z0-9]+)?)', raw)
    if not match:
        raise Denied()
    service, path = match[1], '/' + match[2].lower()
    try:
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=8,
                          strict_parsing=True, errors='strict')
    except (ValueError, UnicodeError) as error:
        raise Denied() from error
    params = dict(pairs)
    if len(params) != len(pairs):
        raise Denied()
    tokens = request.headers.getlist('x-api-key')
    if 'apikey' in params:
        tokens.append(params.pop('apikey'))
    if len(tokens) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', tokens[0]):
        raise Denied()
    if request.headers.get('authorization') or request.headers.get('x-http-method-override'):
        raise Denied()
    entity, external = ('movie', 'tmdbId') if service == 'radarr' else ('series', 'tvdbId')
    if request.method == 'GET':
        if path in {'/system/status', '/qualityprofile', '/rootfolder', '/tag'}:
            allowed = set()
        elif path == '/languageprofile' and service == 'sonarr':
            allowed = set()
        elif path == '/' + entity:
            allowed = {external}
            if external in params:
                numeric(params[external])
        elif re.fullmatch('/' + entity + r'/[1-9][0-9]{0,9}', path):
            numeric(path.rsplit('/', 1)[1])
            allowed = set()
        elif path == '/' + entity + '/lookup':
            allowed = {'term'}
            term = params.get('term', '')
            prefix = 'tmdb:' if service == 'radarr' else 'tvdb:'
            if term.startswith(prefix):
                numeric(term[len(prefix):])
            elif (service != 'sonarr' or not 1 <= len(term) <= 200
                  or any(ord(c) < 32 or c in '/\\' for c in term)
                  or ':' in term):
                raise Denied()
        elif path == '/episode' and service == 'sonarr':
            allowed = {'seriesId'}
            numeric(params.get('seriesId', ''))
        elif path == '/queue':
            allowed = {'includeEpisode'}
            if params.get('includeEpisode', 'true') != 'true':
                raise Denied()
        else:
            raise Denied()
        if set(params) - allowed:
            raise Denied()
    elif (request.method in {'POST', 'PUT'} and path == '/' + entity
          or request.method == 'PUT' and service == 'sonarr' and path == '/episode/monitor'):
        if params:
            raise Denied()
    else:
        raise Denied()
    return service, path, params, tokens[0]


class IntegrationProxy:
    def __init__(self, client: httpx.AsyncClient | None, trusted_peer_ips: tuple[str, ...]):
        self.client = client or httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=10,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
        self.owns_client = client is None
        self.static_peers = {str(ipaddress.ip_address(value)) for value in trusted_peer_ips}
        self.peers: set[str] = set()
        self.peers_until = 0.0
        self.peer_lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(2)

    async def authorized_peer(self, request: Request) -> bool:
        try:
            address = str(ipaddress.ip_address(request.client.host)) if request.client else ''
        except ValueError:
            return False
        if self.static_peers:
            return address in self.static_peers
        async with self.peer_lock:
            if time.monotonic() >= self.peers_until:
                try:
                    rows = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                        'seerr', 5055, type=socket.SOCK_STREAM), timeout=2)
                    self.peers = {str(ipaddress.ip_address(row[4][0])) for row in rows}
                    ttl = 30
                except (OSError, ValueError, TimeoutError):
                    self.peers, ttl = set(), 5
                self.peers_until = time.monotonic() + ttl
            return address in self.peers

    async def native(self, service, path, key, *, method='GET', params=None, body=None):
        async with self.client.stream(
            method, UPSTREAMS[service] + path, headers={'X-Api-Key': key},
            params=params, json=body, follow_redirects=False,
        ) as response:
            if response.status_code in {401, 403}:
                raise Denied(response.status_code)
            if response.status_code >= 300:
                raise Denied(502)
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise Denied(502)
                chunks.append(chunk)
            try:
                result = json.loads(b''.join(chunks))
            except (ValueError, UnicodeError, RecursionError) as error:
                raise Denied(502) from error
            return result, response.status_code

    async def collection(self, service, path, key) -> list[dict]:
        result, _ = await self.native(service, path, key)
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise Denied(502)
        return result

    async def validate_choices(self, service, body, key):
        if 'qualityProfileId' in body or 'profileId' in body:
            profiles = await self.collection(service, '/qualityprofile', key)
            valid = {row.get('id') for row in profiles}
            for field in ('qualityProfileId', 'profileId'):
                if field in body and positive(body[field]) not in valid:
                    raise Denied()
            if ('qualityProfileId' in body and 'profileId' in body
                    and body['qualityProfileId'] != body['profileId']):
                raise Denied()
        if 'languageProfileId' in body:
            profiles = await self.collection(service, '/languageprofile', key)
            if positive(body['languageProfileId']) not in {row.get('id') for row in profiles}:
                raise Denied()
        if 'tags' in body:
            requested = ids(body['tags'], limit=64)
            if requested:
                existing = await self.collection(service, '/tag', key)
                if not set(requested) <= {row.get('id') for row in existing}:
                    raise Denied()

    def validate_fields(self, service, body):
        if 'rootFolderPath' in body and body['rootFolderPath'] != ROOTS[service]:
            raise Denied()
        if 'title' in body and (not isinstance(body['title'], str)
                               or not 1 <= len(body['title']) <= 500
                               or body['title'] in {'.', '..'}
                               or any(ord(c) < 32 or c in '/\\' for c in body['title'])):
            raise Denied()
        for field in ('monitored', 'seasonFolder'):
            if field in body and type(body[field]) is not bool:
                raise Denied()
        if 'year' in body and (type(body['year']) is not int or not 0 <= body['year'] <= 2300):
            raise Denied()
        for field, allowed in {
            'minimumAvailability': {'announced', 'inCinemas', 'released', 'preDB'},
            'monitorNewItems': {'all', 'none'}, 'seriesType': {'standard', 'daily', 'anime'},
        }.items():
            if field in body and (not isinstance(body[field], str) or body[field] not in allowed):
                raise Denied()
        if 'addOptions' in body:
            options = body['addOptions']
            allowed = ({'searchForMovie'} if service == 'radarr' else {
                'ignoreEpisodesWithFiles', 'ignoreEpisodesWithoutFiles',
                'searchForMissingEpisodes'})
            if not isinstance(options, dict) or set(options) - allowed:
                raise Denied()
            for field, flag in options.items():
                if type(flag) is not bool or field.startswith('search') and flag:
                    raise Denied()

    @staticmethod
    def season_changes(requested, existing, *, new: bool) -> list[dict]:
        if not isinstance(requested, list) or not isinstance(existing, list):
            raise Denied()
        if len(requested) > 1000 or any(not isinstance(row, dict) for row in existing):
            raise Denied()
        current = {row.get('seasonNumber'): row for row in existing}
        result, seen = [], set()
        for row in requested:
            if not isinstance(row, dict):
                raise Denied()
            number = row.get('seasonNumber')
            if (type(number) is not int or number < 0 or number not in current or number in seen
                    or type(row.get('monitored')) is not bool):
                raise Denied()
            seen.add(number)
            baseline = current[number]
            if (set(row) - {'seasonNumber', 'monitored'} if new else any(
                    value != baseline.get(key)
                    for key, value in row.items() if key != 'monitored')):
                raise Denied()
            if not new and baseline.get('monitored') and not row['monitored']:
                raise Denied()
            result.append(({} if new else deepcopy(baseline)) | {
                'seasonNumber': number, 'monitored': row['monitored']})
        if seen != set(current):
            raise Denied()
        return result

    @staticmethod
    def safe_existing(service, body):
        path = body.get('path')
        if (not isinstance(path, str) or not path.startswith(ROOTS[service] + '/')
                or any(part in {'', '.', '..'} for part in path.split('/')[1:])
                or '\\' in path or any(ord(c) < 32 for c in path)):
            raise Denied()

    async def mutation(self, service, path, method, body, key) -> dict:
        if path == '/episode/monitor':
            return await self.monitor_episodes(body, key)
        entity, external = ('movie', 'tmdbId') if service == 'radarr' else ('series', 'tvdbId')
        self.validate_fields(service, body)
        if method == 'POST':
            allowed = MOVIE_ADD if service == 'radarr' else SERIES_ADD
            if set(body) - allowed or not {'title', external, 'qualityProfileId',
                                          'rootFolderPath'} <= set(body):
                raise Denied()
            positive(body[external])
            if 'titleSlug' in body and body['titleSlug'] != str(body[external]):
                raise Denied()
            roots = await self.collection(service, '/rootfolder', key)
            if ROOTS[service] not in {row.get('path') for row in roots}:
                raise Denied()
            result = deepcopy(body)
            if service == 'sonarr':
                matches, _ = await self.native(service, '/series/lookup', key,
                                               params={'term': f"tvdb:{body[external]}"})
                if not isinstance(matches, list):
                    raise Denied(502)
                baseline = next((row for row in matches if isinstance(row, dict)
                                 and row.get(external) == body[external]), None)
                if baseline is None:
                    raise Denied()
                result['seasons'] = self.season_changes(
                    body.get('seasons'), baseline.get('seasons'), new=True)
            await self.validate_choices(service, result, key)
            # Enforce preventSearch even if a future client omits addOptions.
            options = result.setdefault('addOptions', {})
            options['searchForMovie' if service == 'radarr' else 'searchForMissingEpisodes'] = False
            return result
        identifier = positive(body.get('id'))
        baseline, _ = await self.native(service, f'/{entity}/{identifier}', key)
        if not isinstance(baseline, dict) or baseline.get('id') != identifier:
            raise Denied(502)
        self.safe_existing(service, baseline)
        allowed = ({'monitored', 'tags', 'qualityProfileId', 'profileId', 'title', 'titleSlug',
                    'year', 'minimumAvailability', 'rootFolderPath', 'addOptions'}
                   if service == 'radarr' else {'monitored', 'tags', 'seasons'})
        for field, value in body.items():
            if field not in allowed and (field not in baseline or value != baseline[field]):
                raise Denied()
        if body.get(external, baseline.get(external)) != baseline.get(external):
            raise Denied()
        if (service == 'radarr' and 'titleSlug' in body
                and body['titleSlug'] != str(baseline.get(external))):
            raise Denied()
        if baseline.get('monitored') and body.get('monitored') is False:
            raise Denied()
        result = deepcopy(baseline)
        for field in allowed & body.keys():
            result[field] = deepcopy(body[field])
        if service == 'sonarr' and 'seasons' in body:
            result['seasons'] = self.season_changes(
                body['seasons'], baseline.get('seasons'), new=False)
        await self.validate_choices(service, body, key)
        # Never replay stale native addOptions that can trigger acquisition.
        result['addOptions'] = {
            'searchForMovie' if service == 'radarr' else 'searchForMissingEpisodes': False}
        return result

    async def monitor_episodes(self, body, key):
        if set(body) != {'episodeIds', 'monitored'} or body['monitored'] is not True:
            raise Denied()
        requested = ids(body['episodeIds'])
        if not requested:
            raise Denied()
        first, _ = await self.native('sonarr', f'/episode/{requested[0]}', key)
        if not isinstance(first, dict):
            raise Denied(502)
        series_id = positive(first.get('seriesId'))
        series, _ = await self.native('sonarr', f'/series/{series_id}', key)
        if not isinstance(series, dict) or series.get('monitored') is not True:
            raise Denied()
        self.safe_existing('sonarr', series)
        seasons = {row['seasonNumber'] for row in series.get('seasons', [])
                   if isinstance(row, dict) and row.get('monitored') is True}
        episodes, _ = await self.native('sonarr', '/episode', key, params={'seriesId': series_id})
        if not isinstance(episodes, list):
            raise Denied(502)
        allowed = {row.get('id') for row in episodes if isinstance(row, dict)
                   and row.get('seriesId') == series_id and row.get('seasonNumber') in seasons}
        if not set(requested) <= allowed:
            raise Denied()
        return {'episodeIds': requested, 'monitored': True}

    async def handle(self, request):
        if not await self.authorized_peer(request):
            raise Denied()
        if request.url.path.startswith('/jellyfin/'):
            from .integration_jellyfin import handle_jellyfin

            return await handle_jellyfin(self.client, request)
        service, path, params, key = parse_request(request)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY:
                raise Denied(413)
            chunks.append(chunk)
        raw = b''.join(chunks)
        if request.method == 'GET':
            if raw:
                raise Denied(400)
            result, status = await self.native(service, path, key, params=params)
            if (service == 'sonarr' and path == '/series/lookup'
                    and params.get('term', '').startswith('tvdb:')):
                if (not isinstance(result, list) or len(result) > 1
                        or any(not isinstance(row, dict) for row in result)):
                    raise Denied(502)
                tvdb_id = int(params['term'][len('tvdb:'):])
                for index, row in enumerate(result):
                    identifier = row.get('id')
                    if identifier is None or type(identifier) is int and identifier == 0:
                        continue
                    if (type(identifier) is not int or not 0 < identifier < 2**31
                            or type(row.get('tvdbId')) is not int or row['tvdbId'] != tvdb_id):
                        raise Denied(502)
                    canonical, _ = await self.native(service, f'/series/{identifier}', key)
                    if (not isinstance(canonical, dict) or canonical.get('id') != identifier
                            or type(canonical.get('tvdbId')) is not int
                            or canonical['tvdbId'] != tvdb_id):
                        raise Denied(502)
                    self.safe_existing(service, canonical)
                    # Seerr clones lookup results for updates. Existing entities
                    # must use the same native baseline as immutable-field guards.
                    result[index] = canonical
            if path == '/rootfolder':
                if not isinstance(result, list):
                    raise Denied(502)
                result = [row for row in result if isinstance(row, dict)
                          and row.get('path') == ROOTS[service]]
        else:
            content_types = request.headers.getlist('content-type')
            if (len(content_types) != 1
                    or content_types[0].split(';', 1)[0].strip().lower() != 'application/json'
                    or request.headers.get('content-encoding')):
                raise Denied(400)
            body = await self.mutation(service, path, request.method, object_json(raw), key)
            result, status = await self.native(service, path, key, method=request.method, body=body)
        return JSONResponse(result, status_code=status)


def create_app(*, client: httpx.AsyncClient | None = None,
               trusted_peer_ips: tuple[str, ...] = ()) -> FastAPI:
    """Factory; fixture dependency injection is unavailable through environment variables."""
    proxy = IntegrationProxy(client, trusted_peer_ips)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if proxy.owns_client:
                await proxy.client.aclose()

    app = FastAPI(title='HomeServer Seerr integration', docs_url=None, redoc_url=None,
                  openapi_url=None, redirect_slashes=False, lifespan=lifespan)

    @app.api_route('/{path:path}',
                   methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
    async def forward(request: Request, path: str):
        try:
            async def bounded():
                async with proxy.slots:
                    return await proxy.handle(request)
            return await asyncio.wait_for(bounded(), timeout=30)
        except Denied as error:
            return JSONResponse({'detail': 'Integration request denied'}, status_code=error.status)
        except (httpx.HTTPError, TimeoutError, OSError):
            return JSONResponse({'detail': 'Integration unavailable'}, status_code=503)

    return app
