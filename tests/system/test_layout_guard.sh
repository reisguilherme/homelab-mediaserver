#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
test_root=$(mktemp -d)
trap 'rm -rf "$test_root"' EXIT
mkdir -p "$test_root/data/torrents" "$test_root/data/media"
printf fixture > "$test_root/data/torrents/source.mkv"

HOMESERVER_LAYOUT_ROOT="$test_root/data" bash "$repo_root/scripts/verify-layout.sh" --environment dev
echo 'layout guard tests passed'
