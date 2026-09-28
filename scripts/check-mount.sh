#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 PATH UUID" >&2
}

if [[ $# -ne 2 ]]; then
  usage
  exit 2
fi

mount_path=$1
expected_uuid=$2

if [[ -z "$mount_path" || -z "$expected_uuid" ]]; then
  echo "mount path and UUID are required" >&2
  exit 2
fi

if [[ ! -d "$mount_path" ]]; then
  echo "mount path is not a directory: $mount_path" >&2
  exit 1
fi

mount_record=''
if ! mount_record=$(findmnt --json --mountpoint "$mount_path" --output TARGET,UUID,OPTIONS 2>/dev/null); then
  echo "no filesystem is mounted exactly at $mount_path" >&2
  exit 1
fi

python3 -c '
import json, sys
try:
    records = json.load(sys.stdin)["filesystems"]
    if len(records) != 1:
        raise ValueError("expected exactly one mounted filesystem")
    record = records[0]
    if record.get("target") != sys.argv[1]:
        raise ValueError("mounted target mismatch")
    if record.get("uuid") != sys.argv[2]:
        raise ValueError("filesystem UUID mismatch")
    if "ro" in record.get("options", "").split(","):
        raise ValueError("filesystem is mounted read-only")
except (ValueError, KeyError, TypeError) as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
' "$mount_path" "$expected_uuid" <<< "$mount_record"

if [[ ! -w "$mount_path" ]]; then
  echo "mount path is not writable: $mount_path" >&2
  exit 1
fi

echo "mount guard passed: $mount_path ($expected_uuid)"
