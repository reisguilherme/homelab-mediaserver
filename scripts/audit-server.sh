#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
Usage: audit-server.sh --check [--json]

Environment:
  AUDIT_INVENTORY_PATH  YAML inventory output (default: config/server.local.yaml)
  AUDIT_REPORT_PATH     Markdown report output (default: docs/evidence/server-audit.md)
USAGE
}

check_mode=false
json_mode=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) check_mode=true; shift ;;
    --json) json_mode=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$check_mode" != true ]]; then
  echo "--check is required" >&2
  usage
  exit 2
fi

inventory_path=${AUDIT_INVENTORY_PATH:-config/server.local.yaml}
report_path=${AUDIT_REPORT_PATH:-docs/evidence/server-audit.md}
mkdir -p "$(dirname "$inventory_path")" "$(dirname "$report_path")"

redact() {
  local value=$1
  value=$(printf '%s' "$value" | sed -E \
    -e 's/(authorized_keys|private[_-]?key|password|token|cookie|secret|authorization)[^[:space:]=:]*[[:space:]]*[:=][^[:space:]]+/\1=<redacted>/Ig' \
    -e 's/(authorized_keys)[^[:space:]]*/\1=<redacted>/Ig')
  printf '%s' "$value"
}

json_escape() {
  local value=${1:-}
  value=${value//$'\\'/$'\\\\'}
  value=${value//\"/\\\"}
  value=${value//$'\r'/$'\\r'}
  value=${value//$'\n'/$'\\n'}
  printf '%s' "$value"
}

declare -a check_names=()
declare -a check_statuses=()
declare -a check_values=()
declare -a check_codes=()
declare -a check_commands=()
required_failures=0

collect() {
  local name=$1
  local command_text=$2
  shift 2
  local output exit_code status
  if output=$("$@" 2>&1); then
    exit_code=0
    status=verified
  else
    exit_code=$?
    status=missing
    required_failures=$((required_failures + 1))
  fi
  output=$(redact "$output")
  check_names+=("$name")
  check_statuses+=("$status")
  check_values+=("$output")
  check_codes+=("$exit_code")
  check_commands+=("$command_text")
}

collect os_release 'cat /etc/os-release' cat /etc/os-release
collect kernel 'uname -r' uname -r
collect identity 'id' id
collect docker_version 'docker version' docker version
collect compose_version 'docker compose version' docker compose version
collect media_mount 'findmnt --json --target /srv/data --output TARGET,SOURCE,UUID,FSTYPE,OPTIONS' findmnt --json --target /srv/data --output TARGET,SOURCE,UUID,FSTYPE,OPTIONS
collect disk_usage 'df -B1 /srv/data /srv/appdata /srv/transcode /srv/backup-staging' df -B1 /srv/data /srv/appdata /srv/transcode /srv/backup-staging
collect block_devices 'lsblk --json --output NAME,SIZE,FSTYPE,UUID,MOUNTPOINTS' lsblk --json --output NAME,SIZE,FSTYPE,UUID,MOUNTPOINTS
collect service_status 'systemctl is-active ssh tailscaled docker lenovo-conservation.service' systemctl is-active ssh tailscaled docker lenovo-conservation.service
collect power_targets 'systemctl is-enabled sleep.target suspend.target hibernate.target hybrid-sleep.target' systemctl is-enabled sleep.target suspend.target hibernate.target hybrid-sleep.target
collect logind_config 'systemd-analyze cat-config systemd/logind.conf' systemd-analyze cat-config systemd/logind.conf
collect permissions 'stat -c %u:%g %a %n /srv/data /srv/appdata /srv/transcode /srv/backup-staging' stat -c '%u:%g %a %n' /srv/data /srv/appdata /srv/transcode /srv/backup-staging
collect render_devices 'ls -l /dev/dri' ls -l /dev/dri
collect pci_devices 'lspci -nnk' lspci -nnk

generated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if (( required_failures == 0 )); then
  overall_status=verified
  overall_exit=0
else
  overall_status=missing
  overall_exit=2
fi

{
  printf 'schema_version: 1\n'
  printf 'generated_at: %s\n' "$generated_at"
  printf 'status: %s\n' "$overall_status"
  printf 'checks:\n'
  for i in "${!check_names[@]}"; do
    printf '  - name: %s\n' "${check_names[$i]}"
    printf '    command: %s\n' "${check_commands[$i]}"
    printf '    status: %s\n' "${check_statuses[$i]}"
    printf '    return_code: %s\n' "${check_codes[$i]}"
    printf '    value: |-\n'
    while IFS= read -r line; do printf '      %s\n' "$line"; done <<< "${check_values[$i]}"
  done
} > "$inventory_path"

{
  printf '# Server baseline audit\n\n'
  printf -- '- Generated: `%s`\n' "$generated_at"
  printf -- '- Overall status: `%s`\n' "$overall_status"
  printf -- '- This report is observational; it never changes packages, mounts, power, SSH, Tailscale or Docker configuration.\n\n'
  printf '| Check | Status | Return code | Value |\n|---|---|---:|---|\n'
  for i in "${!check_names[@]}"; do
    value=${check_values[$i]//$'\n'/<br>}
    printf '| `%s` | `%s` | `%s` | %s |\n' "${check_names[$i]}" "${check_statuses[$i]}" "${check_codes[$i]}" "$value"
  done
} > "$report_path"

if [[ "$json_mode" == true ]]; then
  printf '{"schema_version":1,"generated_at":"%s","status":"%s","checks":[' "$(json_escape "$generated_at")" "$overall_status"
  for i in "${!check_names[@]}"; do
    (( i > 0 )) && printf ','
    printf '{"command":"%s","status":"%s","return_code":%s,"value":"%s"}' \
      "$(json_escape "${check_names[$i]}")" \
      "${check_statuses[$i]}" \
      "${check_codes[$i]}" \
      "$(json_escape "${check_values[$i]}")"
  done
  printf ']}\n'
fi

if [[ "$json_mode" != true ]]; then
  printf 'Audit status: %s\nReport: %s\nInventory: %s\n' "$overall_status" "$report_path" "$inventory_path"
fi
exit "$overall_exit"
