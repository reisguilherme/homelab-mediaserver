"""Modern Jellyfin credentials for internal clients, never a relay credential source."""

import re

_TOKEN = re.compile(r'[A-Za-z0-9._~-]+\Z')
_CLIENT = ('MediaBrowser Client="HomeServer", Device="HomeServer", '
           'DeviceId="homeserver-internal", Version="1"')


def jellyfin_headers(token: str) -> dict[str, str]:
    """Return native authentication without accepting header/schema injection."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError('Invalid Jellyfin credential')
    return {'Authorization': f'{_CLIENT}, Token="{token}"'}
