#!/usr/bin/env bash
set -euo pipefail

release=''; config=''; snapshot=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) release=${2:?missing release}; shift 2 ;;
    --config) config=${2:?missing config}; shift 2 ;;
    --snapshot) snapshot=${2:?missing snapshot}; shift 2 ;;
    -h|--help) echo 'Usage: rollback.sh --release SHA --config FILE [--snapshot ID]'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$release" =~ ^[0-9a-fA-F]{40}$ && -f "$config" ]] || exit 2
# shellcheck disable=SC1090
source "$config"
: "${HOMESERVER_ROOT:?HOMESERVER_ROOT is required}"
target="$HOMESERVER_ROOT/releases/$release"
[[ -d "$target" ]] || { echo "release not found: $release" >&2; exit 1; }
if [[ -n "$snapshot" ]]; then
  printf 'rollback requested with snapshot %s; restore is a separate isolated operation\n' "$snapshot"
fi
ln -sfn "$target" "$HOMESERVER_ROOT/current"
printf 'current release is now %s\n' "$release"
