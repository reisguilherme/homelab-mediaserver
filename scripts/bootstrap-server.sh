#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: bootstrap-server.sh (--check|--plan|--apply) [--mode adopt|fresh] --config FILE" >&2
}

action=''
mode=adopt
config=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check|--plan|--apply) [[ -z "$action" ]] || { echo 'only one action is allowed' >&2; exit 2; }; action=${1#--}; shift ;;
    --mode) mode=${2:?missing mode}; shift 2 ;;
    --config) config=${2:?missing config}; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done
[[ -n "$action" && -n "$config" ]] || { usage; exit 2; }
[[ -f "$config" ]] || { echo "config not found: $config" >&2; exit 1; }

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
source "$script_dir/lib/bootstrap.sh"
BOOTSTRAP_MODE=$mode
export BOOTSTRAP_MODE

state_root=${HOMESERVER_STATE_ROOT:-${HOMESERVER_FIXTURE_ROOT:-.runtime}/bootstrap}
report=${HOMESERVER_REPORT:-$state_root/report.yaml}
mkdir -p "$state_root"
lock_dir="$state_root/lock.d"
if ! mkdir "$lock_dir" 2>/dev/null; then
  echo 'another bootstrap operation is running' >&2
  exit 2
fi
trap 'rmdir "$lock_dir" 2>/dev/null || true' EXIT

failed=0
detect_platform || failed=1
check_packages || failed=1
check_services || failed=1
check_power || failed=1
check_storage "$config" || failed=1
plan_changes

case "$action" in
  check)
    if (( failed == 0 )); then write_report "$report" ok; else write_report "$report" incomplete; fi
    (( failed == 0 )) || exit 2
    ;;
  plan)
    write_report "$report" "$([[ $failed -eq 0 ]] && echo ready || echo incomplete)"
    printf 'Bootstrap plan (%s):\n' "$mode"
    printf '  - %s\n' "${BOOTSTRAP_CHANGES[@]}"
    (( failed == 0 )) || exit 2
    ;;
  apply)
    (( failed == 0 )) || { write_report "$report" incomplete; exit 2; }
    apply_missing "$mode" || { write_report "$report" failed; exit 1; }
    write_report "$report" applied
    ;;
esac
