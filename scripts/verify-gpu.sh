#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: verify-gpu.sh --device PATH --user USER" >&2
}

device=''
user_name=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --device) device=${2:?missing render device}; shift 2 ;;
    --user) user_name=${2:?missing service user}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$device" && -n "$user_name" ]] || { usage; exit 2; }
[[ -e "$device" ]] || { echo "render device is missing: $device" >&2; exit 1; }
id "$user_name" >/dev/null 2>&1 || { echo "service user is missing: $user_name" >&2; exit 1; }
stat -c 'device=%n mode=%a owner=%u:%g' "$device"
groups=$(id -Gn "$user_name")
case " $groups " in
  *" render "*|*" video "*) ;;
  *)
    echo "service user is not in render/video group: $user_name" >&2
    exit 1
    ;;
esac
if command -v vainfo >/dev/null 2>&1; then
  vainfo --display drm --device "$device" 2>&1 | sed -n '1,30p'
else
  echo 'vainfo unavailable; device/group checks passed only' >&2
fi
echo "GPU access checks passed for $user_name on $device"
