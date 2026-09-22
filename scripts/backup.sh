#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: backup.sh --config FILE (--capture-and-send|--send-pending|--verify-repository)" >&2
}

config=''
action=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config=${2:?missing config path}; shift 2 ;;
    --capture-and-send|--send-pending|--verify-repository)
      [[ -z "$action" ]] || { echo 'only one action is allowed' >&2; exit 2; }
      action=${1#--}; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$config" && -n "$action" ]] || { usage; exit 2; }
[[ -f "$config" ]] || { echo "config not found: $config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"

: "${BACKUP_STAGING_ROOT:?BACKUP_STAGING_ROOT is required}"
: "${BACKUP_REPOSITORY:?BACKUP_REPOSITORY is required}"
: "${BACKUP_MAX_BYTES:=20000000000}"
: "${BACKUP_ITEMS:=}"
: "${BACKUP_LOCK:=.runtime/backup.lock}"

mkdir -p "$BACKUP_STAGING_ROOT" "$BACKUP_REPOSITORY" "$(dirname "$BACKUP_LOCK")"
lock_dir="$BACKUP_LOCK.d"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo 'another backup operation is running' >&2
  exit 2
fi
trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT

checksum_manifest() {
  local root=$1
  (cd "$root" && find . -type f ! -name manifest.sha256 ! -name .sent -print0 | sort -z | xargs -0 sha256sum > manifest.sha256)
}

capture() {
  [[ -n "$BACKUP_ITEMS" ]] || { echo 'BACKUP_ITEMS is empty' >&2; return 1; }
  local id tmp final item name bytes
  id=$(date -u +%Y%m%dT%H%M%SZ)-$$
  tmp="$BACKUP_STAGING_ROOT/.incomplete-$id"
  final="$BACKUP_STAGING_ROOT/$id"
  mkdir "$tmp"
  for item in $BACKUP_ITEMS; do
    [[ -e "$item" && ! -L "$item" ]] || { echo "backup item missing or symlink: $item" >&2; rm -rf "$tmp"; return 1; }
    name=$(basename "$item")
    cp -a "$item" "$tmp/$name"
  done
  bytes=$(du -sb "$tmp" | awk '{print $1}')
  if (( bytes > BACKUP_MAX_BYTES )); then
    echo "backup staging exceeds limit: $bytes > $BACKUP_MAX_BYTES" >&2
    rm -rf "$tmp"
    return 1
  fi
  checksum_manifest "$tmp"
  printf '{"schema_version":1,"snapshot_id":"%s","bytes":%s,"admission_enabled":false}\n' "$id" "$bytes" > "$tmp/metadata.json"
  mv "$tmp" "$final"
  cp -a "$final" "$BACKUP_REPOSITORY/$id"
  touch "$BACKUP_REPOSITORY/$id/.sent"
  echo "backup snapshot ready: $id"
}

send_pending() {
  local snapshot id
  shopt -s nullglob
  for snapshot in "$BACKUP_STAGING_ROOT"/*; do
    [[ -d "$snapshot" && "$snapshot" != *.incomplete-* ]] || continue
    [[ -f "$snapshot/.sent" ]] && continue
    [[ -f "$snapshot/manifest.sha256" ]] || continue
    (cd "$snapshot" && sha256sum -c manifest.sha256 >/dev/null)
    id=$(basename "$snapshot")
    cp -a "$snapshot" "$BACKUP_REPOSITORY/$id"
    touch "$BACKUP_REPOSITORY/$id/.sent" "$snapshot/.sent"
    echo "sent backup snapshot: $id"
  done
}

verify_repository() {
  local failures=0 snapshot
  shopt -s nullglob
  for snapshot in "$BACKUP_REPOSITORY"/*; do
    [[ -d "$snapshot" && -f "$snapshot/manifest.sha256" ]] || continue
    if ! (cd "$snapshot" && sha256sum -c manifest.sha256 >/dev/null); then
      echo "checksum failed: $snapshot" >&2
      failures=$((failures + 1))
    fi
  done
  (( failures == 0 ))
}

case "$action" in
  capture-and-send) capture ;;
  send-pending) send_pending ;;
  verify-repository) verify_repository ;;
  *) echo "unsupported action: $action" >&2; exit 2 ;;
esac
