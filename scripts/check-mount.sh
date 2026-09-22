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
if ! mount_record=$(findmnt --mountpoint "$mount_path" --output TARGET,UUID,OPTIONS --noheadings 2>/dev/null); then
  echo "no filesystem is mounted exactly at $mount_path" >&2
  exit 1
fi

read -r actual_target actual_uuid actual_options _ <<< "$mount_record"

if [[ "$actual_target" != "$mount_path" ]]; then
  echo "mounted target mismatch: expected $mount_path, got ${actual_target:-<empty>}" >&2
  exit 1
fi

if [[ "$actual_uuid" != "$expected_uuid" ]]; then
  echo "filesystem UUID mismatch: expected $expected_uuid, got ${actual_uuid:-<empty>}" >&2
  exit 1
fi

case ",$actual_options," in
  *,ro,*)
    echo "filesystem is mounted read-only: $mount_path" >&2
    exit 1
    ;;
esac

if [[ ! -w "$mount_path" ]]; then
  echo "mount path is not writable: $mount_path" >&2
  exit 1
fi

echo "mount guard passed: $mount_path ($actual_uuid)"
