#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: restore.sh --config FILE --snapshot ID --target PATH --isolated" >&2
}

config=''
snapshot=''
target=''
isolated=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) config=${2:?missing config path}; shift 2 ;;
    --snapshot) snapshot=${2:?missing snapshot id}; shift 2 ;;
    --target) target=${2:?missing restore target}; shift 2 ;;
    --isolated) isolated=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$config" && -n "$snapshot" && -n "$target" && "$isolated" == true ]] || { usage; exit 2; }
[[ -f "$config" ]] || { echo "config not found: $config" >&2; exit 1; }
# shellcheck disable=SC1090
source "$config"
: "${BACKUP_REPOSITORY:?BACKUP_REPOSITORY is required}"
: "${BACKUP_RESTORE_ROOT:?BACKUP_RESTORE_ROOT is required}"

[[ "$BACKUP_RESTORE_ROOT" = /* && "$target" = /* ]] || {
  echo 'restore root and target must be absolute paths' >&2
  exit 1
}

restore_root_real=$(realpath -m -- "$BACKUP_RESTORE_ROOT")
target_real=$(realpath -m -- "$target")
case "$target_real" in
  "$restore_root_real"/*) ;;
  *) echo "restore target must resolve under BACKUP_RESTORE_ROOT: $BACKUP_RESTORE_ROOT" >&2; exit 1 ;;
esac

relative_target=${target_real#"$restore_root_real"/}
cursor=$restore_root_real
IFS=/ read -r -a target_parts <<< "$relative_target"
for part in "${target_parts[@]}"; do
  [[ -n "$part" ]] || continue
  cursor="$cursor/$part"
  [[ ! -L "$cursor" ]] || { echo "restore target contains a symlink: $cursor" >&2; exit 1; }
done

case "$target_real" in
  /|/srv|/srv/data|/srv/appdata|/srv/transcode|/srv/backup-staging)
    echo 'production roots are never valid restore targets' >&2
    exit 1
    ;;
esac
target=$target_real

source_dir="$BACKUP_REPOSITORY/$snapshot"
[[ -d "$source_dir" && -f "$source_dir/manifest.sha256" ]] || { echo "snapshot not found: $snapshot" >&2; exit 1; }
(cd "$source_dir" && sha256sum -c manifest.sha256 >/dev/null) || { echo 'snapshot checksum failed' >&2; exit 1; }
mkdir -p "$target"
cp -a "$source_dir"/. "$target"/
printf 'admission_enabled=false\nrecovery_snapshot=%s\n' "$snapshot" > "$target/RECOVERY_MODE"
echo "restored isolated snapshot $snapshot to $target with admission disabled"
