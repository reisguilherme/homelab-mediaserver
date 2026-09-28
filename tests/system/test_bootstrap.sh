#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
script="$repo_root/scripts/bootstrap-server.sh"
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$test_root/etc" "$test_root/srv/data"
printf 'fixture-fstab\n' > "$test_root/etc/fstab"
before=$(sha256sum "$test_root/etc/fstab")

config="$test_root/server.env"
cat > "$config" <<EOF
HOMESERVER_ENVIRONMENT=dev
HOMESERVER_INSTALL_ROOT=$test_root/install
HOMESERVER_APPDATA_ROOT=$test_root/appdata
HOMESERVER_MEDIA_ROOT=$test_root/srv/data
HOMESERVER_TRANSCODE_ROOT=$test_root/transcode
HOMESERVER_BACKUP_STAGING_ROOT=$test_root/backups
HOMESERVER_RUN_ROOT=$test_root/run
EOF

bash "$script" --check --mode adopt --env-file "$config" > "$test_root/report.json"
bash "$script" --plan --mode adopt --env-file "$config" > "$test_root/plan.json"
after=$(sha256sum "$test_root/etc/fstab")
test "$before" = "$after"
test -s "$test_root/report.json"
test -s "$test_root/plan.json"
test ! -e "$test_root/install"

echo 'bootstrap tests passed'
