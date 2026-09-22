#!/usr/bin/env bash
set -euo pipefail

config=''; environment=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config=${2:?missing config}; shift 2 ;;
    --environment) environment=${2:?missing environment}; shift 2 ;;
    -h|--help) echo 'Usage: smoke.sh --config FILE --environment dev|prod'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ -f "$config" ]] || { echo 'config is required' >&2; exit 2; }
[[ "$environment" == dev || "$environment" == prod ]] || { echo 'environment must be dev or prod' >&2; exit 2; }
# shellcheck disable=SC1090
source "$config"
if [[ "$environment" == prod ]]; then
  : "${HOMESERVER_MEDIA_PATH:=/srv/data}"
  : "${HOMESERVER_MEDIA_UUID:?HOMESERVER_MEDIA_UUID is required for prod smoke}"
  bash "$(dirname "${BASH_SOURCE[0]}")/check-mount.sh" "$HOMESERVER_MEDIA_PATH" "$HOMESERVER_MEDIA_UUID" >/dev/null
fi
if [[ -n "${HOMESERVER_HEALTH_URL:-}" ]] && command -v curl >/dev/null 2>&1; then
  curl --fail --silent --show-error --max-time 5 "$HOMESERVER_HEALTH_URL/health/live" >/dev/null
fi
echo "smoke checks passed: $environment"
