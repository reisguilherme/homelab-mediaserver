#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
script="$repo_root/scripts/audit-server.sh"
test_root=$(mktemp -d)
fake_bin="$test_root/bin"
mkdir -p "$fake_bin"
trap 'rm -rf "$test_root"' EXIT

cat > "$fake_bin/probe" <<'FAKE_PROBE'
#!/usr/bin/env bash
printf 'fixture-safe-value\n'
FAKE_PROBE
chmod +x "$fake_bin/probe"
for command_name in cat uname id docker findmnt df lsblk systemctl systemd-analyze stat ls lspci; do
  cp "$fake_bin/probe" "$fake_bin/$command_name"
done

json_output="$test_root/audit.json"
inventory_output="$test_root/server.local.yaml"
report_output="$test_root/server-audit.md"
PATH="$fake_bin:$PATH" \
  AUDIT_INVENTORY_PATH="$inventory_output" \
  AUDIT_REPORT_PATH="$report_output" \
  bash "$script" --check --json > "$json_output"

grep -q '"status":"verified"' "$json_output"
grep -q '"command":"os_release"' "$json_output"
test -s "$inventory_output"
test -s "$report_output"
! grep -Eiq 'authorized_keys|cookie|token|secret' "$json_output"
! grep -Eiq 'authorized_keys|cookie|token|secret' "$inventory_output"

echo 'audit server tests passed'
