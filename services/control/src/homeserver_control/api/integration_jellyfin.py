"""Fixed Jellyfin subset used by Seerr v3.5.0, including bounded avatar/logout calls.

Source contracts: seerr-team/seerr v3.5.0 server/api/jellyfin.ts,
server/routes/auth.ts and server/routes/avatarproxy.ts. Bootstrap key creation and
Quick Connect are deliberately unavailable through the integration connection.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import parse_qsl

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from .integration_proxy import MAX_BODY, MAX_RESPONSE, Denied, object_json

BASE = 'http://jellyfin:8096/jellyfin'
GUID = r'(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})'
AUTH_PART = re.compile(r'\s*([A-Za-z]+)="([^"\\\r\n]*)"\s*')


def guid(value: str, *, me: bool = False) -> bool:
    return bool(re.fullmatch(GUID, value)) or me and value == 'Me'


def seerr_device(value: str) -> bool:
    if not 1 <= len(value) <= 512:
        return False
    if value in {'BOT_seerr', 'BOT_jellyseerr'}:
        return True
    try:
        decoded = base64.b64decode(value, validate=True).decode('utf-8')
    except (ValueError, UnicodeError, binascii.Error):
        return False
    # Preserve IDs stored by the pre-migration Jellyseerr 2.7.3 installation.
    return (decoded in {'BOT_seerr', 'BOT_jellyseerr'} or any(
        decoded.startswith(prefix) and len(decoded) > len(prefix)
        for prefix in ('BOT_seerr_', 'BOT_jellyseerr_')
    )) and not any(ord(c) < 32 for c in decoded)


def auth_headers(request: Request, *, authenticated: bool, login: bool = False) -> dict:
    if any(request.headers.get(name) for name in (
        'x-emby-token', 'x-emby-authorization', 'x-api-key', 'x-http-method-override',
    )):
        raise Denied()
    values = request.headers.getlist('authorization')
    if not values and not authenticated and not login:
        return {}
    if len(values) != 1 or len(values[0]) > 4096 or not values[0].startswith('MediaBrowser '):
        raise Denied()
    remaining = values[0][len('MediaBrowser '):]
    fields = {}
    while remaining:
        match = AUTH_PART.match(remaining)
        if not match or match[1] in fields:
            raise Denied()
        fields[match[1]] = match[2]
        remaining = remaining[match.end():]
        if remaining:
            if not remaining.startswith(',') or not remaining[1:].strip():
                raise Denied()
            remaining = remaining[1:]
    required = {'Client', 'Device', 'DeviceId', 'Version'}
    if not required <= fields.keys() or fields.keys() - required - {'Token'}:
        raise Denied()
    if (fields['Client'] != 'Seerr' or fields['Device'] != 'Seerr'
            or not (seerr_device(fields['DeviceId']) or fields['DeviceId'] == 'seerr')
            or not re.fullmatch(r'[A-Za-z0-9._+\-]{1,64}', fields['Version'])):
        raise Denied()
    token = fields.get('Token')
    if (authenticated and not token or login and token
            or token is not None and not re.fullmatch(r'[A-Za-z0-9._~+\-]{1,2048}', token)):
        raise Denied()
    return {'Authorization': 'MediaBrowser ' + ', '.join(
        f'{key}="{value}"' for key, value in fields.items())}


def route(request: Request) -> tuple[str, dict[str, str], str]:
    try:
        raw = request.scope.get('raw_path', b'').decode('ascii')
        query = request.scope.get('query_string', b'').decode('ascii')
        if len(query) > 4096:
            raise Denied()
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=16,
                          strict_parsing=True, errors='strict')
    except (ValueError, UnicodeError) as error:
        raise Denied() from error
    if len(raw) > 256 or not re.fullmatch(r'/jellyfin(?:/[A-Za-z0-9-]+)+', raw):
        raise Denied()
    path, params = raw[len('/jellyfin'):], dict(pairs)
    if len(params) != len(pairs):
        raise Denied()
    if request.method == 'POST' and path == '/Users/AuthenticateByName' and not params:
        return path, params, 'login'
    if request.method == 'DELETE' and path == '/Devices' and set(params) == {'Id'}:
        if not seerr_device(params['Id']):
            raise Denied()
        return path, params, 'device'
    if request.method in {'GET', 'HEAD'} and path == '/UserImage':
        if set(params) != {'UserId'} or not guid(params['UserId']):
            raise Denied()
        return path, params, 'image'
    if request.method != 'GET':
        raise Denied()
    if path == '/System/Info/Public' and not params:
        return path, params, 'public'
    if not params and (path in {'/System/Info', '/Users', '/Library/MediaFolders'}
                       or re.fullmatch(rf'/Users/(?:{GUID}|Me)(?:/Views)?', path)
                       or re.fullmatch(rf'/Shows/{GUID}/Seasons', path)):
        return path, params, 'json'
    if path == '/Items':
        if (set(params) == {'ids', 'fields'} and guid(params['ids'])
                and params['fields'] == 'ProviderIds,MediaSources,Width,Height,IsHD,DateCreated'):
            return path, params, 'json'
        expected = {'SortBy': 'SortName', 'SortOrder': 'Ascending',
                    'IncludeItemTypes': 'Series,Movie,Others', 'Recursive': 'true',
                    'StartIndex': '0', 'collapseBoxSetItems': 'false'}
        if (set(params) == set(expected) | {'ParentId'} and guid(params['ParentId'])
                and all(params[key] == value for key, value in expected.items())):
            return path, params, 'json'
    if (path == '/Items/Latest' and set(params) == {'Limit', 'ParentId', 'userId'}
            and params['Limit'] == '12' and guid(params['ParentId'])
            and guid(params['userId'], me=True)):
        return path, params, 'json'
    if (re.fullmatch(rf'/Shows/{GUID}/Episodes', path)
            and set(params) in ({'seasonId'}, {'seasonId', 'fields'})
            and guid(params['seasonId'])
            and params.get('fields', 'MediaSources') == 'MediaSources'):
        return path, params, 'json'
    raise Denied()


async def native(client, path, *, method='GET', params=None, headers=None, body=None, image=False):
    async with client.stream(method, BASE + path, params=params, headers=headers,
                             json=body, follow_redirects=False) as response:
        if response.status_code >= 300:
            raise Denied(response.status_code if response.status_code in {
                400, 401, 403, 404, 500,
            } else 502)
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > (4 * 1024 * 1024 if image else MAX_RESPONSE):
                raise Denied(502)
            chunks.append(chunk)
        raw = b''.join(chunks)
        if response.status_code == 204:
            return None, 204, {}
        if image:
            media_type = response.headers.get('content-type', '').split(';', 1)[0].lower()
            if media_type not in {
                'image/jpeg', 'image/png', 'image/webp', 'image/gif', 'image/avif',
            }:
                raise Denied(502)
            safe = {'Content-Type': media_type, 'X-Content-Type-Options': 'nosniff'}
            for key in ('last-modified', 'etag'):
                value = response.headers.get(key, '')
                if value and len(value) <= 128 and not any(ord(c) < 32 for c in value):
                    safe[key] = value
            return raw, response.status_code, safe
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise Denied(502) from error
        return data, response.status_code, {}


async def handle_jellyfin(client, request: Request) -> Response:
    path, params, kind = route(request)
    headers = auth_headers(request, authenticated=kind in {'json', 'device'}, login=kind == 'login')
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > (8192 if kind == 'login' else MAX_BODY):
            raise Denied(413)
        chunks.append(chunk)
    raw = b''.join(chunks)
    body = None
    if kind == 'login':
        content_types = request.headers.getlist('content-type')
        if (len(content_types) != 1 or content_types[0].split(';', 1)[0] != 'application/json'
                or request.headers.get('content-encoding')):
            raise Denied(400)
        body = object_json(raw)
        if (set(body) != {'Username', 'Pw'} or not isinstance(body['Username'], str)
                or not 1 <= len(body['Username']) <= 256 or not isinstance(body['Pw'], str)
                or len(body['Pw']) > 1024 or any(ord(c) < 32 for c in body['Username'])):
            raise Denied()
    elif raw:
        raise Denied(400)
    if kind == 'device':
        devices, _, _ = await native(client, '/Devices', headers=headers)
        if not isinstance(devices, dict) or not isinstance(devices.get('Items'), list):
            raise Denied(502)
        matches = [row for row in devices['Items'] if isinstance(row, dict)
                   and row.get('Id') == params['Id']
                   and row.get('AppName') in {'Seerr', 'Jellyseerr'}]
        if len(matches) != 1:
            raise Denied()
    result, status, response_headers = await native(
        client, path, method=request.method, params=params, headers=headers, body=body,
        image=kind == 'image')
    if status == 204:
        return Response(status_code=204)
    if kind == 'image':
        return Response(content=b'' if request.method == 'HEAD' else result,
                        status_code=status, headers=response_headers)
    return JSONResponse(result, status_code=status)
