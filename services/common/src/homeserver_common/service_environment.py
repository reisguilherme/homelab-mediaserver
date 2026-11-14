"""Explicit process environments: new credentials are never inherited implicitly."""

from collections.abc import Mapping

from .settings import FIELDS, PORTS


def _keys(names: str) -> frozenset[str]:
    return frozenset('HOMESERVER_' + name for name in names.split())


# These variables are needed to locate the installed interpreter and shared libraries.
# Deliberately do not forward arbitrary cloud, proxy, shell or SDK credentials.
PROCESS_ENVIRONMENT = frozenset({
    'PATH', 'PYTHONPATH', 'PYTHONUNBUFFERED', 'PYTHONDONTWRITEBYTECODE',
    'LANG', 'LC_ALL', 'TZ', 'HOME', 'TMPDIR', 'TMP', 'TEMP',
    'SYSTEMROOT', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT',
})
COMMON = _keys('SERVICE_ROLE LOG_LEVEL TIMEZONE')
GENERATED = _keys(
    'DB_PATH MEDIA_ROOTS CAPACITY_SNAPSHOT HOST_SNAPSHOT RECOVERY_MODE '
    'ARR_UID ARR_GID WORKER_INTERVAL STORAGE_REGISTRY'
)
REGISTERED = frozenset('HOMESERVER_' + name.upper() for name in FIELDS)
NONSECRET = frozenset('HOMESERVER_' + name.upper()
                      for name, field in FIELDS.items() if not field.secret)
PRIVILEGED_ROLES = frozenset({'control-api', 'control-worker', 'download-gateway'})
# Telemetry implements authenticated private administrative operations. It reads
# the original file to preserve literal passwords, but passes only its four
# required authentication values to the child process (never native app keys).
ENV_FILE_ROLES = PRIVILEGED_ROLES | {'telemetry'}
ROLE_KEYS = {
    'control-api': REGISTERED | GENERATED,
    'control-worker': REGISTERED | GENERATED | _keys('WORKER_ONCE'),
    'download-gateway': NONSECRET | GENERATED | _keys('ARR_TOKEN QBIT_PASSWORD'),
    'telemetry': _keys(
        'ADMIN_USERNAME ADMIN_PASSWORD ADMIN_TOKEN CSRF_TOKEN HOST_SNAPSHOT '
        'CAPACITY_SNAPSHOT MEDIA_ROOT MEDIA_UUID METRICS_MAX_AGE_SECONDS '
        'CAPACITY_SNAPSHOT_MAX_AGE_SECONDS STORAGE_REGISTRY TAILSCALE_HOSTNAME'
    ) | frozenset('HOMESERVER_' + name.upper() + suffix
                  for name in PORTS for suffix in ('_PUBLIC_URL', '_PORT')),
    'host-metrics': _keys(
        'METRICS_INTERVAL_SECONDS MEDIA_ROOT MEDIA_UUID RUN_ROOT '
        'NETWORK_INTERFACE STORAGE_REGISTRY'
    ),
    'jellyfin-relay': _keys(
        'JELLYFIN_URL JELLYFIN_SUBTITLE_LABELS_ENABLED '
        'JELLYFIN_SUBTITLE_LABEL_EN_US JELLYFIN_SUBTITLE_LABEL_PT_BR'
    ),
    'public-security': _keys(
        'JELLYFIN_URL SEERR_URL PUBLIC_ACCESS_STATE_PATH PUBLIC_SEERR_ORIGIN '
        'PUBLIC_ACCESS_TRUSTED_PROXY_IPS'
    ),
    'integration-proxy': frozenset(),
}


def environment_for_service(role: str, environment: Mapping[str, str]) -> dict[str, str]:
    """Select only reviewed settings; unknown roles and keys never broaden access."""
    if role not in ROLE_KEYS:
        raise ValueError('unknown service role')
    allowed = ROLE_KEYS[role] | COMMON | PROCESS_ENVIRONMENT
    return {key: value for key, value in environment.items() if key in allowed}
