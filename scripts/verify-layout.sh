#!/usr/bin/env bash
set -euo pipefail

environment=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --environment) environment=${2:?missing environment}; shift 2 ;;
    -h|--help) echo 'Usage: verify-layout.sh --environment dev|prod'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$environment" == dev || "$environment" == prod ]] || { echo 'environment must be dev or prod' >&2; exit 2; }

if [[ -n "${HOMESERVER_LAYOUT_ROOT:-}" ]]; then
  root=$HOMESERVER_LAYOUT_ROOT
elif [[ "$environment" == dev ]]; then
  root=.runtime/dev/data
else
  root=/srv/data
fi
torrents="$root/torrents"
media="$root/media"
[[ -d "$torrents" && -d "$media" ]] || { echo "layout directories missing below $root" >&2; exit 1; }

if [[ "$environment" == prod ]]; then
  : "${HOMESERVER_MEDIA_UUID:?HOMESERVER_MEDIA_UUID is required for prod layout verification}"
  bash "$(dirname "${BASH_SOURCE[0]}")/check-mount.sh" "$root" "$HOMESERVER_MEDIA_UUID" >/dev/null
fi

test_dir="$torrents/.homeserver-layout-test-$$"
test_file="$test_dir/payload.bin"
imported="$media/.homeserver-layout-test-$$.bin"
mkdir "$test_dir"
trap 'rm -rf "$test_dir" "$imported"' EXIT
printf 'homeserver-layout-fixture' > "$test_file"
if ! ln "$test_file" "$imported"; then
  echo "hardlink creation failed between $torrents and $media" >&2
  exit 1
fi
original_stat=$(stat -c '%d:%i' "$test_file")
imported_stat=$(stat -c '%d:%i' "$imported")
[[ "$original_stat" == "$imported_stat" ]] || { echo 'hardlink device/inode mismatch' >&2; exit 1; }
rm "$test_file"
[[ "$(cat "$imported")" == homeserver-layout-fixture ]] || { echo 'payload disappeared after unlink' >&2; exit 1; }
echo "layout verified: $root"
