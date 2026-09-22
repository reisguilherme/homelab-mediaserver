#!/usr/bin/env bash
set -euo pipefail

BOOTSTRAP_STATUS_NAMES=()
BOOTSTRAP_STATUS_VALUES=()
BOOTSTRAP_CHANGES=()

record_status() {
  BOOTSTRAP_STATUS_NAMES+=("$1")
  BOOTSTRAP_STATUS_VALUES+=("$2")
}

config_value() {
  local key=$1 file=$2
  awk -v key="$key" '$1 == key ":" {sub(/^[^:]+:[[:space:]]*/, ""); gsub(/^['"'"']|['"'"']$/, ""); print; exit}' "$file"
}

detect_platform() {
  if [[ -n "${HOMESERVER_FIXTURE_ROOT:-}" ]]; then
    record_status platform fixture
    return 0
  fi
  if [[ -r /etc/os-release ]] && grep -q '^ID=ubuntu' /etc/os-release; then
    record_status platform ubuntu
    return 0
  fi
  record_status platform unsupported
  return 1
}

check_packages() {
  local missing=0 command_name
  if [[ -n "${HOMESERVER_FIXTURE_ROOT:-}" ]]; then
    record_status packages fixture
    return 0
  fi
  for command_name in bash findmnt awk sha256sum; do
    if command -v "$command_name" >/dev/null 2>&1; then
      record_status "package:$command_name" present
    else
      record_status "package:$command_name" missing
      missing=$((missing + 1))
    fi
  done
  return "$missing"
}

check_services() {
  if [[ -n "${HOMESERVER_FIXTURE_ROOT:-}" ]]; then
    record_status services fixture
    return 0
  fi
  local missing=0 service
  for service in ssh tailscaled docker; do
    if systemctl is-active --quiet "$service"; then
      record_status "service:$service" active
    else
      record_status "service:$service" missing
      missing=$((missing + 1))
    fi
  done
  return "$missing"
}

check_power() {
  if [[ -n "${HOMESERVER_FIXTURE_ROOT:-}" ]]; then
    record_status power fixture
    return 0
  fi
  if [[ -r /etc/systemd/logind.conf ]]; then
    record_status power logind-config-present
    return 0
  fi
  record_status power unknown
  return 1
}

check_storage() {
  local config_file=$1
  local mount_path
  mount_path=$(config_value media_mount "$config_file")
  if [[ -z "$mount_path" || ! -d "$mount_path" ]]; then
    record_status storage missing
    return 1
  fi
  record_status storage "directory:$mount_path"
  if [[ -n "${HOMESERVER_FIXTURE_ROOT:-}" ]]; then
    return 0
  fi
  local uuid
  uuid=$(config_value media_uuid "$config_file")
  if [[ -z "$uuid" ]] || ! bash "$(dirname "${BASH_SOURCE[0]}")/../check-mount.sh" "$mount_path" "$uuid" >/dev/null; then
    record_status storage mount-guard-failed
    return 1
  fi
  record_status storage mount-guard-passed
}

plan_changes() {
  BOOTSTRAP_CHANGES=(
    'verify required packages and services'
    'verify the existing media mount by UUID'
    'write a sanitized bootstrap report'
  )
  if [[ "${BOOTSTRAP_MODE:-adopt}" == fresh ]]; then
    BOOTSTRAP_CHANGES+=("prepare missing packages only after explicit apply")
  else
    BOOTSTRAP_CHANGES+=("preserve existing SSH, Docker, Tailscale, power and Lenovo files")
  fi
}

apply_missing() {
  local mode=${1:-adopt}
  if [[ "$mode" != adopt && "$mode" != fresh ]]; then
    echo "invalid bootstrap mode: $mode" >&2
    return 2
  fi
  if [[ "$mode" == adopt ]]; then
    record_status apply no-host-changes-needed
    return 0
  fi
  if [[ -z "${HOMESERVER_EXPLICIT_FRESH:-}" ]]; then
    echo 'fresh mode requires explicit HOMESERVER_EXPLICIT_FRESH=1' >&2
    return 1
  fi
  record_status apply fresh-prerequisites-only
}

write_report() {
  local report=$1 overall=${2:-ok}
  mkdir -p "$(dirname "$report")"
  {
    printf 'schema_version: 1\nstatus: %s\n' "$overall"
    printf 'checks:\n'
    for i in "${!BOOTSTRAP_STATUS_NAMES[@]}"; do
      printf '  - name: %s\n    status: %s\n' "${BOOTSTRAP_STATUS_NAMES[$i]}" "${BOOTSTRAP_STATUS_VALUES[$i]}"
    done
    printf 'planned_changes:\n'
    for change in "${BOOTSTRAP_CHANGES[@]}"; do printf '  - %s\n' "$change"; done
  } > "$report"
}
