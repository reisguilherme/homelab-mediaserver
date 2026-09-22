#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
script="$repo_root/scripts/bootstrap-server.sh"
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$test_root/etc" "$test_root/srv/data"
printf 'fixture-fstab\n' > "$test_root/etc/fstab"
before=$(sha256sum "$test_root/etc/fstab")

config="$test_root/server-valid.yaml"
cat > "$config" <<EOF
server:
  mode: adopt
storage:
  media_mount: $test_root/srv/data
  media_uuid: fixture-uuid
EOF

HOMESERVER_FIXTURE_ROOT="$test_root" HOMESERVER_REPORT="$test_root/report.yaml" \
  bash "$script" --check --config "$config"
HOMESERVER_FIXTURE_ROOT="$test_root" HOMESERVER_REPORT="$test_root/plan.yaml" \
  bash "$script" --plan --config "$config"
after=$(sha256sum "$test_root/etc/fstab")
test "$before" = "$after"
test -s "$test_root/report.yaml"
test -s "$test_root/plan.yaml"

echo 'bootstrap tests passed'
